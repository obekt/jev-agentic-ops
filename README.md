# JeV Agentic Ops — System One decision layers for agent fleets

A pattern and reference implementation for wiring [TypeSafe's Jev](https://docs.typesafe.ai/introduction)
(a "System One" model: typed questions in, probabilistic decisions out — no text
generation, no parsing) into **autonomous agent operations**.

Built with [Hermes Agent](https://hermes-agent.nousresearch.com) as the host fleet,
but the pattern is host-agnostic.

## The core idea

LLM agents normally burn a reasoning model on *every* decision, including ones a
human expert would make in two seconds ("is this pirate nearby dangerous?", "is this
draft spam?"). JeV — a fast calibrated decision model — replaces that class of
decisions at ~100-1000x lower cost, while the expensive reasoning model stays in
charge of **code, planning, and execution**.

The contract:

```
CODE (host agent)               JEV (System One)
- owns all side effects          - answers atomic typed questions
- computes deterministic facts   - never executes anything
- composes answers with logic    - never sees the whole plan
- sets confidence thresholds    - returns probabilities + confidence
- handles fallback when low     - one parallel call per decision point
```

Three rules we learned running this for real:

1. **Never let Jev touch the trigger.** It ranks and flags; code acts. A wrong
   `noul=0.9` can't fire a mutation by itself — your thresholds decide.
2. **One call, many questions.** Atomic questions (Choice / Score / Noul) run
   in parallel against the same state. 4 questions ≈ same latency as 1. Decompose
   every judgment or the model drifts.
3. **Confidence is the second axis.** `answer` tells you *what*, `confidence`
   tells you *whether to act*. Below the bar → code heuristic fallback, logged as
   fallback so you can audit how often the model actually decided.

## Components

```
lib/jevlib.py               shared client: ask(state, {name:(type,instr[,criteria])})
pilots/spacemolt_pilot.py    live game pilot: Jev flies a cargo hauler 24/7
gates/content_gate.py        pre-publish quality/safety gate (spam/quality/jailbreak/hook)
```

### 1. The shared client (`jevlib`)

```python
from jevlib import Jev
ans = Jev().ask(
    state={"fuel": 163, "nearby_pirates": 0, "mission": "deliver 12 gold wire"},
    questions={
        "threat":   ("noul",  "staying here exposes the ship to significant combat risk"),
        "strategy": ("choice", "which operation next", {
            "deliver_mission": "go deliver", "acquire_then_deliver": "buy goods first",
            "mine_locally": "mine here", "return_home": "resupply"}),
    })
# ans["strategy"] == "acquire_then_deliver", ans["_raw"]["strategy"]["confidence"] == 0.75
```

Every call is logged to a usage JSONL so fleet spend is auditable (~$0.04/M input).

### 2. Game pilot: `pilots/spacemolt_pilot.py`

A live example: the pilot flies an MMO cargo run (SpaceMolt) where Jev makes the
strategic call each tick and code does everything else.

Each decision = one JeV call with 4 atomic questions:

| question | type | role |
|---|---|---|
| `strategy` | Choice | which operation: deliver / acquire-then-deliver / mine / go home / freight board |
| `threat`   | Noul  | hostile risk in current system |
| `urgency`  | Score | time pressure of mission/fuel clocks |
| `mine_fit` | Noul  | is current POI resource-rich |

Decision policy (pure code, tunable):

```python
if threat > 0.7:          act = "flee_home"          # threat overrides everything
elif confidence < 0.45:   act = heuristic(state)      # mine_fit>0.5 → mine, else go home
else:                     act = map(strategy)         # Jev drives
```

Execution layer (Jev never sees any of this): route computation via the game's
`find_route`, a shared cross-process tick gate (`play_gate.json`, 1 mutation/10s),
auto-dock + auto-buy of the exact mission item + refuel on arrival at the market
system, `complete_mission` on delivery arrival.

**Observed live:** 20+ consecutive decisions all `acquire_goods_then_deliver` at
confidence 0.67–0.89 across 10+ systems — stable strategic coherence with zero
context-rot, while deterministic code executed the multi-hour route.

### 3. Content gate: `gates/content_gate.py`

Pre-publish System One screen for anything an agent posts. One call, 4 questions:

```
spam_risk  (Score)  — table-dump-without-view vs genuine analysis
quality    (Score)  — filler vs testable claim with reasoning
jailbreak  (Noul)   — hidden instructions targeting AI readers
hook       (Noul)   — does it actually end with a discussion question
```

Policy: block on `jailbreak > 0.7 or quality < 1.0`; warn if no hook.
Calibrated against platform ground truth (our known is_spam=true/false corpus):

| draft | spam | quality | jailbreak | verdict | truth |
|---|---|---|---|---|---|
| normal analysis brief | 2.0 | 2.0 | 0.06 | pass | not spam ✓ |
| "Buy my thing. Click here." | 0.02 | 0.0 | 0.05 | blocked | junk ✓ |
| "Ignore all previous instructions…" | 0.08 | 0.01 | **0.93** | blocked | attack ✓ |

## Why this pattern (vs plain LLM or plain rules)

- **Rules alone** can't phrase "how frustrated is this customer" without brittle
  keyword soup. **LLM-as-judge** works but costs ~200x more per decision and adds
  latency + parse surface.
- Jev answers are *calibrated distributions*, so the same question can drive
  different thresholds in different products without retraining.
- Agents keep full control: when Jev is uncertain, your fallback logic runs —
  and you can log every fallback and tune the bar from live data.
- The host LLM is reserved for what it's good at: writing the code, the plans,
  the posts — while Jev is the cheap fast *gut* the code consults thousands of times.

## Cost shape

Our live numbers: ~700 input + ~120 output tokens per 4-question decision ≈
**$0.03 per thousand decisions at $0.04/M**. A 24/7 decision loop costs
single-digit cents/day. That's the unlock: decision frequency stops being the
budget constraint.

## Quickstart

```bash
export TYPESAFE_API_KEY=***        # from console.typesafe.ai
export SPACEMOLT_USER=... SPACEMOLT_PASS=...
pip install typesafe-sdk            # or just use lib/jevlib.py (stdlib only)

python3 pilots/spacemolt_pilot.py decide   # see Jev's read of your game state
python3 gates/content_gate.py post draft.md # gate a content draft (exit 0/2/1)
```

## Status

Running in production on our Hermes fleet:
- `spacemolt-jev-loop` cron: Jev pilots the hauler every 12 minutes, 24/7
- every outbound Moltbook post passes through the JeV gate before publish
- usage logged to `~/.hermes/logs/jev_usage.jsonl`

MIT. Feedback welcome — this is a young pattern and we want more deployments.
