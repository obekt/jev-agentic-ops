#!/usr/bin/env python3
"""Alpaca crypto scalping pilot — Jev (System One) decides direction, code acts.

Modes:
  manage   : enforce stops/targets/time-stops on open positions (deterministic)
  decide   : gather market state -> Jev typed questions -> gates -> open position
  cycle    : manage + decide (what cron runs)
  status   : print account/positions/orders summary

Risk rules live in CODE, not Jev. Jev only answers typed questions.
Kill switch: touch ~/.hermes/workspace/alpaca/STOP  -> manage still protects, decide halts.
"""
import json, os, sys, time, urllib.request, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(HERE, "alpaca_jev_log.jsonl")
STOP = os.path.join(HERE, "STOP")

def load_creds(path):
    with open(os.path.expanduser(path)) as f:
        return json.load(f)

AC = load_creds("~/.config/alpaca/credentials.json")
TS = load_creds("~/.config/typesafe/credentials.json")

PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD"]
STOCKS = ["TSLA", "NVDA", "AMD", "HOOD", "MU", "PLTR", "SMCI", "ARM", "SDS", "SQQQ"]
STOCK_STAKE = 22.0        # 1/day, settled-cash constrained
STOCK_MAX_PER_DAY = 1
STOCK_STOP_PCT = 1.5
STOCK_TARGET_PCT = 2.5
STOCK_TIME_STOP_MIN = 240
STOCK_MAX_SPREAD_PCT = 0.35
STOCK_HOURS_UTC = (13, 20)   # 9:30-16 ET ~ 13:30-20:00 UTC; coarse gate 13-20
STAKE_USD = 18.0          # per crypto position
MAX_OPEN = 2              # simultaneous positions
STOP_PCT = 1.0            # hard stop-loss %
TARGET_PCT = 1.5          # take-profit %
TIME_STOP_MIN = 90        # close if losing after this many minutes
CONF_BAR = 0.65           # Jev confidence gate
NOUL_BAR = 0.62           # Jev p(up) gate
MAX_SPREAD_PCT = 0.15     # skip pair if book spread too wide

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

def logrec(rec):
    rec["ts"] = now_iso()
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")

def api(method, url, body=None, base=None):
    base = base or AC["base"]
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + url, data=data, method=method,
        headers={"Content-Type": "application/json",
                "APCA-API-KEY-ID": AC["key_id"],
                "APCA-API-SECRET-KEY": AC["secret"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        t = r.read().decode()
        return json.loads(t) if t else {}

def jev(state, questions, timeout=30):
    req = urllib.request.Request("https://api.typesafe.ai/v1/systemone",
        data=json.dumps({"state": state, "model": "jev-latest", "questions": questions}).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {TS['api_key']}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def get_account():
    return api("GET", "/v2/account")

def get_positions():
    return api("GET", "/v2/positions")

def get_open_orders():
    return api("GET", "/v2/orders?status=open")

def get_snapshots(pairs):
    qs = ",".join(pairs)
    d = api("GET", f"/v1beta3/crypto/us/snapshots?symbols={qs}", base=AC["data"])
    return d.get("snapshots", {})

def get_min_sizes(pairs):
    out = {}
    d = api("GET", "/v2/assets?asset_class=crypto&status=active")
    want = {p.replace("/", ""): p for p in pairs}
    for a in d:
        if a["symbol"] in want and a.get("tradable"):
            out[want[a["symbol"]]] = float(a.get("min_order_size", 0) or 0)
    return out

def spread_pct(snap):
    q = snap.get("latestQuote", {})
    bid, ask = float(q.get("bp", 0)), float(q.get("ap", 0))
    return (ask - bid) / ask * 100 if ask > 0 else 99.0

# ---------------- manage ----------------

def manage():
    """Deterministic exits: stop-loss, take-profit, time-stop. Runs every cycle."""
    actions = []
    try:
        pos = get_positions()
    except Exception as e:
        logrec({"event": "manage_err", "err": str(e)})
        return actions
    meta = {}
    try:
        meta = json.load(open(os.path.join(HERE, "positions.json")))
    except Exception:
        meta = {}
    for p in pos:
        sym = p["symbol"]
        if sym.endswith("USDCUSD") or sym == "USDCUSD":
            continue  # base currency
        is_stock = "/" not in sym
        stop_p = STOCK_STOP_PCT if is_stock else STOP_PCT
        tgt_p = STOCK_TARGET_PCT if is_stock else TARGET_PCT
        tstop = STOCK_TIME_STOP_MIN if is_stock else TIME_STOP_MIN
        upc = float(p.get("unrealized_plpc", 0)) * 100
        opened = meta.get(sym, {}).get("opened_ts")
        age_min = None
        if opened:
            t0 = datetime.datetime.fromisoformat(opened)
            age_min = (datetime.datetime.now(datetime.timezone.utc) - t0).total_seconds() / 60
        why = None
        if upc <= -stop_p:
            why = f"stop-loss {upc:.2f}%"
        elif upc >= tgt_p:
            why = f"take-profit {upc:.2f}%"
        elif is_stock and (datetime.datetime.now(datetime.timezone.utc).weekday() >= 5 or datetime.datetime.now(datetime.timezone.utc).hour >= 19):
            why = f"EOD flat {upc:.2f}% (no weekend/overnight hold)"
        elif age_min and age_min > tstop and upc < 0:
            why = f"time-stop {age_min:.0f}m losing {upc:.2f}%"
        if why:
            qty = p["qty_available"]
            tif = "ioc" if not is_stock else "day"  # IOC rejected outside market hours
            if float(qty) > 0:
                try:
                    o = api("POST", "/v2/orders", {
                        "symbol": sym, "qty": qty, "side": "sell",
                        "type": "market", "time_in_force": tif})
                    actions.append({"closed": sym, "reason": why, "order_id": o.get("id")})
                    logrec({"event": "close", "symbol": sym, "reason": why,
                            "upc": upc, "age_min": age_min, "order_id": o.get("id")})
                    meta.pop(sym, None)
                except Exception as e:
                    logrec({"event": "close_err", "symbol": sym, "err": str(e)})
    json.dump(meta, open(os.path.join(HERE, "positions.json"), "w"))
    return actions

# ---------------- decide ----------------

def decide():
    if os.path.exists(STOP):
        return {"halted": True}
    acct = get_account()
    cash = float(acct["cash"])
    pv = float(acct.get("portfolio_value", 0))
    pos = [p for p in get_positions() if "USDC" not in p["symbol"] and "/" in p["symbol"]]
    if len(pos) >= MAX_OPEN:
        return {"skip": f"max open {len(pos)}"}
    want_stake = min(STAKE_USD, max(cash * 0.95, 0))
    if want_stake < 12:
        return {"skip": f"insufficient cash {cash:.2f}"}
    snaps = get_snapshots(PAIRS)
    mins = get_min_sizes(PAIRS)
    pair_state = {}
    for pr in PAIRS:
        s = snaps.get(pr)
        if not s:
            continue
        q = s.get("latestQuote", {})
        bid, ask = float(q.get("bp", 0)), float(q.get("ap", 0))
        day = s.get("dailyBar", {})
        prev = s.get("prevDailyBar", {}) or {}
        mom = s.get("minuteBar", {})
        chg_day = ((day.get("c", 0) - prev.get("c", day.get("o", 1))) / prev.get("c", day.get("o", 1)) * 100) if prev else 0
        pair_state[pr] = {
            "bid": bid, "ask": ask, "spread_pct": round(spread_pct(s), 4),
            "day_chg_pct": round(chg_day, 2),
            "last_min_chg_pct": round((mom.get("c", bid) - mom.get("o", bid)) / max(mom.get("o", 1), 1e-9) * 100, 3) if mom.get("o") else 0,
            "day_high": day.get("h"), "day_low": day.get("l"),
            "pos_off_day_low_pct": round((bid - day.get("l", bid)) / max(day.get("l", 1), 1e-9) * 100, 2),
        }
    tradeable = {k: v for k, v in pair_state.items() if v["spread_pct"] <= MAX_SPREAD_PCT and k.replace("/", "") in {kk.replace("/", "") for kk in mins}}
    state = {
        "context": "Crypto scalping. I hold USD cash, trading spot crypto with market orders. Fee: 0.25% taker per side (0.5% round trip). My edge must beat that. I buy and sell within 30-90 minutes. Facts per pair: bid/ask, spread_pct, day_chg_pct (vs prev close), last_min_chg_pct (momentum last minute), pos_off_day_low_pct (how far above day's low).",
        "cash_usd": round(cash, 2),
        "portfolio_usd": round(pv, 2),
        "open_positions": [{"sym": p["symbol"], "upc_pct": round(float(p.get("unrealized_plpc", 0)) * 100, 2)} for p in pos],
        "pairs": tradeable,
        "rules": "A good scalp: strong short-term momentum, room under day high, tight spread. Do NOT buy if momentum is flat or falling.",
    }
    try:
        cat = json.load(open(os.path.join(HERE, "catalyst.json")))
        state["news_catalysts"] = cat.get("catalysts", [])
    except Exception:
        pass
    questions = {
        "p_up_btc": {"type": "noul", "instructions": "BTC/USD will rise at least 0.5% within the next 30 minutes"},
        "p_up_eth": {"type": "noul", "instructions": "ETH/USD will rise at least 0.5% within the next 30 minutes"},
        "p_up_sol": {"type": "noul", "instructions": "SOL/USD will rise at least 0.5% within the next 30 minutes"},
        "best_action": {"type": "choice",
                       "instructions": "Which single action maximizes expected profit net of 0.5% round-trip fees over the next 30-90 minutes",
                       "criteria": {
                           "buy_btc": "Open long BTC/USD now",
                           "buy_eth": "Open long ETH/USD now",
                           "buy_sol": "Open long SOL/USD now",
                           "no_trade": "No pair has a profitable scalp setup; stay in cash"}},
    }
    ans = jev(state, questions)
    a = ans["answers"]
    choice = a["best_action"]["choice"]
    conf = a["best_action"].get("confidence", 0)
    nouls = {"buy_btc": a["p_up_btc"]["noul"], "buy_eth": a["p_up_eth"]["noul"], "buy_sol": a["p_up_sol"]["noul"]}
    logrec({"event": "jev", "state": state, "answers": a, "usage": ans.get("usage")})
    rec = {"choice": choice, "conf": conf, "noul": nouls.get(choice)}
    if choice == "no_trade" or conf < CONF_BAR or nouls.get(choice, 0) < NOUL_BAR:
        rec["acted"] = False
        logrec({"event": "no_trade", "why": f"conf {conf:.2f} noul {nouls.get(choice)}", "choice": choice})
        return rec
    sym = {"buy_btc": "BTC/USD", "buy_eth": "ETH/USD", "buy_sol": "SOL/USD"}[choice]
    sp = tradeable.get(sym, {}).get("spread_pct", 99)
    if sp > MAX_SPREAD_PCT:
        rec["acted"] = False
        logrec({"event": "no_trade", "why": f"spread {sp} too wide", "choice": choice})
        return rec
    px = tradeable[sym]["ask"]
    qty = round(want_stake / px, 6)
    min_sz = mins.get(sym, 0)
    if qty < min_sz:
        rec["acted"] = False
        logrec({"event": "no_trade", "why": f"qty {qty} < min {min_sz}"})
        return rec
    o = api("POST", "/v2/orders", {"symbol": sym, "qty": str(qty), "side": "buy",
                                    "type": "market", "time_in_force": "ioc"})
    meta = {}
    try:
        meta = json.load(open(os.path.join(HERE, "positions.json")))
    except Exception:
        pass
    meta[sym] = {"opened_ts": now_iso(), "stake": want_stake, "qty": qty,
                 "entry_ref": px, "jev_conf": conf, "jev_noul": nouls.get(choice)}
    json.dump(meta, open(os.path.join(HERE, "positions.json"), "w"))
    rec["acted"] = True
    rec["stake"] = want_stake
    rec["symbol"] = sym
    rec["order_id"] = o.get("id")
    logrec({"event": "open", "symbol": sym, "qty": qty, "stake": want_stake,
            "order_id": o.get("id"), "conf": conf, "noul": nouls.get(choice)})
    return rec

# ---------------- decide stocks ----------------

def get_stock_snapshots(syms):
    qs = ",".join(syms)
    d = api("GET", f"/v2/stocks/snapshots?symbols={qs}", base=AC["data"])
    # stocks endpoint returns symbols at top level (no "snapshots" wrapper)
    return d.get("snapshots", d)

def decide_stocks():
    if os.path.exists(STOP):
        return {"halted": True}
    now = datetime.datetime.now(datetime.timezone.utc)
    if not (STOCK_HOURS_UTC[0] <= now.hour < STOCK_HOURS_UTC[1]) or now.weekday() >= 5:
        return {"skip": "outside market hours"}
    acct = get_account()
    settled = float(acct.get("settled_cash") or acct.get("buying_power") or 0)
    budget = min(STOCK_STAKE, settled)
    if budget < 15:
        return {"skip": f"settled cash {settled:.2f} too low"}
    meta = {}
    try:
        meta = json.load(open(os.path.join(HERE, "stock_day.json")))
    except Exception:
        meta = {}
    if meta.get("date") == now.date().isoformat() and meta.get("used", 0) >= STOCK_MAX_PER_DAY:
        return {"skip": "daily stock budget used"}
    snaps = get_stock_snapshots(STOCKS)
    stocks_state = {}
    for sym in STOCKS:
        s = snaps.get(sym)
        if not s:
            continue
        q = s.get("latestQuote", {})
        bid, ask = float(q.get("bp", 0) or 0), float(q.get("ap", 0) or 0)
        if bid <= 0 or ask <= 0:
            continue
        sp = (ask - bid) / ask * 100
        day = s.get("dailyBar", {}) or {}
        prev = s.get("prevDailyBar", {}) or {}
        mom = s.get("minuteBar", {}) or {}
        ref_prev = prev.get("c") or day.get("o") or bid
        stocks_state[sym] = {
            "bid": round(bid, 2), "ask": round(ask, 2), "spread_pct": round(sp, 4),
            "day_chg_pct": round((day.get("c", bid) - ref_prev) / max(ref_prev, 1e-9) * 100, 2),
            "last_min_chg_pct": round((mom.get("c", bid) - mom.get("o", bid)) / max(mom.get("o", 1e-9), 1e-9) * 100, 3) if mom.get("o") else 0,
            "day_high": day.get("h"), "day_low": day.get("l"),
            "pos_off_day_low_pct": round((bid - day.get("l", bid)) / max(day.get("l", 1), 1e-9) * 100, 2),
        }
    tradeable = {k: v for k, v in stocks_state.items() if v["spread_pct"] <= STOCK_MAX_SPREAD_PCT}
    if not tradeable:
        return {"skip": "all spreads too wide"}
    state = {
        "context": "US stock day trading (long only, buy now, sell within 2-4 hours same day). Stocks are commission-free; my costs are the bid/ask spread plus settlement limits. I pick the single best trending stock with momentum and room before end of day. NOTE: SDS and SQQQ are INVERSE index ETFs (SDS=-2x S&P500, SQQQ=-3x Nasdaq-100) — they RISE when the index falls; treat them as short-market positions, chosen only when a session decline is expected. Avoid inverses on flat/up days. Facts per stock: bid/ask, spread_pct, day_chg_pct vs prev close, last_min_chg_pct, day_high/low, pos_off_day_low_pct.",
        "budget_usd": round(budget, 2),
        "settled_cash": round(settled, 2),
        "time_utc": now.strftime("%H:%M"),
        "stocks": tradeable,
    }
    try:
        cat = json.load(open(os.path.join(HERE, "catalyst.json")))
        state["news_catalysts"] = cat.get("catalysts", [])
    except Exception:
        pass
    questions = {
        "best_stock": {"type": "choice",
                      "instructions": "Which single stock has the best expected intraday gain (>=1% in next 2-4 hours), or no_trade if none",
                      "criteria": {**{s: f"Buy {s} now for intraday momentum" for s in tradeable},
                                   "no_trade": "No stock here has a strong enough intraday setup"}},
    }
    ans = jev(state, questions)
    a = ans["answers"]["best_stock"]
    choice = a["choice"]
    conf = a.get("confidence", 0)
    logrec({"event": "jev_stock", "state": state, "answer": a, "usage": ans.get("usage")})
    rec = {"choice": choice, "conf": conf}
    if choice == "no_trade" or conf < CONF_BAR:
        rec["acted"] = False
        return rec
    sym = choice
    if sym not in tradeable:
        rec["acted"] = False
        rec["why"] = "choice not tradeable"
        return rec
    px = tradeable[sym]["ask"]
    qty_f = budget / px
    whole = int(budget // px)
    if whole >= 1:
        qty = str(whole)
    else:
        # fractional share sizing (Alpaca: up to 6 decimals, min ~$1 notional)
        qty = str(round(qty_f, 6))
    if qty_f < 0.0001:
        rec["acted"] = False
        rec["why"] = f"budget {budget:.2f} < price {px}"
        return rec
    o = api("POST", "/v2/orders", {"symbol": sym, "qty": qty, "side": "buy",
                                    "type": "market", "time_in_force": "day"})
    meta["date"] = now.date().isoformat()
    meta["used"] = meta.get("used", 0) + 1
    json.dump(meta, open(os.path.join(HERE, "stock_day.json"), "w"))
    pm = {}
    try:
        pm = json.load(open(os.path.join(HERE, "positions.json")))
    except Exception:
        pass
    pm[sym] = {"opened_ts": now.isoformat(), "stake": budget, "qty": qty,
               "entry_ref": px, "jev_conf": conf, "class": "stock"}
    json.dump(pm, open(os.path.join(HERE, "positions.json"), "w"))
    rec.update({"acted": True, "symbol": sym, "qty": qty, "stake": budget, "order_id": o.get("id")})
    logrec({"event": "open_stock", "symbol": sym, "qty": qty, "stake": budget,
            "order_id": o.get("id"), "conf": conf})
    return rec

# ---------------- status ----------------

def status():
    acct = get_account()
    out = {"cash": acct["cash"], "portfolio": acct.get("portfolio_value"),
           "buying_power": acct.get("buying_power")}
    out["positions"] = [{"sym": p["symbol"], "qty": p["qty"],
                         "upc%": round(float(p.get("unrealized_plpc", 0)) * 100, 3)}
                        for p in get_positions()]
    out["open_orders"] = [{"sym": o["symbol"], "side": o["side"], "type": o["type"],
                          "status": o["status"]} for o in get_open_orders()]
    return out

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    if mode == "manage":
        r = manage()
    elif mode == "decide":
        r = decide()
    elif mode == "decide_stocks":
        r = decide_stocks()
    elif mode == "cycle":
        m = manage()
        d = decide()
        ds = decide_stocks()
        lines = []
        for c in m:
            lines.append(f"CLOSED {c['closed']} — {c['reason']}")
        for x, lbl in ((d, "crypto"), (ds, "stock")):
            if x.get("acted"):
                lines.append(f"OPENED {lbl} {x.get('symbol', x.get('choice'))} ${x.get('stake','')} conf={x['conf']:.2f} order={x.get('order_id')}")
            elif x.get("halted"):
                lines.append("HALTED (STOP file) — manage still active")
            elif x.get("skip"):
                pass  # routine skip, stay quiet
            elif x.get("choice"):
                lines.append(f"no {lbl} trade: jev {x['choice']} conf {x.get('conf')}")
        s = status()
        lines.append(f"equity=${s['portfolio']} cash=${s['cash']} positions={s['positions']}")
        r = {"lines": lines}
    else:
        r = status()
    print(json.dumps(r, indent=1))
