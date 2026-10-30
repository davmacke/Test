import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from analysis import portfolio_risk


ACTIVE_ORDER_STATES = {"SUBMITTING", "UNKNOWN", "PENDING", "OPEN", "CLOSING", "CANCELLING"}


def _now():
    return datetime.now(timezone.utc)


def _float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _id(row, *keys):
    for key in keys:
        if row.get(key) is not None:
            return row[key]
    return None


def _pnl(position):
    for key in ("pnL", "pnl", "PnL", "unrealizedPnL"):
        if position.get(key) is not None:
            return _float(position[key], 0.0)
    return 0.0


def _flag(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _managed_positions(portfolio):
    client = (portfolio or {}).get("clientPortfolio") or {}
    return {
        str(_id(position, "positionID", "positionId")): position
        for position in (client.get("positions") or [])
        if _id(position, "positionID", "positionId") is not None
    }


def _save_order(connection, order_id, status, **values):
    fields = ["status=?", "updated_at=?"]
    args = [status, _now().isoformat()]
    allowed = {
        "order_id", "position_id", "units", "entry_price", "pnl",
        "close_order_id", "message", "request_id",
    }
    for field, value in values.items():
        if field in allowed:
            fields.append(f"{field}=?")
            args.append(value)
    args.append(order_id)
    connection.execute(
        f"UPDATE demo_bot_orders SET {', '.join(fields)} WHERE id=?",
        args,
    )
    connection.commit()


def _sync_orders(connection, feed, portfolio, log):
    positions = _managed_positions(portfolio)
    rows = connection.execute(
        "SELECT * FROM demo_bot_orders ORDER BY id"
    ).fetchall()

    for row in rows:
        state = row["status"]
        position_id = row["position_id"]
        if state in {"OPEN", "CLOSING"} and position_id is not None:
            position = positions.get(str(position_id))
            if position:
                _save_order(
                    connection, row["id"], state,
                    pnl=_pnl(position),
                )
            else:
                _save_order(connection, row["id"], "CLOSED", pnl=row["pnl"])
            continue

        if state not in {"SUBMITTING", "UNKNOWN", "PENDING", "CANCELLING"}:
            continue
        try:
            order = feed.lookup_demo_order(
                order_id=row["order_id"],
                reference_id=None if row["order_id"] else row["request_id"],
            )
            status = order.get("status") or {}
            status_id = int(status.get("id") or 0)
            if status_id in {3, 5}:
                executions = order.get("positionExecutions") or []
                execution = executions[0] if executions else {}
                opening = execution.get("openingData") or {}
                _save_order(
                    connection, row["id"], "OPEN",
                    position_id=_id(execution, "positionId", "positionID"),
                    units=_id(execution, "remainingUnits", "remainingContracts"),
                    entry_price=_id(opening, "avgPrice") or row["entry_price"],
                    message=status.get("name", "Filled"),
                )
            elif status_id in {4, 10}:
                message = status.get("errorMessage") or status.get("name") or "Rejected"
                _save_order(connection, row["id"], "REJECTED", message=message)
                log("WARN", f"Demo order rejected for {row['symbol']}: {message}")
            else:
                _save_order(
                    connection, row["id"], "PENDING",
                    message=status.get("name", "Waiting for eToro execution"),
                )
        except Exception as error:
            # Keep an ambiguous submission blocked. The same request id is checked next cycle.
            _save_order(connection, row["id"], "UNKNOWN", message=f"{type(error).__name__}: {error}")


def _close_order(connection, feed, row, log, reason):
    if not row["position_id"]:
        _save_order(connection, row["id"], "UNKNOWN", message="Missing position id; close skipped")
        return
    request_id = str(uuid.uuid4())
    try:
        result = feed.close_demo_position(
            row["position_id"], row["instrument_id"], request_id
        )
        close = result.get("orderForClose") or {}
        _save_order(
            connection, row["id"], "CLOSING",
            close_order_id=_id(close, "orderID", "orderId"),
            request_id=request_id,
            message=reason,
        )
        log("INFO", f"Requested demo close for {row['symbol']} ({reason})")
    except Exception as error:
        # Do not automatically resend an ambiguous close request.
        _save_order(
            connection, row["id"], "CLOSING",
            request_id=request_id,
            message=f"Close outcome unknown: {type(error).__name__}: {error}",
        )
        log("ERROR", f"Demo close outcome unknown for {row['symbol']}: {type(error).__name__}")


def run_demo_cycle(connection, feed, mapping, quotes, portfolio, log):
    enabled = os.getenv("DEMO_AUTOTRADE_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
    mode = os.getenv("ETORO_TRADING_MODE", "demo").strip().lower()
    if not enabled:
        return {"status": "DISABLED", "message": "Set DEMO_AUTOTRADE_ENABLED=true to enable demo execution."}
    if mode != "demo":
        return {"status": "BLOCKED", "message": "Order execution is hard-locked to demo mode."}

    client = (portfolio or {}).get("clientPortfolio") or {}
    if not client:
        return {"status": "WAITING", "message": "Waiting for a readable eToro demo portfolio."}

    risk = portfolio_risk(portfolio, {
        str(meta.get("instrument_id")): meta.get("asset_class", "Unclassified")
        for meta in mapping.values()
    })
    now = _now()
    hours = max(1, min(12, int(os.getenv("DEMO_RUN_HOURS", "8"))))
    state = connection.execute(
        "SELECT * FROM demo_bot_state WHERE id=1"
    ).fetchone()
    if state is None:
        started = now.isoformat()
        ends = (now + timedelta(hours=hours)).isoformat()
        start_unrealized = _float(client.get("unrealizedPnL", client.get("unrealisedPnL")), 0.0)
        start_realized = _float(client.get("closedPositionsNetProfit"), 0.0)
        connection.execute(
            """INSERT INTO demo_bot_state
               (id,started_at,ends_at,status,start_unrealized_pnl,last_unrealized_pnl,
                start_realized_pnl,last_realized_pnl,stop_reason)
               VALUES(1,?,?,?, ?,?,?,?,?)""",
            (started, ends, "RUNNING", start_unrealized, start_unrealized,
             start_realized, start_realized, ""),
        )
        connection.commit()
        state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()

    current_unrealized = _float(client.get("unrealizedPnL", client.get("unrealisedPnL")), 0.0)
    current_realized = _float(client.get("closedPositionsNetProfit"), state["last_realized_pnl"])
    connection.execute(
        """UPDATE demo_bot_state SET last_unrealized_pnl=?,last_realized_pnl=? WHERE id=1""",
        (current_unrealized, current_realized),
    )
    connection.commit()

    _sync_orders(connection, feed, portfolio, log)
    state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()
    rows = connection.execute("SELECT * FROM demo_bot_orders ORDER BY id").fetchall()
    managed_open_pnl = sum(
        _float(row["pnl"]) for row in rows if row["status"] in {"OPEN", "CLOSING"}
    )
    stop_at = datetime.fromisoformat(state["ends_at"])
    stop_reason = state["stop_reason"] or ""
    if state["status"] == "RUNNING" and managed_open_pnl <= -abs(float(os.getenv("DEMO_MAX_OPEN_LOSS_USD", "25"))):
        stop_reason = "Demo open-position loss limit reached"
        connection.execute(
            "UPDATE demo_bot_state SET status='STOPPING',stop_reason=? WHERE id=1",
            (stop_reason,),
        )
        connection.commit()
        state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()
    if state["status"] == "RUNNING" and now >= stop_at:
        stop_reason = "Eight-hour demo run window ended"
        connection.execute(
            "UPDATE demo_bot_state SET status='STOPPING',stop_reason=? WHERE id=1",
            (stop_reason,),
        )
        connection.commit()
        state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()

    if state["status"] == "STOPPING":
        for row in rows:
            if row["status"] == "OPEN":
                _close_order(connection, feed, row, log, stop_reason or "Run stopping")
            elif row["status"] == "PENDING" and row["order_id"]:
                request_id = str(uuid.uuid4())
                try:
                    feed.cancel_demo_order(row["order_id"], request_id)
                    _save_order(connection, row["id"], "CANCELLING", request_id=request_id)
                except Exception as error:
                    _save_order(
                        connection, row["id"], "CANCELLING",
                        request_id=request_id,
                        message=f"Cancel outcome unknown: {type(error).__name__}: {error}",
                    )
        remaining = connection.execute(
            "SELECT COUNT(*) AS n FROM demo_bot_orders WHERE status IN "
            "('OPEN','PENDING','SUBMITTING','UNKNOWN','CLOSING','CANCELLING')"
        ).fetchone()["n"]
        if not remaining:
            connection.execute("UPDATE demo_bot_state SET status='STOPPED' WHERE id=1")
            connection.commit()
        state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()

    if state["status"] == "RUNNING":
        # A deteriorating signal closes only positions opened by this demo bot.
        signal_by_symbol = {}
        for row in connection.execute("SELECT symbol,payload FROM market_analysis WHERE status='ok'"):
            try:
                signal_by_symbol[row["symbol"]] = json.loads(row["payload"]).get("action")
            except (TypeError, json.JSONDecodeError):
                continue
        for row in rows:
            if row["status"] == "OPEN" and signal_by_symbol.get(row["symbol"]) == "REDUCE / AVOID":
                _close_order(connection, feed, row, log, "Signal changed to Reduce / Avoid")
                return _snapshot(connection, portfolio, state, managed_open_pnl)

        active = [row for row in rows if row["status"] in ACTIVE_ORDER_STATES]
        order_limit = max(1, min(5, int(os.getenv("DEMO_MAX_ORDERS_PER_RUN", "5"))))
        position_limit = max(1, min(5, int(os.getenv("DEMO_MAX_OPEN_POSITIONS", "5"))))
        max_order = max(10.0, min(100.0, float(os.getenv("DEMO_MAX_ORDER_USD", "50"))))
        max_exposure = max_order * max(1, min(5, int(os.getenv("DEMO_MAX_OPEN_POSITIONS", "5"))))
        opens = [row for row in rows if row["action"] == "open"]
        open_notional = sum(
            _float(row["amount_usd"]) for row in active
            if row["action"] == "open"
        )
        current_positions = _managed_positions(portfolio)
        held_instruments = {
            str(_id(position, "instrumentID", "instrumentId"))
            for position in (client.get("positions") or [])
        }
        available_cash = max(0.0, risk["cash_usd"])
        quotes_by_symbol = {quote["symbol"]: quote for quote in quotes}

        if len(opens) < order_limit and len(active) < position_limit:
            for symbol, meta in mapping.items():
                previous = connection.execute(
                    "SELECT id FROM demo_bot_orders WHERE symbol=? LIMIT 1", (symbol,)
                ).fetchone()
                instrument_id = int(meta["instrument_id"])
                quote = quotes_by_symbol.get(symbol) or {}
                if previous or str(instrument_id) in held_instruments:
                    continue
                # Unknown market flags are not safe to trade; require explicit true values.
                if not _flag(meta.get("is_currently_tradable")):
                    continue
                if not _flag(meta.get("is_buy_enabled")):
                    continue
                if not _flag(meta.get("is_open")):
                    continue
                signal_row = connection.execute(
                    "SELECT payload FROM market_analysis WHERE symbol=? AND status='ok'",
                    (symbol,),
                ).fetchone()
                if not signal_row:
                    continue
                try:
                    analysis = json.loads(signal_row["payload"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if analysis.get("action") != "BUY CANDIDATE":
                    continue
                price = _float(quote.get("ask") or quote.get("last"))
                stop_gap = _float(analysis.get("stop_gap_pct"))
                if price <= 0 or stop_gap <= 0 or stop_gap > 8:
                    continue
                requested = _float(analysis.get("suggested_size_usd"))
                amount = min(
                    requested, max_order, available_cash * 0.1,
                    max(0.0, max_exposure - open_notional),
                )
                if amount < 10:
                    continue
                stop_rate = price * (1 - stop_gap / 100)
                target_rate = price * (1 + 2 * stop_gap / 100)
                request_id = str(uuid.uuid4())
                created = now.isoformat()
                cursor = connection.execute(
                    """INSERT INTO demo_bot_orders
                       (symbol,instrument_id,request_id,action,status,amount_usd,
                        entry_price,stop_rate,target_rate,created_at,updated_at,message)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (symbol, instrument_id, request_id, "open", "SUBMITTING",
                     amount, price, stop_rate, target_rate, created, created,
                     "Long-only demo entry; 1x leverage"),
                )
                connection.commit()
                try:
                    result = feed.place_demo_order({
                        "action": "open",
                        "transaction": "buy",
                        "instrumentId": instrument_id,
                        "orderType": "mkt",
                        "amount": round(amount, 2),
                        "orderCurrency": "usd",
                        "leverage": 1,
                        "stopLossType": "fixed",
                        "stopLossRate": round(stop_rate, 8),
                        "takeProfitRate": round(target_rate, 8),
                    }, request_id)
                    _save_order(
                        connection, cursor.lastrowid, "PENDING",
                        order_id=_id(result, "orderId", "orderID"),
                        message="Submitted to eToro demo; awaiting final order status",
                    )
                    log("INFO", f"Submitted demo BUY for {symbol}, amount {amount:.2f} USD")
                except Exception as error:
                    _save_order(
                        connection, cursor.lastrowid, "UNKNOWN",
                        message=f"Submission outcome unknown: {type(error).__name__}: {error}",
                    )
                    log("ERROR", f"Demo order outcome unknown for {symbol}: {type(error).__name__}")
                break

    state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()
    rows = connection.execute("SELECT * FROM demo_bot_orders ORDER BY id").fetchall()
    managed_open_pnl = sum(
        _float(row["pnl"]) for row in rows if row["status"] in {"OPEN", "CLOSING"}
    )
    return _snapshot(connection, portfolio, state, managed_open_pnl)


def _snapshot(connection, portfolio, state, bot_pnl):
    rows = connection.execute(
        "SELECT symbol,action,status,amount_usd,entry_price,stop_rate,target_rate,position_id,pnl,message,created_at "
        "FROM demo_bot_orders ORDER BY id DESC"
    ).fetchall()
    client = (portfolio or {}).get("clientPortfolio") or {}
    current_unrealized = _float(client.get("unrealizedPnL", client.get("unrealisedPnL")), 0.0)
    current_realized = _float(client.get("closedPositionsNetProfit"), state["last_realized_pnl"])
    return {
        "status": state["status"],
        "started_at": state["started_at"],
        "ends_at": state["ends_at"],
        "stop_reason": state["stop_reason"],
        "account_unrealized_change": current_unrealized - _float(state["start_unrealized_pnl"]),
        "account_realized_change": current_realized - _float(state["start_realized_pnl"]),
        "bot_open_pnl": bot_pnl,
        "orders": [dict(row) for row in rows],
        "message": state["stop_reason"] or "Demo-only automatic trading is active.",
    }
