#!/usr/bin/env python3
"""jev_pilot.py — System One pilot for SpaceMolt.

Architecture (per TypeSafe "How to build with System One"):
  - CODE owns: deterministic facts (fuel math, tick gate, route computation),
    execution of mutations, logging.
  - JEV owns: ambiguous judgment calls as atomic typed questions:
      * strategy  (Choice) — what kind of operation next
      * threat    (Noul)   — nearby hostile risk
      * urgency   (Score)  — how pressed are fuel/mission clocks
  - Confidence-gated routing: act on Jev when confidence clears the bar,
    fall back to code heuristics when it doesn't.
Usage:
  python3 jev_pilot.py decide          # ask Jev, print decision, no mutate
  python3 jev_pilot.py act [n]        # decide + execute n actions (11s spaced)
  python3 jev_pilot.py loop           # run until gate closes / stop file
"""
import json, os, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sm_api as api

KEY = json.load(open(os.path.expanduser("~/.config/typesafe/credentials.json")))["api_key"]
TS_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
LOG = os.path.join(HERE, "jev_pilot_log.jsonl")
STOP = os.path.join(HERE, "jev_pilot.stop")
CONF_BAR = 0.45  # confidence below this => code fallback, not Jev

def jev(state, questions, timeout=30):
    req = urllib.request.Request(TS_ENDPOINT, data=json.dumps({
        "state": state, "model": "jev-latest", "questions": questions}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

def gather():
    sc = api.structured(api.query("spacemolt", "get_status"))
    if isinstance(sc, str): sc = json.loads(sc)
    return sc

def decide(sc):
    """One parallel Jev call: strategy + threat + urgency + mission-fit."""
    loc = sc.get("location", {})
    ship = sc.get("ship", {})
    player = sc.get("player", {})
    missions = sc.get("missions", {}).get("active", []) or []
    nearby = {
        "pirates": nearby_count(loc, "pirate"),
        "players": nearby_count(loc, "player"),
        "empire_npc": nearby_count(loc, "empire"),
    }
    state = {
        "game": "SpaceMolt. I pilot a cargo hauler. Current facts:",
        "system": loc.get("system_name"), "poi": loc.get("poi_name"),
        "poi_type": loc.get("poi_type"),
        "security": loc.get("security_status"),
        "nearby_threats": nearby,
        "fuel": f"{ship.get('fuel')}/{ship.get('fuel_capacity')}",
        "cargo_used": f"{ship.get('cargo_capacity',0)-0 and sc.get('ship',{}).get('cargo_used','?')}",
        "credits": player.get("credits"),
        "home_system": player.get("home_system"),
        "active_missions": [
            {"deliver": m["objectives"][0].get("item_name"),
             "to": m.get("issuing_system_name") or m["objectives"][0].get("system_name"),
             "have": m["objectives"][0].get("current"), "need": m["objectives"][0].get("required"),
             "ticks_left": m.get("expires_in_ticks"), "difficulty": m.get("difficulty")}
            for m in missions],
    }
    questions = {
        "threat": {"type": "noul",
                   "instructions": "Staying or acting in this system right now exposes the ship to significant combat risk"},
        "urgency": {"type": "score",
                     "instructions": "How time-critical is it to move toward resources or the mission destination",
                     "criteria": ["No hurry, plenty of margin",
                                  "Should get moving soon",
                                  "Must act now or lose the mission/run"]},
        "strategy": {"type": "choice",
                      "instructions": "Given the state, which single operation should the pilot run next",
                      "criteria": {
                          "deliver_mission": "Head to the active mission delivery destination and complete it",
                          "acquire_goods_then_deliver": "Fly/buy the mission goods at a market first, then deliver",
                          "mine_locally": "Mine resources at the current POI and sell them",
                          "return_home": "Head back to home base to resupply and regroup",
                          "freight_boarding": "Go to nearest station and pick up profitable freight contracts"}},
        "mine_fit": {"type": "noul",
                     "instructions": "The current POI is a good place to mine (resource-rich belt, ice field, or asteroid)"},
    }
    ans = jev(state, questions)
    return state, ans

def nearby_count(loc, kind):
    if kind == "pirate": return loc.get("nearby_pirate_count", 0)
    if kind == "player": return loc.get("nearby_player_count", 0)
    return loc.get("nearby_empire_npc_count", 0)

def logrec(rec):
    rec["ts"] = time.time()
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")

def mission_ready(sc):
    """True if we carry enough of every active mission's deliver item."""
    cargo = {c["item_id"]: c.get("quantity", 0) for c in sc.get("cargo", [])}
    missions = sc.get("missions", {}).get("active", []) or []
    for m in missions:
        for o in m.get("objectives", []):
            if o.get("type") == "deliver_item" or o.get("item_id"):
                have = int(cargo.get(o.get("item_id"), 0)) + int(o.get("current", 0) or 0)
                if o.get("required") and have < int(o["required"]):
                    return False
    return bool(missions)

def pick_action(sc, ans):
    """Map Jev answers to a concrete action with confidence gate."""
    strat = ans["answers"]["strategy"]
    mine_fit = ans["answers"]["mine_fit"]["noul"]
    threat = ans["answers"]["threat"]["noul"]
    conf = strat.get("confidence", 0)
    choice = strat["choice"]
    if threat > 0.7:
        return "flee_to_home", f"threat noul {threat:.2f} > 0.7", conf
    # CODE override: goods in hold -> deliver, regardless of market-leaning strategy
    if mission_ready(sc) and choice in ("acquire_goods_then_deliver", "deliver_mission", "freight_boarding"):
        return "jump_mission", "cargo satisfies mission -> deliver", conf
    if conf < CONF_BAR:
        # fallback heuristic in code: mine if POI fits, else head home
        if mine_fit > 0.5:
            return "mine", f"jev conf {conf:.2f} low -> mine (fit {mine_fit:.2f})", conf
        return "return_home", f"jev conf {conf:.2f} low -> home", conf
    return {"deliver_mission": "jump_mission",
            "acquire_goods_then_deliver": "jump_market",
            "mine_locally": "mine",
            "return_home": "return_home",
            "freight_boarding": "jump_station"}[choice], f"jev {choice} conf {conf:.2f}", conf

GATE = os.path.join(HERE, "play_gate.json")  # shared 10s tick gate with play.py

def gated(mutate_fn):
    """Serialize mutations across processes via play_gate.json."""
    gate = {}
    if os.path.exists(GATE):
        try: gate = json.load(open(GATE))
        except Exception: pass
    wait = 11 - (time.time() - gate.get("last", 0))
    if wait > 0:
        time.sleep(wait)
    res = mutate_fn()
    json.dump({"last": time.time()}, open(GATE, "w"))
    return res

def ensure_docked(target_base=None):
    """Travel to the system's station POI if needed, then dock. Returns fresh state."""
    sc = gather()
    loc = sc["location"]
    if loc.get("docked_at"):
        return sc
    sysid = loc["system_id"]
    s2 = api.structured(api.query("spacemolt", "get_system", {"system_id": sysid}))
    if isinstance(s2, str):
        s2 = json.loads(s2)
    stations = [p for p in s2["system"]["pois"] if p.get("type") == "station"]
    if not stations:
        raise RuntimeError(f"no station POI in {sysid}")
    tgt = target_base or stations[0]["id"]
    if loc["poi_id"] != tgt:
        gated(lambda: api.mutate("spacemolt", "travel", {"target_poi": tgt}))
        time.sleep(12)
    gated(lambda: api.mutate("spacemolt", "dock", {}))
    return gather()

def arrive_actions(sc, action):
    """We are at the target system — travel to station, dock, trade, complete."""
    if action == "jump_mission":
        sc = ensure_docked("traders_rest_resort_station")
        missions = sc.get("missions", {}).get("active", []) or []
        out = {"docked": True}
        if missions:
            mid = missions[0].get("mission_id")
            cm = gated(lambda: api.mutate("spacemolt", "complete_mission", {"mission_id": mid}))
            out["complete"] = str(api.structured(cm))[:250]
        return out
    sc = ensure_docked()
    out = {"docked": True}
    missions = sc.get("missions", {}).get("active", []) or []
    if missions:
        obj = missions[0]["objectives"][0]
        need = int(obj.get("required", 0)) - int(obj.get("current", 0))
        cargo = {c["item_id"]: c.get("quantity", 0) for c in sc.get("cargo", [])}
        need = max(0, need - cargo.get(obj["item_id"], 0))
        if need > 0:
            try:
                buy = gated(lambda: api.mutate("spacemolt_market", "create_buy_order",
                                             {"item_id": obj["item_id"], "quantity": need, "price_each": 120}))
                out["buy_order"] = str(api.structured(buy))[:200]
            except Exception as e:
                out["buy_order_err"] = str(e)[:150]
        else:
            out["buy"] = "already carrying mission goods"
    ref = gated(lambda: api.mutate("spacemolt", "refuel", {"item_id": "fuel_cell", "quantity": 80}))
    out["refuel"] = str(api.structured(ref))[:200]
    return out

def execute(action, sc):
    """Execute one game mutation for the chosen action. Returns result dict."""
    sysid = (sc.get("location") or {}).get("system_id")
    if action == "mine":
        return gated(lambda: api.mutate("spacemolt", "mine", {}))
    if action in ("return_home", "jump_market", "jump_station", "flee_to_home"):
        if sysid == "haven":
            return arrive_actions(sc, action)
        home = json.loads(json.dumps(api.structured(api.query("spacemolt", "find_route", {"target_system": "haven"}))))
        nxt = home["route"][1]["system_id"]
        return gated(lambda: api.mutate("spacemolt", "jump", {"target_system": nxt}))
    if action == "jump_mission":
        if sysid == "traders_rest":
            return arrive_actions(sc, action)
        tgt = json.loads(json.dumps(api.structured(api.query("spacemolt", "find_route", {"target_system": "traders_rest"}))))
        mnx = tgt["route"][1]["system_id"]
        return gated(lambda: api.mutate("spacemolt", "jump", {"target_system": mnx}))
    return {"error": f"unknown action {action}"}

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "decide"
    api.ensure_auth()
    if mode == "decide":
        sc = gather()
        state, ans = decide(sc)
        action, why, conf = pick_action(sc, ans)
        print("STATE:", json.dumps(state)[:600])
        print("ANSWERS:", json.dumps(ans["answers"], indent=1))
        print("USAGE:", ans.get("usage"))
        print("DECISION:", action, "|", why)
        logrec({"event": "decide", "action": action, "why": why, "answers": ans["answers"]})
    elif mode == "act":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        for i in range(n):
            sc = gather()
            state, ans = decide(sc)
            action, why, conf = pick_action(sc, ans)
            res = execute(action, sc)
            rsc = api.structured(res)
            msg = (rsc.get("message") if isinstance(rsc, dict) else str(rsc)) or str(res)[:200]
            print(f"[{i+1}/{n}] {action} ({why}) -> {str(msg)[:180]}")
            logrec({"event": "act", "action": action, "why": why, "result": msg, "answers": ans["answers"]})
            if i < n - 1: time.sleep(12)
