# Spike: step 3 (term decomposition), 2026-10-12

One call per term; input is the intake bundle only (definition + referenced definitions). Code verifies quotes and
scores against the hand-built answer key. `python -m covenant_engine.core.decompose_term WEST "Adjusted Consolidated EBITDA" --model opus`

| Deal / term | Model | Clauses found | Sign | Quotes verified | Caps | Cost | Time |
|---|---|---|---|---|---|---|---|
| WEST Adjusted Consolidated EBITDA (25 clauses) | Opus | 25/25 | 25/25 | 25/25 | 4/4 incl. 7-step date schedule, shared pool, "after" basis | $0.65 | 100s |
| | Sonnet | 25/25 | 25/25 | 25/25 | 4/4 | $0.19 | 25s |
| | Haiku | 25/25 | 25/25 | 25/25 | 4/4 | $0.02 | 59s |
| ROAD Consolidated Adjusted EBITDA (12) | Opus | 11/12 | 11/11 | 11/11 | 2/2 real caps right ("before" basis; lesser-of 10%/$7.5M) | $0.08 | 29s |
| | Sonnet | 11/12 | 11/11 | 11/11 | same | $0.07 | 13s |
| | Haiku | 11/12 | 11/11 | 11/11 | same; split one clause into 5 provisos (broke the one-per-clause rule) | $0.01 | 71s |

Read by hand, not just scored:
- Caveat: WEST categories were scored against an enum partly derived from WEST's own key (easy mode). Other keys use
  free-form category names, so categories there were judged by reading: all reasonable; models disagree on genuinely
  ambiguous ones (ROAD terminal run-rate: custom vs projected_savings).
- Spec gaps the spike exposed: cap types need lesser_of / greater_of; components need a `conditions` field (time
  windows like "paid within 12 months of Oct 30 2024", agent approval, certification). The model had nowhere to put them.
- The one "missed" ROAD item is a "for the avoidance of doubt" sentence, not a lettered clause; the answer key may be over-specified.
- On clause-structured definitions all three tiers do well; differences are in edge judgment (over-splitting, category
  choice on ambiguous clauses). Not yet tested: CARS (30 components), TRS (cap on CNI exclusions), MEC (lifetime caps).

## Round 2 (2026-10-12): TRS, CARS, MEC (answer keys) + QUAD, SRI (no keys) x Opus/Sonnet/Haiku, one run each
`python -m covenant_engine.core.run_round --deals TRS CARS MEC QUAD SRI --models opus sonnet haiku` (summary in data/spike/round2/summary.txt).
Matching is by quote POSITION in the agreement (nearest within 60 chars), since keys label clauses differently.
First pass had a matcher bug (first-in-window instead of nearest, 250 chars) that paired neighbouring clauses; fixed and rescored.
- Lettered clauses: Opus and Sonnet found all; Haiku missed 1 in MEC and 1 in CARS, over-split TRS (19 vs 15-16), 1 quote failed verification (caught).
- Consistent "misses" across all models were key/spec issues, read by hand: CARS key calls 3 clauses cap "unlimited" (models: none, + time-limit notes);
  MEC key signs "+/-" (schema forced +/-); TRS/ROAD unmatched items are pro forma calculation paragraphs, not clauses.
- Model-vs-model category agreement on shared clauses: Opus-Sonnet ~95%, with Haiku ~90%; disagreements are genuinely ambiguous clauses.
- Cost per term: Opus $0.14-0.25, Sonnet $0.05-0.09, Haiku ~$0.01. Decision: Sonnet default for step 3, Opus for disputes; test model
  differentiation next on step 2 (regimes/events).
- More spec gaps: bi-directional sign (+/-), pro forma rules as calculation rules (not components), `conditions` (time limits) again.

## Step 2: maintenance covenants, schedules and regimes (2026-10-12, `step2.py`, one run, 7 deals x 3 models)
Input: the intake packet only. One structured-output call per deal (enums for covenant kind, direction, frequency,
row regime, regime type, trigger, adjustment). Scored by evaluating each schedule at every quarter-end 2025-2028
(standard and relief separately) against the answer keys, and by regime type + parameters.

| | Opus | Sonnet | Haiku |
|---|---|---|---|
| Key covenants found (5 deals) | 13/14 | 13/14 | 13/14 |
| Schedule points matching key (adj.*) | all | all | WEST 54/72, MEC 34/41 |
| Regime fields (27) | 27/27 | 26/27 | 26/27 |
| Quotes verified (7 deals) | 87/87 | 85/85 | 87/87 |
| Cost per deal | $0.24-0.96 | $0.06-0.23 | $0.01-0.15 |

*Adjusted for answer-key artifacts, not model errors: MEC capex pre-2026 limit ($50M) is history that's no longer in the
live text; CARS 7.12 sale-leaseback cap is in the key as a "level" covenant but is tested per transaction (all 3
models excluded it, as instructed for incurrence-type tests).
- Opus = Sonnet: 104/104 schedule points agree on WEST, identical everywhere else. Sonnet missed one field: CARS
  step-up duration is "12 months" and Sonnet left quarters blank rather than convert (spec fix: duration + unit).
- Haiku errors are structural, not numeric: MEC relief rows labelled "standard" (values right, regime wrong, so the
  early-exit override would never apply); WEST liquidity covenants given an end date of 2026-10-01 (a date that
  appears in the text, but for the coverage step, not the relief period). Haiku also raised a step-up on SRI from a
  dangling reference ("the increase pursuant to Section 8.2.16") to a step-up that Amendment No. 3 removed: wrong
  as a regime, but a real inconsistency in the document that deserves a flag.
- QUAD/SRI (no key): all 3 models agree on covenants and every schedule point.
- Takeaway: no Opus/Sonnet differentiation on step 2 either. Sonnet stays default. Haiku needs code checks on
  regime labels (e.g. rows inside a named relief period must be "relief").
