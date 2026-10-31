from math import sqrt
import pandas as pd


def analyze_candles(candles, current_price=None, equity=0.0,
                    risk_per_trade_pct=0.5, max_position_pct=5.0):
    """Build an explainable trend signal from daily OHLC data."""
    rows = []
    for candle in candles or []:
        try:
            close = float(candle.get("close"))
            if close > 0:
                rows.append({
                    "date": candle.get("fromDate"),
                    "close": close,
                    "high": float(candle.get("high", close)),
                    "low": float(candle.get("low", close)),
                })
        except (TypeError, ValueError):
            continue
    frame = pd.DataFrame(rows)
    if not frame.empty and "date" in frame:
        frame = frame.drop_duplicates("date").sort_values("date")
    if len(frame) < 50:
        return {
            "action": "INSUFFICIENT DATA", "score": 0,
            "reasons": [f"Need 50 daily candles; received {len(frame)}"],
            "bars": len(frame), "suggested_size_usd": None,
        }

    close = frame["close"].astype(float)
    price = float(current_price or close.iloc[-1])
    sma20 = float(close.tail(20).mean())
    sma50 = float(close.tail(50).mean())
    momentum20 = float((close.iloc[-1] / close.iloc[-21] - 1) * 100)
    delta = close.diff()
    gains = delta.clip(lower=0).tail(14).mean()
    losses = -delta.clip(upper=0).tail(14).mean()
    rsi = 100.0 if losses == 0 else float(100 - (100 / (1 + gains / losses)))
    returns = close.pct_change().dropna()
    volatility = float(returns.tail(20).std() * sqrt(252) * 100) if len(returns) >= 20 else None
    peak = close.tail(90).cummax()
    drawdown = float(((close.tail(90) / peak) - 1).min() * 100)

    true_range = pd.concat([
        frame["high"] - frame["low"],
        (frame["high"] - close.shift()).abs(),
        (frame["low"] - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = float(true_range.tail(14).mean())
    stop_gap_pct = max((atr * 2 / price * 100) if price > 0 else 0, 2.0)

    score = 0
    reasons = []
    if price > sma20:
        score += 1
        reasons.append("Price is above its 20-day average")
    else:
        score -= 1
        reasons.append("Price is below its 20-day average")
    if sma20 > sma50:
        score += 2
        reasons.append("20-day trend is above the 50-day trend")
    else:
        score -= 2
        reasons.append("20-day trend is below the 50-day trend")
    if momentum20 > 0:
        score += 1
        reasons.append(f"20-day momentum is positive ({momentum20:.1f}%)")
    else:
        score -= 1
        reasons.append(f"20-day momentum is negative ({momentum20:.1f}%)")
    if 50 <= rsi <= 68:
        score += 1
        reasons.append(f"RSI is constructive ({rsi:.0f})")
    elif rsi >= 72:
        score -= 1
        reasons.append(f"RSI is elevated ({rsi:.0f})")
    elif rsi < 30:
        reasons.append(f"RSI is oversold ({rsi:.0f}); trend confirmation is still needed")
    else:
        reasons.append(f"RSI is neutral ({rsi:.0f})")

    if score >= 4:
        action = "BUY CANDIDATE"
    elif score <= -2:
        action = "REDUCE / AVOID"
    else:
        action = "HOLD / WATCH"

    suggested_size = None
    if action == "BUY CANDIDATE" and equity > 0 and stop_gap_pct > 0:
        risk_budget = equity * max(0.0, float(risk_per_trade_pct)) / 100
        position_cap = equity * max(0.0, float(max_position_pct)) / 100
        suggested_size = round(min(position_cap, risk_budget / (stop_gap_pct / 100)), 2)
        reasons.append(
            f"Risk-budget size estimate: {suggested_size:,.2f} USD "
            f"using {risk_per_trade_pct:g}% account risk and a {stop_gap_pct:.1f}% stop gap"
        )

    return {
        "action": action, "score": score, "reasons": reasons,
        "bars": int(len(frame)), "price": price, "sma20": sma20, "sma50": sma50,
        "rsi14": rsi, "momentum20_pct": momentum20,
        "annual_volatility_pct": volatility, "max_drawdown90_pct": drawdown,
        "atr14": atr, "stop_gap_pct": stop_gap_pct,
        "suggested_size_usd": suggested_size,
    }


def portfolio_risk(portfolio, instrument_classes=None):
    """Summarize concentration and leverage from eToro's portfolio snapshot."""
    instrument_classes = instrument_classes or {}
    client = (portfolio or {}).get("clientPortfolio") or {}
    positions = list(client.get("positions") or [])
    for mirror in client.get("mirrors") or []:
        positions.extend(mirror.get("positions") or [])

    rows = []
    class_capital = {}
    total_capital = 0.0
    gross_exposure = 0.0
    leveraged = 0
    for position in positions:
        try:
            amount = float(position.get("amount") or position.get("initialAmountInDollars") or 0)
            leverage = float(position.get("leverage") or 1)
            instrument_id = position.get("instrumentID", position.get("instrumentId"))
            category = instrument_classes.get(str(instrument_id), "Unclassified")
            if amount <= 0:
                continue
            total_capital += amount
            gross_exposure += amount * max(1.0, leverage)
            leveraged += int(leverage > 1)
            class_capital[category] = class_capital.get(category, 0.0) + amount
            rows.append({
                "instrument_id": instrument_id, "asset_class": category,
                "capital_usd": amount, "leverage": leverage,
                "direction": "Long" if position.get("isBuy", True) else "Short",
                "has_stop": (bool(position.get("stopLossRate")) if "isNoStopLoss" not in position else not bool(position.get("isNoStopLoss"))),
            })
        except (TypeError, ValueError):
            continue

    try:
        cash = float(client.get("credit") or 0)
    except (TypeError, ValueError):
        cash = 0.0
    equity_estimate = max(0.0, cash + total_capital)
    largest_position = max((row["capital_usd"] for row in rows), default=0.0)
    largest_weight = (largest_position / equity_estimate * 100) if equity_estimate else 0.0
    class_weights = {
        category: (amount / equity_estimate * 100) if equity_estimate else 0.0
        for category, amount in class_capital.items()
    }
    flags = []
    if not rows:
        flags.append("No open positions were returned by the portfolio endpoint.")
    if largest_weight > 20:
        flags.append(f"Largest position is about {largest_weight:.1f}% of estimated account value.")
    for category, weight in class_weights.items():
        if weight > 50:
            flags.append(f"{category} exposure is about {weight:.1f}% of estimated account value.")
    if leveraged:
        flags.append(f"{leveraged} position(s) use leverage; losses can move faster than invested capital.")
    if rows and any(not row["has_stop"] for row in rows):
        flags.append("At least one open position has no stop loss recorded.")
    if equity_estimate and cash / equity_estimate < 0.05:
        flags.append("Available cash is below 5% of the estimated account value.")

    return {
        "positions": rows, "position_count": len(rows),
        "cash_usd": cash, "invested_capital_usd": total_capital,
        "estimated_account_value_usd": equity_estimate,
        "estimated_gross_exposure_usd": gross_exposure,
        "leverage_multiple": (gross_exposure / equity_estimate) if equity_estimate else 0.0,
        "largest_position_pct": largest_weight,
        "asset_class_weights": class_weights, "flags": flags,
    }
