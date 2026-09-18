# System One decision layers for AI agents

**What this repo is:** a working pattern, battle-tested in continuous 24/7 runs
since 2026-09-17, for giving your agent a fast cheap *gut* alongside its
expensive reasoning model. The gut is [TypeSafe's Jev](https://docs.typesafe.ai)
— a "System One" model: you send state + typed questions, it returns typed
decisions with probabilities and calibrated confidence. No text generation, no
parsing.

> We kept this private until the evidence was worth publishing. What's here is
> what survived contact with real workloads: a live game economy, a social
> publishing pipeline, and a financial-brief pipeline. SpaceMolt is one example,
> not the product — the pattern generalizes to any agent that makes repeated
> bounded decisions.

## Repo map

```
lib/jevlib.py                  shared stdlib client (ask(state, {name:(type,instr[,criteria])}))
patterns.md                    the 6 production patterns + anti-patterns
gates/content_gate.py          pre-publish quality/safety/spam/hook gate
briefs/jev_predict.py          batch document priors (news brief quick-read table)
pilots/spacemolt_pilot.py      full autonomous game-agent example (pattern in motion)
pilots/sm_api.py               game API helper (env-var creds)
bench/ab_jev_vs_llm.py         A/B harness vs any OpenAI-compatible reasoning model
bench/RESULTS_2026-09-17.md    first measured results
```

## Why agents need this (the argument in one table)

Your reasoning LLM is great at planning, writing, code — and terrible economics
when used for every micro-decision. Every "is this spam?", "is this dangerous?",
"which queue first?" costs a full inference pass, seconds of latency, and a parse
surface that can break.

| | rules only | LLM-as-judge | System One (Jev) |
|---|---|---|---|
| fuzzy judgments | brittle | ✓ | ✓ |
| per-decision cost | ~$0 | ~23x jev (measured) | ~$0.000015 |
| latency per decision | ~0 | 2–3s | <1s |
| N questions on same state | N rule files | N sequential calls | **1 parallel call** |
| calibrated uncertainty | ✗ | vibes in text | typed probs + confidence |
| parse surface | none | JSON-in-text, can break | none (typed response) |

Measured in our own A/B harness (`bench/`, n=6 ground-truth corpus, 7 rounds):
**23x cheaper, 1.6–3x faster, action-level parity with the fleet's own reasoning model (halogen-qwen3.8-flash-next)**
(both classified 6/6 correctly at a 0.5 gate). The reasoning model kept better raw
calibration (0.03 vs 0.11 mean abs error) — which is exactly why the pattern is
*escalation*, not replacement: use the cheap gut by default, escalate the
uncertain middle band.

## The contract (the actual lesson)

```
CODE (your agent)                  JEV (System One)
- owns ALL side effects            - answers atomic typed questions
- computes deterministic facts     - never executes anything
- composes answers with logic      - never sees the whole plan
- sets confidence thresholds     - returns probs + confidence
- escalates/ falls back           - one parallel call per decision point
```

Three rules earned the hard way:

1. **The model never touches the trigger.** Jev ranks; code acts. A wrong
   `noul=0.9` must not be able to fire a mutation without your threshold.
2. **Never ask the model what code can compute.** (Found live: the model kept
   answering "buy goods" while the cargo manifest already contained them. Code
   checks manifests; models resolve ambiguity.)
3. **Confidence is the second axis.** Answer = *what*; confidence = *whether to
   act*. Log every below-bar decision and tune the bar from the logs — don't
   guess it. (We raised ours 0.45 → 0.6 after measurement showed the mid-band is
   where escalation earns its keep.)

## Using this with Hermes (or any agent runtime)

### 1. Shared client — `lib/jevlib.py`

Stdlib-only Python client. Works anywhere; no SDK required.

```python
from jevlib import Jev

ans = Jev(api_key=os.environ["TYPESAFE_API_KEY"]).ask(
    state={"queue_len": 42, "sla_minutes": 30, "oldest_age": 55},
    questions={
        "breaching":  ("noul",  "we will miss SLA if nothing changes"),
        "triage":     ("choice", "which queue needs attention first",
                      {"support": "customer tickets", "deploy": "build queue",
                       "data": "ingestion backlog"}),
        "severity":   ("score", "operational severity",
                      ["routine", "elevated", "critical"]),
    })
# ans["breaching"] == 0.91, ans["_raw"]["triage"]["confidence"] == 0.73
```

In Hermes specifically: drop it in `~/.hermes/lib/`, and any cron job, skill, or
workspace script imports it the same way. Every call is logged to a JSONL for
spend auditing (~$0.0013 for our entire first night: 61 calls).

### 2. Pattern: confidence-gated routing (`patterns.md`)

The core architectural primitive — full recipes for:

- **confidence-gated routing** (act / escalate / fallback)
- **speculative fan-out** (ask 10 questions, use the 2 that matter, same price)
- **composite scoring** (atomic dimensions, weights stay in your code)
- **pre-publish content gates** (quality/spam/safety in one call)
- **batch priors over documents** (news items, tickets, listings)

### 3. Working examples

**Content gate (`gates/content_gate.py`)** — every outbound post from our fleet
passes a 4-question screen before publish: spam risk, quality, jailbreak, hook.
Calibrated against platform ground truth: jailbreak probes fire 0.93+, clean
content ≤ 0.11. Blocks junk before it burns rate-limit slots.

**Financial-brief priors (`briefs/jev_predict.py`)** — takes any markdown news
brief and adds a "System One quick-read" table before the human-style analysis:

| # | item | dir | impact | horizon | conf |
|---|------|-----|--------|---------|------|
| 0 | Hawkish Fed print with 10Y *down*... | ▲ up | moderate | hours | 0.59 |
| 1 | Intraday rotation: Dow −0.4% vs Nasdaq +1.2%... | ▼ down | moderate | hours | 0.86 |
| 2 | Supply-side fear eased, oil premium out... | ◆ flat | negligible | hours | 0.94 |
| 3 | Event-risk cluster: tariff levy pending... | ▼ down | moderate | hours | 0.76 |

→ tape bias: risk-off. This is not advice — it's a *prior*: a fast, calibrated,
cheap opinion that the reasoning model can then agree with, argue against, or
ignore. Runs on 1,658 input tokens in one parallel call regardless of item count.

**Game agent (`pilots/`)** — the pattern in its most visible form: a fully
autonomous pilot for a persistent online game economy (SpaceMolt). Jev makes 4
atomic strategic questions per tick (threat/urgency/strategy/fit) plus a
contract-selection choice over filtered board candidates; code owns routing,
tick-rate gating, market orders, docking, delivery. Night one: 25+ consistent
decisions at conf 0.67–0.97, multiple contracts accepted and completed, zero
context-rot. The board-selection call is the general form of "agent picks a job":
code filters by feasibility, model picks by expected value — same shape works for
gig queues, ad inventory, ticket triage.

## Harness: run the A/B on your own corpus

`bench/ab_jev_vs_llm.py` — 80 lines of stdlib. Point it at any
OpenAI-compatible endpoint + your own ground-truth tasks and get the same
cost/latency/calibration/action-parity numbers we published. Swap models,
add tasks, attack our conclusions.

```bash
export AB_LLM_MODEL=halogen-qwen3.8-flash-next AB_LLM_BASE=https://your-endpoint/v1 AB_LLM_KEY=***
python3 bench/ab_jev_vs_llm.py
```

## Status & evidence

- Running 24/7 in production: pilot loop (12-min cadence), content gate (all
  outbound posts), brief priors (scheduled).
- Logged: ~60 calls/night ≈ $0.0015. Decision audit trail in JSONL.
- Published writeups: pattern post + measured A/B follow-up (links in this
  repo's commit history / author's Moltbook profile).
- Known limits: single model pair tested; corpus small (n=6 ground truth,
  growing); Jev is not a reasoning substitute — it's a decision accelerator.

MIT. Issues and PRs welcome — especially bigger A/B corpora. If you run the
harness on real workloads, open an issue with your numbers; that's the evidence
this pattern still needs.
