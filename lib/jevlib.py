#!/usr/bin/env python3
"""jevlib — shared TypeSafe "Jev" (System One) helper for all agent operations.

Pattern: CODE owns execution + deterministic logic; Jev owns atomic judgment.
Every judgment is a separate question in ONE parallel call; confidence gates
whether we act on Jev or fall back to a code heuristic.

Usage:
    from jevlib import Jev
    j = Jev()                       # reads ~/.config/typesafe/credentials.json
    ans = j.ask(state_dict, {
        "relevance": ("score", "How financially material is this item to a trading agent",
                      ["noise", "background context", "actionable signal"]),
        "urgent":    ("noul", "This item requires action within hours"),
    })
    print(ans["relevance"], ans["_raw"])

ans maps question name -> scalar (score float / choice str / noul float),
with ans["_raw"] holding full per-question detail incl. confidence.
"""
import json, os, time, urllib.request

CRED = os.path.expanduser("~/.config/typesafe/credentials.json")
# KEY resolution order: TYPESAFE_API_KEY env > credentials file
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
STATS = os.path.expanduser("~/.hermes/logs/jev_usage.jsonl")

class Jev:
    def __init__(self, model=None, api_key=None):
        self.key = api_key or os.environ.get("TYPESAFE_API_KEY")
        if not self.key:
            cfg = json.load(open(CRED))
            self.key = cfg["api_key"]
            model = model or cfg.get("model", "jev-latest")
        self.model = model or "jev-latest"

    def ask(self, state, questions, timeout=30, retries=2):
        """questions: {name: (type, instructions[, criteria])}. Returns flat dict."""
        payload_q = {}
        for name, spec in questions.items():
            qtype, instr = spec[0], spec[1]
            q = {"type": qtype, "instructions": instr}
            if len(spec) > 2 and spec[2] is not None:
                q["criteria"] = spec[2]
            payload_q[name] = q
        body = json.dumps({"state": state, "model": self.model, "questions": payload_q}).encode()
        last = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(ENDPOINT, data=body, headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.key}"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    resp = json.loads(r.read().decode())
                break
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 * (attempt + 1))
        else:
            raise last
        out = {}
        for name, a in resp.get("answers", {}).items():
            t = a.get("type")
            out[name] = a.get("choice") if t == "choice" else a.get("score") if t == "score" else a.get("noul")
            out.setdefault("_raw", {})[name] = {
                "type": t,
                "value": out[name],
                "confidence": a.get("confidence"),
                "probabilities": a.get("probabilities"),
            }
        out["_usage"] = resp.get("usage", {})
        try:
            with open(STATS, "a") as f:
                f.write(json.dumps({"ts": time.time(), "questions": len(payload_q),
                                   "usage": out.get("_usage")}) + "\n")
        except Exception:
            pass
        return out

def decide_with_fallback(jev_state, questions, fallback, conf_bar=0.45, key=None):
    """Ask Jev; if confidence < bar (or call fails), use fallback(state).
    Returns (value, source) where source in {'jev','fallback'}."""
    try:
        j = Jev()
        ans = j.ask(jev_state, questions)
        raw = ans["_raw"].get(key or list(questions)[0], {})
        conf = raw.get("confidence")
        if conf is None:  # noul has no confidence; treat as decisive
            return ans[key or list(questions)[0]], "jev"
        if conf >= conf_bar:
            return raw["value"], "jev"
    except Exception:
        pass
    return fallback(jev_state), "fallback"
