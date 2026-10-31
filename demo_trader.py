import json
import os
import uuid
from datetime import datetime, timedelta, timezone

from analysis import portfolio_risk
from bollinger_rsi import STRATEGY as BB_STRATEGY, plan_bollinger_entry
from data_quality import quote_problem, market_problem


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


def _save_order(connection, row_id, status, **values):
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
    args.append(row_id)
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



def _execution_check(connection, feed, log):
    token = os.getenv("DEMO_EXECUTION_CHECK_ID", "").strip()
    if not token:
        return
    request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "etoro-demo-check:" + token))
    connection.execute(
        "CREATE TABLE IF NOT EXISTS demo_execution_checks "
        "(token TEXT PRIMARY KEY, bot_order_id INTEGER NOT NULL)"
    )
    prior = connection.execute(
        "SELECT o.* FROM demo_execution_checks c JOIN demo_bot_orders o "
        "ON o.id=c.bot_order_id WHERE c.token=?", (token,)
    ).fetchone()
    if prior:
        print("DEMO_EXECUTION_CHECK " + json.dumps({
            "status": prior["status"], "symbol": prior["symbol"],
            "amount_usd": prior["amount_usd"], "order_id": prior["order_id"],
            "position_id": prior["position_id"], "message": prior["message"],
        }), flush=True)
        return
    # Fixed, explicitly requested execution check; independent of strategy signals.
    mapping = feed.resolve_symbols(["SOL"])
    meta = mapping.get("SOL") or {}
    if meta.get("instrument_id") != 100063:
        raise RuntimeError("Execution check could not verify SOL identity")
    quotes = feed.fetch_rates({"SOL": meta})
    price = _float(quotes[0].get("ask"))
    bid = _float(quotes[0].get("bid"))
    problem = market_problem(meta) or quote_problem(quotes[0])
    if problem:
        raise RuntimeError("Execution check blocked: " + problem)
    if price <= 0 or bid <= 0 or (price - bid) / price > 0.02:
        raise RuntimeError("Execution check requires a usable SOL quote")
    fresh = feed.fetch_portfolio("demo")
    cash = _float((fresh.get("clientPortfolio") or {}).get("credit"))
    if cash < 10:
        raise RuntimeError("Execution check requires 10 USD demo cash")
    stamp = _now().isoformat()
    cursor = connection.execute(
        """INSERT INTO demo_bot_orders
           (symbol,instrument_id,request_id,action,status,amount_usd,
            entry_price,stop_rate,target_rate,created_at,updated_at,message)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("SOL", 100063, request_id, "open", "SUBMITTING", 10.0,
         price, price * 0.95, price * 1.10, stamp, stamp,
         "User-requested 10 USD demo execution check; not a strategy signal"),
    )
    connection.execute(
        "UPDATE demo_bot_orders SET run_key=(SELECT run_key FROM demo_bot_state WHERE id=1), "
        "strategy='execution_check' WHERE id=?", (cursor.lastrowid,)
    )
    connection.execute(
        "INSERT INTO demo_execution_checks(token,bot_order_id) VALUES(?,?)",
        (token, cursor.lastrowid),
    )
    connection.commit()
    try:
        result = feed.place_demo_order({
            "action": "open", "transaction": "buy", "instrumentId": 100063,
            "orderType": "mkt", "amount": 10.0, "orderCurrency": "usd",
            "leverage": 1, "stopLossType": "fixed",
            "stopLossRate": round(price * 0.95, 8),
            "takeProfitRate": round(price * 1.10, 8),
        }, request_id)
        order_id = _id(result, "orderId", "orderID")
        _save_order(connection, cursor.lastrowid, "PENDING", order_id=order_id,
                    message="10 USD demo execution check submitted; awaiting broker fill")
        print("DEMO_EXECUTION_CHECK " + json.dumps({
            "status": "PENDING", "symbol": "SOL", "amount_usd": 10,
            "order_id": order_id,
        }), flush=True)
    except Exception as error:
        _save_order(connection, cursor.lastrowid, "UNKNOWN",
                    message=f"Execution check outcome unknown: {type(error).__name__}: {error}")
        print("DEMO_EXECUTION_CHECK " + json.dumps({
            "status": "UNKNOWN", "error_type": type(error).__name__,
            "http_status": getattr(getattr(error, "response", None), "status_code", None),
        }), flush=True)
        # Persisted request id prevents automatic resubmission after any ambiguity.



def _select_strategy(analysis):
    bollinger = analysis.get("bollinger_rsi") or {}
    if bollinger.get("action") == "BUY CANDIDATE":
        return BB_STRATEGY, bollinger
    return "trend", analysis


def _evaluate_entry(feed, symbol, meta, quote, analysis):
    """Refresh dynamic market flags for every buy candidate; never trust cached flags."""
    if analysis.get("action") != "BUY CANDIDATE":
        return "Strategy: " + str(analysis.get("action") or "analysis unavailable"), {}
    try:
        fresh = feed.resolve_symbols([symbol]).get(symbol)
    except Exception as error:
        return "Market status refresh failed: " + type(error).__name__, {}
    if not fresh or fresh.get("instrument_id") != meta.get("instrument_id"):
        return "Market status refresh returned no matching instrument", {}
    now = _now()
    reason = market_problem(fresh, now) or quote_problem(quote, now)
    return reason, fresh


def run_demo_cycle(connection, feed, mapping, quotes, portfolio, log):
    enabled = os.getenv("DEMO_AUTOTRADE_ENABLED", "false").strip().lower() in {"1", "true", "yes"}
    mode = os.getenv("ETORO_TRADING_MODE", "demo").strip().lower()
    if not enabled:
        return {"status": "DISABLED", "message": "Set DEMO_AUTOTRADE_ENABLED=true to enable demo execution."}
    if mode != "demo":
        return {"status": "BLOCKED", "message": "Order execution is hard-locked to demo mode."}

    if os.getenv("DEMO_EXECUTION_CHECK_ID", "").strip():
        portfolio = feed.fetch_portfolio("demo")
    client = (portfolio or {}).get("clientPortfolio") or {}
    if not client:
        return {"status": "WAITING", "message": "Waiting for a readable eToro demo portfolio."}

    columns = {row["name"] for row in connection.execute("PRAGMA table_info(demo_bot_state)")}
    if "cash_budget_usd" not in columns:
        connection.execute("ALTER TABLE demo_bot_state ADD COLUMN cash_budget_usd REAL")
        connection.commit()

    risk = portfolio_risk(portfolio, {
        str(meta.get("instrument_id")): meta.get("asset_class", "Unclassified")
        for meta in mapping.values()
    })
    now = _now()
    requested_run_key = os.getenv("DEMO_RUN_ID", "").strip()
    hours = max(1, min(12, int(os.getenv("DEMO_RUN_HOURS", "8"))))
    state = connection.execute(
        "SELECT * FROM demo_bot_state WHERE id=1"
    ).fetchone()
    if state is not None and requested_run_key and requested_run_key != state["run_key"]:
        active_count = connection.execute(
            "SELECT COUNT(*) AS n FROM demo_bot_orders WHERE status IN "
            "('SUBMITTING','UNKNOWN','PENDING','OPEN','CLOSING','CANCELLING')"
        ).fetchone()["n"]
        if active_count:
            return {"status": "BLOCKED", "message": "New run blocked until prior bot orders are reconciled."}
        connection.execute(
            """UPDATE demo_bot_state SET started_at=?,ends_at=?,status='RUNNING',
               start_unrealized_pnl=?,last_unrealized_pnl=?,start_realized_pnl=?,
               last_realized_pnl=?,stop_reason='',cash_budget_usd=?,run_key=? WHERE id=1""",
            (now.isoformat(), (now+timedelta(hours=hours)).isoformat(),
             _float(client.get("unrealizedPnL", client.get("unrealisedPnL"))),
             _float(client.get("unrealizedPnL", client.get("unrealisedPnL"))),
             _float(client.get("closedPositionsNetProfit")), _float(client.get("closedPositionsNetProfit")),
             max(0.0, _float(risk.get("cash_usd"))), requested_run_key),
        )
        connection.commit()
        state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()
    if state is None:
        started = now.isoformat()
        ends = (now + timedelta(hours=hours)).isoformat()
        start_unrealized = _float(client.get("unrealizedPnL", client.get("unrealisedPnL")), 0.0)
        start_realized = _float(client.get("closedPositionsNetProfit"), 0.0)
        start_cash_budget = max(0.0, _float(risk.get("cash_usd"), 0.0))
        connection.execute(
            """INSERT INTO demo_bot_state
               (id,started_at,ends_at,status,start_unrealized_pnl,last_unrealized_pnl,
                start_realized_pnl,last_realized_pnl,stop_reason,cash_budget_usd,run_key)
               VALUES(1,?,?,?,?,?,?,?,?,?,?)""",
            (started, ends, "RUNNING", start_unrealized, start_unrealized,
             start_realized, start_realized, "", start_cash_budget, requested_run_key or "legacy"),
        )
        connection.commit()
        state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()

    run_key = state["run_key"]
    current_unrealized = _float(client.get("unrealizedPnL", client.get("unrealisedPnL")), 0.0)
    current_realized = _float(client.get("closedPositionsNetProfit"), state["last_realized_pnl"])
    connection.execute(
        """UPDATE demo_bot_state SET last_unrealized_pnl=?,last_realized_pnl=? WHERE id=1""",
        (current_unrealized, current_realized),
    )
    connection.commit()

    _sync_orders(connection, feed, portfolio, log)
    state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()
    rows = connection.execute("SELECT * FROM demo_bot_orders WHERE run_key=? ORDER BY id", (run_key,)).fetchall()
    available_cash = max(0.0, _float(risk.get("cash_usd"), 0.0))
    budget_locked = any(
        row["action"] == "open"
        and row["status"] not in {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED"}
        for row in rows
    )
    if not budget_locked:
        cash_budget = available_cash
        connection.execute(
            "UPDATE demo_bot_state SET cash_budget_usd=? WHERE id=1",
            (cash_budget,),
        )
        connection.commit()
        state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()
    else:
        cash_budget = max(0.0, _float(state["cash_budget_usd"], available_cash))
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

    entry_decisions = []
    if state["status"] == "RUNNING":
        if os.getenv("DEMO_EXECUTION_CHECK_ID", "").strip():
            _execution_check(connection, feed, log)
            return _snapshot(connection, portfolio, state, managed_open_pnl)
        # A deteriorating signal closes only positions opened by this demo bot.
        signal_by_symbol = {}
        for row in connection.execute("SELECT symbol,payload FROM market_analysis WHERE status='ok'"):
            try:
                signal_by_symbol[row["symbol"]] = json.loads(row["payload"]).get("action")
            except (TypeError, json.JSONDecodeError):
                continue
        for row in rows:
            if row["status"] == "OPEN" and row["strategy"] == "trend" and signal_by_symbol.get(row["symbol"]) == "REDUCE / AVOID":
                _close_order(connection, feed, row, log, "Signal changed to Reduce / Avoid")
                return _snapshot(connection, portfolio, state, managed_open_pnl)

        active = [row for row in rows if row["status"] in ACTIVE_ORDER_STATES]
        order_limit = max(1, min(5, int(os.getenv("DEMO_MAX_ORDERS_PER_RUN", "5"))))
        position_limit = max(1, min(5, int(os.getenv("DEMO_MAX_OPEN_POSITIONS", "5"))))
        max_order = max(10.0, cash_budget)
        max_exposure = cash_budget
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
        quotes_by_symbol = {quote["symbol"]: quote for quote in quotes}

        if len(opens) < order_limit and len(active) < position_limit:
            for symbol, meta in mapping.items():
                previous = connection.execute(
                    "SELECT id FROM demo_bot_orders WHERE symbol=? AND run_key=? LIMIT 1", (symbol, run_key)
                ).fetchone()
                instrument_id = int(meta["instrument_id"])
                quote = quotes_by_symbol.get(symbol) or {}
                if previous or str(instrument_id) in held_instruments:
                    entry_decisions.append({"Symbol": symbol, "Signal": "Not checked",
                                            "Decision": "Previously attempted" if previous else "Already held"})
                    continue
                signal_row = connection.execute(
                    "SELECT payload FROM market_analysis WHERE symbol=? AND status='ok'",
                    (symbol,),
                ).fetchone()
                try:
                    analysis = json.loads(signal_row["payload"]) if signal_row else {}
                except (TypeError, json.JSONDecodeError):
                    analysis = {}
                trend_action = analysis.get("action", "Unavailable")
                bb_action = (analysis.get("bollinger_rsi") or {}).get("action", "Unavailable")
                strategy, analysis = _select_strategy(analysis)
                reason, fresh_meta = _evaluate_entry(feed, symbol, meta, quote, analysis)
                decision = {
                    "Symbol": symbol, "Strategy": strategy,
                    "Signal": analysis.get("action", "Unavailable"),
                    "Trend signal": trend_action, "Bollinger RSI signal": bb_action,
                    "Decision": reason or "Data checks passed",
                    "Broker timestamp": quote.get("quote_ts"),
                    "Fetched at": quote.get("fetched_at"),
                    "Status checked": fresh_meta.get("status_checked_at"),
                    "Market open": fresh_meta.get("is_open"),
                    "Open status source": fresh_meta.get("open_status_source", "unavailable"),
                    "Buying enabled": fresh_meta.get("is_buy_enabled"),
                    "Tradable": fresh_meta.get("is_currently_tradable"),
                }
                entry_decisions.append(decision)
                if reason:
                    continue
                price = _float(quote.get("ask") or quote.get("last"))
                if strategy == BB_STRATEGY:
                    plan, reason = plan_bollinger_entry(
                        analysis, price, risk.get("estimated_account_value_usd", 0), _now()
                    )
                    if reason:
                        decision["Decision"] = reason
                        continue
                    analysis = {**analysis, **plan}
                stop_gap = _float(analysis.get("stop_gap_pct"))
                if price <= 0 or stop_gap <= 0 or stop_gap > 8:
                    decision["Decision"] = "Risk sizing: invalid price or stop gap outside limits"
                    continue
                requested = _float(analysis.get("suggested_size_usd"))
                amount = min(
                    requested, max_order, available_cash,
                    max(0.0, max_exposure - open_notional),
                )
                if amount < 10:
                    decision["Decision"] = "Risk sizing: cash or remaining budget below minimum"
                    continue
                stop_rate = analysis["stop_rate"] if strategy == BB_STRATEGY else price * (1 - stop_gap / 100)
                target_rate = analysis["target_rate"] if strategy == BB_STRATEGY else price * (1 + 2 * stop_gap / 100)
                request_id = str(uuid.uuid4())
                created = now.isoformat()
                cursor = connection.execute(
                    """INSERT INTO demo_bot_orders
                       (symbol,instrument_id,request_id,action,status,amount_usd,
                        entry_price,stop_rate,target_rate,created_at,updated_at,message,strategy,signal_candle,run_key)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (symbol, instrument_id, request_id, "open", "SUBMITTING",
                     amount, price, stop_rate, target_rate, created, created,
                     f"Long-only demo entry; 1x leverage; {strategy}", strategy,
                     analysis.get("signal_candle") if strategy == BB_STRATEGY else None, run_key),
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
                    decision["Decision"] = "Demo order submitted"
                    log("INFO", f"Submitted demo BUY for {symbol}, amount {amount:.2f} USD, strategy {strategy}")
                except Exception as error:
                    _save_order(
                        connection, cursor.lastrowid, "UNKNOWN",
                        message=f"Submission outcome unknown: {type(error).__name__}: {error}",
                    )
                    decision["Decision"] = "Submission outcome unknown; automatic retry blocked"
                    log("ERROR", f"Demo order outcome unknown for {symbol}: {type(error).__name__}")
                break
        else:
            entry_decisions.append({"Symbol": "All", "Signal": "Not checked",
                                    "Decision": "Run entry limit or open position limit reached"})

    state = connection.execute("SELECT * FROM demo_bot_state WHERE id=1").fetchone()
    rows = connection.execute("SELECT * FROM demo_bot_orders WHERE run_key=? ORDER BY id", (run_key,)).fetchall()
    managed_open_pnl = sum(
        _float(row["pnl"]) for row in rows if row["status"] in {"OPEN", "CLOSING"}
    )
    return _snapshot(connection, portfolio, state, managed_open_pnl, entry_decisions)


def _snapshot(connection, portfolio, state, bot_pnl, decisions=None):
    rows = connection.execute(
        "SELECT symbol,run_key,strategy,signal_candle,action,status,amount_usd,entry_price,stop_rate,target_rate,position_id,pnl,message,created_at "
        "FROM demo_bot_orders ORDER BY id DESC"
    ).fetchall()
    client = (portfolio or {}).get("clientPortfolio") or {}
    current_unrealized = _float(client.get("unrealizedPnL", client.get("unrealisedPnL")), 0.0)
    current_realized = _float(client.get("closedPositionsNetProfit"), state["last_realized_pnl"])
    decisions = decisions or []
    counts = {}
    for decision in decisions:
        reason = decision["Decision"]
        counts[reason] = counts.get(reason, 0) + 1
    print("DEMO_BOT_STATUS " + json.dumps({
        "status": state["status"], "run_key": state["run_key"],
        "started_at": state["started_at"], "ends_at": state["ends_at"],
        "bot_open_pnl": bot_pnl, "decision_counts": counts,
        "candidate_checks": [d for d in decisions if d.get("Signal") == "BUY CANDIDATE"],
        "orders": [{"symbol": r["symbol"], "strategy": r["strategy"], "run_key": r["run_key"], "status": r["status"]} for r in rows],
    }), flush=True)
    return {
        "entry_decisions": decisions,
        "run_key": state["run_key"],
        "status": state["status"],
        "started_at": state["started_at"],
        "ends_at": state["ends_at"],
        "stop_reason": state["stop_reason"],
        "account_unrealized_change": current_unrealized - _float(state["start_unrealized_pnl"]),
        "account_realized_change": current_realized - _float(state["start_realized_pnl"]),
        "bot_open_pnl": bot_pnl,
        "cash_budget_usd": max(0.0, _float(state["cash_budget_usd"], 0.0)),
        "bot_reserved_cash_usd": sum(
            _float(row["amount_usd"]) for row in rows
            if row["action"] == "open" and row["status"] in ACTIVE_ORDER_STATES and row["run_key"] == state["run_key"]
        ),
        "orders": [dict(row) for row in rows],
        "message": state["stop_reason"] or "Demo-only automatic trading is active.",
    }
