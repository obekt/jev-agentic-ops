#!/usr/bin/env python3
"""jev_predict.py — System One priors for news briefs.

Reads a markdown news brief, extracts the headline/bullet items, and asks Jev
ONE parallel call covering every item: direction, impact, horizon. Appends a
"Jev quick-read" table so readers see what a calibrated decision model thinks
before the reasoning model argues.

Usage:
  python3 jev_predict.py brief.md            # print table
  python3 jev_predict.py brief.md --insert   # insert section into the file
"""
import json, os, re, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lib"))
from jevlib import Jev

MAX_ITEMS = 14

def extract_items(md):
    items = []
    for line in md.splitlines():
        s = line.strip()
        if (s.startswith("- ") or s.startswith("* ")) and len(s) > 25:
            txt = s[2:].strip()
            # skip our own sections / disclaimers
            if "jev" in txt.lower() or "not financial advice" in txt.lower():
                continue
            items.append(txt)
        if len(items) >= MAX_ITEMS:
            break
    return items

def predict(items):
    state = {"task": "pre-market read of news items by a fast decision model",
             "items": {str(i): t for i, t in enumerate(items)}}
    q = {}
    for i in range(len(items)):
        q[f"d{i}"] = ("choice", f"Net market direction of news item {i} over its horizon",
                      {"up": "risk-on / bullish net effect",
                       "down": "risk-off / bearish net effect",
                       "flat": "no clear net directional effect"})
        q[f"m{i}"] = ("score", f"Market impact magnitude of news item {i}",
                      ["negligible: noise for most portfolios",
                       "moderate: sector or single-name moves likely",
                       "major: index-level repricing plausible"])
        q[f"h{i}"] = ("choice", f"Dominant reaction horizon of news item {i}",
                      {"hours": "repriced within the session",
                       "days": "multi-day digestion",
                       "weeks": "slow structural effect"})
    ans = Jev().ask(state, q, timeout=45)
    rows = []
    for i, txt in enumerate(items):
        d = ans.get(f"d{i}", "?")
        m = ans.get(f"m{i}")
        h = ans.get(f"h{i}", "?")
        conf = (ans["_raw"].get(f"m{i}", {}) or {}).get("confidence")
        rows.append({"i": i, "item": txt, "dir": d, "impact": m, "horizon": h, "conf": conf})
    return rows, ans.get("_usage", {})

def to_table(rows):
    icon = {"up": "▲", "down": "▼", "flat": "◆"}
    lines = ["| # | item | dir | impact | horizon | conf |",
             "|---|------|-----|--------|---------|------|"]
    for r in rows:
        imp = r["impact"]
        imp_txt = {0: "negligible", 1: "moderate", 2: "major"}
        impact = imp_txt.get(round(imp) if imp is not None else -1, str(imp))
        conf = f"{r['conf']:.2f}" if r["conf"] is not None else "—"
        item = r["item"][:90] + ("…" if len(r["item"]) > 90 else "")
        lines.append(f"| {r['i']} | {item} | {icon.get(r['dir'],'?')} {r['dir']} | {impact} | {r['horizon']} | {conf} |")
    return "\n".join(lines)

def section(rows, usage):
    bias = sum(1 for r in rows if r["dir"] == "up")
    bear = sum(1 for r in rows if r["dir"] == "down")
    big = [r for r in rows if (r["impact"] or 0) >= 1.5]
    summary = (f"System One quick read: {bias} bullish, {bear} bearish, "
               f"{len(rows)-bias-bear} flat across {len(rows)} items. "
               f"Model-weighted tape bias: "
               f"{'risk-on' if bias > bear else 'risk-off' if bear > bias else 'balanced'}. "
               f"{len(big)} item(s) rated major impact.")
    return (f"\n## Jev quick-read (System One priors)\n\n"
            f"_Fast calibrated decision model (no text generation) scoring each item "
            f"before the reasoning pass — {usage.get('input_tokens','?')} input tokens, "
            f"one parallel call._\n\n"
            f"{to_table(rows)}\n\n"
            f"**{summary}**\n")

if __name__ == "__main__":
    path = sys.argv[1]
    md = open(path).read()
    items = extract_items(md)
    if not items:
        print("no bullet items found"); sys.exit(1)
    rows, usage = predict(items)
    sec = section(rows, usage)
    if "--insert" in sys.argv:
        out = md.rstrip() + "\n" + sec
        open(path, "w").write(out)
        print(f"inserted Jev quick-read ({len(rows)} items) into {path}")
    else:
        print(sec)
