# Review policy (proposal): ask the analyst less, where it's safe

Status: **proposal, not built.** The numbers below are measured on the stored abstracts, 2026-10-31 (GPT check run on all 9 deals that evening).

## Principle
What the analyst sees for a check depends on two things:
1. **Criticality:** how directly the field drives a covenant pass/fail.
2. **Model accuracy:** how often the extraction is right for that kind of field, plus whether a second model (GPT)
   independently agrees, with a quote code finds verbatim in the agreement.

High criticality or low agreement means the analyst decides. Low criticality and two models agreeing means it's a
fast confirm in a batch. A person still clicks; no item is ever auto-resolved. Code verifies, and four eyes applies.

## Criticality tiers
| Tier | What | Why |
|---|---|---|
| 1 Critical | Covenant levels and test dates, step-up / relief regimes, caps on EBITDA add-backs, equity cure, conflicts between documents | Changes pass/fail directly |
| 2 Material | Clauses of defined terms with no cap (add-back vs deduction, category), calculation rules, deemed values | Changes the inputs; usually small |
| 3 Admin | Agent, commitment, maturity, pricing grid, reporting days | Doesn't change pass/fail |

## Accuracy so far (development deals, not a holdout)
- Covenant schedules 350/351, regime fields 27/27 (WEST, ROAD, TRS, CARS, MEC vs answer keys)
- Loan 51/51, events 15/15, calculation rules 9/9, deemed values 5/5, pricing 36/36
- GPT second check, all 9 deals: agrees with 898 of 917 claims; 6 verified disagreements (UCTT), 2 not found, 11 with
  no verdict (WEST). Every agreement but one carries a quote code found verbatim in the agreement.
- Is GPT actually checking? Planted-error probe on CARS: 5 claims altered (two covenant levels, two add-backs flipped to
  deductions, the step-up ceiling); GPT flagged all 5 and still agreed with the other 82.
- Caveat: each claim shows GPT our quote, so GPT confirms rather than extracting blind.
- Term-clause accuracy: answer keys exist for MEC, TRS and WEST only. To be measured.

## Proposed levels
| Level | Rule | Today's items (77 open across 9 deals) |
|---|---|---|
| **Decide** (full card, all context) | Tier 1 anything flagged; any GPT disagreement code verified; missing documents; ambiguous dates; conflicts | flags 35, GPT 8, calc/loan/pricing 3 |
| **Confirm in a batch** ("Both models agree", one Accept-all) | Tier 2 clause flagged only because its category is custom, uncapped, and GPT agrees on effect, caps and quote | 17 custom uncapped clauses: GPT agrees on all 17 |
| **Show, don't ask** | Tier 3 fields with the quote verbatim and GPT agreeing: visible in the tabs, no review item | none are items today |

The other 14 term-check items (custom with a cap, or a clause that couldn't be checked) stay at **Decide**.

## What has to be true first
1. ~~GPT check run on all 9 deals, with per-claim verdicts saved.~~ Done 2026-10-31; the card shows GPT's verdict.
2. Term-clause accuracy measured against the keys, and a second deal family, before trusting Tier 2 batching.
3. Batch-confirm still records each item as accepted by the named reviewer.
