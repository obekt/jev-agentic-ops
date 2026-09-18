# Patterns — confidence-gated decision layers

Recipes for composing System One answers into agent behavior. All code lives in
your agent; Jev only answers questions. Every pattern includes the production
lesson we learned deploying it.

## 1. Confidence-gated routing (the core primitive)

```python
ans = Jev().ask(state, {"decision": ("choice", "...", options)})
value = ans["decision"]
conf  = ans["_raw"]["decision"]["confidence"]

if conf >= HIGH_BAR:            act(value)              # trust the gut
elif conf >= LOW_BAR:           escalate_to_llm(state)  # pay for thinking
else:                           fallback_heuristic(state)
log_decision(value, conf, source)                       # always
```

**Production lesson:** start with LOW=0.45/HIGH=0.6 and tune from logs. The
escalation band matters — measurement showed it's where the reasoning model
actually earns its 23x price premium; the tails are where it wastes tokens.
Noul questions have no confidence field; treat the probability itself with a
wider dead-band (0.35–0.65 escalate) or pair every noul with a scoring twin.

## 2. Speculative fan-out

Ask questions you might not need in the same call — adding questions barely
changes latency or cost (~120 tokens each).

```python
q = {
  "is_spam": ("score", ...), "is_urgent": ("noul", ...),
  "is_jailbreak": ("noul", ...), "topic": ("choice", ...),
  "would_engage": ("noul", ...),  # speculative
}
```

**Production lesson:** the speculative questions are free insurance. We added
"would this get a reply?" to our publish gate and it now correlates with
engagement better than our own heuristic did.

## 3. Composite scoring

Don't ask "is this good?". Ask 4 atomic dimensions, weight them in code:

```python
dims = jev(state, {d: ("score", RUBRIC[d], LEVELS) for d in DIMS})
score = 0.4*dims["quality"] + 0.3*dims["relevance"] + 0.3*dims["freshness"]
```

**Production lesson:** when priorities shift, change a coefficient, not a
prompt. We retuned our brief-prior weighting three times with zero re-prompting
and zero re-asking of the model.

## 4. Feasibility filter + expected-value pick (agent picks a job)

General form of contract/task selection:

1. **Code** filters candidates by hard capability constraints (what the agent
   *can* do).
2. **Jev** picks one via Choice over the filtered set, with the agent's
   history/traits in the state.
3. Fallback = argmax on a simple metric if confidence < bar.

**Production lesson:** putting loss history in the state changed picks
dramatically — the model avoided high-reward/high-risk options we'd historically
failed at (0.98 probability on a moderate-paying safe option). Feeds like
"51 units lost historically to X" are legitimate decision inputs.

## 5. Batch document priors

Score N documents/items in one call (see `briefs/jev_predict.py`): direction,
magnitude, horizon per item. Use as a *prior table* that downstream reasoning
can agree/argue with, and as a fast pre-filter (drop negligible items before
anywhere expensive).

**Production lesson:** conf on these is modest (0.45–0.94) — surface the conf
column. Honest priors with visible uncertainty beat confident vibes.

## 6. Pre-publish safety gate

One call: spam_risk / quality / jailbreak / hook. Block on jailbreak > 0.7 or
quality floor. Fails OPEN with a log if Jev is unreachable (never let the gate
take down publishing) — but alert on fail-open frequency.

**Production lesson:** the jailbreak detector is the single most valuable
question we ask. 0.93 on obvious injection attempts, ≤0.11 on everything
legit. Keep a second line of defense; don't trust one 0.7 threshold for
truly high-stakes content.

## Anti-patterns we hit

- Asking the model to notice computable facts (inventory, counts) — code owns
  those, always.
- One mega-question ("what should I do?") — decompose or lose reliability.
- Acting on probability when confidence is what should gate the action.
- Unlogged decisions — you can't tune thresholds you don't measure.
