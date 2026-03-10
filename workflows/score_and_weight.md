# Workflow: score_and_weight

## Objective
Aggregate the five agent signals into a single confidence-weighted final score, apply Dalio's regime-based weight shifts, and return a BUY / WATCH / AVOID recommendation.

## Tool
`tools/score_and_weight.py`

## Prerequisites
All five agents must have completed:
- `tools/dalio_agent.py` → `regime_flag` + signal
- `tools/graham_agent.py`, `buffett_agent.py`, `lynch_agent.py`, `simons_agent.py` → signal + confidence

Missing agents are skipped and their weights are redistributed to the remaining agents.

## Required Input
```python
agent_results = {
    "dalio":   { ... dalio agent output ... },
    "graham":  { ... graham agent output ... },
    "buffett": { ... buffett agent output ... },
    "lynch":   { ... lynch agent output ... },
    "simons":  { ... simons agent output ... },
}
```

## Expected Output
```json
{
  "ticker":        "AAPL",
  "regime_flag":   "neutral",
  "final_score":   0.731,
  "recommendation":"BUY",
  "applied_weights": {
    "graham": 0.150, "buffett": 0.250, "dalio": 0.200,
    "lynch":  0.200, "simons":  0.200
  },
  "agent_contributions": {
    "graham":  { "signal":"buy",  "vetoed_signal":"buy",  "veto_applied":false,
                 "confidence":0.78, "raw_score":1.0,
                 "base_weight":15.0, "effective_weight":0.171, "contribution":0.171 },
    ...
  },
  "consensus": {
    "buy":   ["graham", "buffett", "lynch"],
    "watch": ["dalio", "simons"],
    "avoid": []
  },
  "deleveraging_veto_applied": false,
  "run_at": "2026-03-10T09:32:11"
}
```

Cached to `.tmp/<TICKER>_score.json`.

## Scoring Formula

### Step 1 — Signal → numeric
| Signal | Score |
|---|---|
| buy | 1.0 |
| watch | 0.5 |
| avoid | 0.0 |

### Step 2 — Regime-adjusted base weights

Raw weights before normalization:
| Regime | Graham | Buffett | Dalio | Lynch | Simons | Total |
|---|---|---|---|---|---|---|
| neutral | 15 | 25 | 20 | 20 | 20 | 100 |
| risk-on | 5 | 25 | 20 | 40 | 30 | 120 |
| risk-off | 35 | 35 | 20 | 5 | 15 | 110 |
| deleveraging | 45 | 25 | 20 | 20 | 20 | 130 |

Weights are normalized to sum to 1.0 at runtime (missing agents also trigger renormalization).

### Step 3 — Confidence weighting
```
effective_weight_i = base_weight_i × confidence_i
```
Then all effective weights are normalized to sum to 1.0. An agent with confidence 0.4 has 40% of their nominal weight relative to a fully confident agent.

### Step 4 — Weighted score
```
final_score = Σ(signal_score_i × normalized_effective_weight_i)
```

### Step 5 — Deleveraging veto (conditional)
If `regime_flag == "deleveraging"` AND `dalio.signal == "avoid"`:
- Any non-Dalio agent signaling "buy" is capped to "watch" before scoring
- `veto_applied` flag set on those agents in output
- Prevents the final score from reaching 0.70 BUY threshold

### Step 6 — Recommendation threshold
| Score | Recommendation |
|---|---|
| 0.70 – 1.00 | BUY |
| 0.50 – 0.69 | WATCH |
| 0.00 – 0.49 | AVOID |

## Edge Cases

| Situation | Behavior |
|---|---|
| Agent result missing | Skipped; remaining weights renormalized |
| All agents missing | Returns score 0.5 (neutral fallback) |
| regime_flag unrecognized | Defaults to "neutral" weights |
| Dalio missing but deleveraging veto would apply | Veto cannot apply without Dalio result — treated as no veto |

## Updates
- 2026-03-10: Initial version. Confidence-weighted scoring, deleveraging veto, consensus summary.
