# Review policy (proposal): ask the analyst less, where it's safe

Status: **built 2026-11-02** (`src/covenant_engine/brief.py`: `ACCURACY`, `level()`); the widget groups items by level. The numbers below are measured on the stored abstracts, 2026-10-31 (GPT check run on all 9 deals that evening).

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
- GPT second check, **blind** (GPT answers the same questions without seeing Claude's answers; code compares), all 9
  deals: same answer on 893 of 917 claims; 15 disagreements with a verbatim quote, 9 clauses GPT couldn't find. These 24
  are review items. At least one is a real miss by Claude: CARS's $125M cap on the cash netted in Senior Secured Net
  Leverage was not stored (the earlier confirm-mode check, which showed GPT Claude's answer, agreed with it).
- Planted-error probe on the blind comparison: 36 errors planted in Claude's values (levels +0.25, add-backs flipped)
  on CARS, TRS and MEC; the comparison caught 35.
- The confirm-mode run (GPT sees Claude's claim and quote) agreed with 898/917 and caught 5/5 planted errors, but
  missed the CARS cash cap: anchoring is real.
- Term-clause accuracy: answer keys exist for MEC, TRS and WEST only. To be measured.

## Proposed levels
| Level | Rule | Today's items (93 open across 9 deals) |
|---|---|---|
| **Decide** (full card, all context) | Tier 1 anything flagged; any GPT disagreement code verified; missing documents; ambiguous dates; conflicts | flags 35, GPT 24 (blind), calc/loan/pricing 3 |
| **Confirm in a batch** ("Both models agree", one Accept-all) | Tier 2 clause flagged only because its category is custom, uncapped, and GPT agrees on effect, caps and quote | 17 custom uncapped clauses: GPT gets the same answer on all 17 |
| **Show, don't ask** | Tier 3 fields with the quote verbatim and GPT agreeing: visible in the tabs, no review item | none are items today |

The other 14 term-check items (custom with a cap, or a clause that couldn't be checked) stay at **Decide**.

## What has to be true first
1. ~~GPT check run on all 9 deals, with per-claim verdicts saved.~~ Done 2026-10-31; the card shows GPT's verdict.
2. Term-clause accuracy measured against the keys, and a second deal family, before trusting Tier 2 batching.
3. Batch-confirm still records each item as accepted by the named reviewer.

## As built (2026-11-02)
Open items across 9 deals: **53 Decide, 40 Both models agree** (was 93 all shown the same way). CARS: 3 decide, 4 confirm.

| Rule | Level |
|---|---|
| GPT disagreed with a verbatim quote | Decide |
| GPT couldn't find the clause, Claude's quote is verbatim | Confirm |
| Flag: conflict / ambiguous date / dangling reference / missing schedule | Decide |
| Other flag, GPT read every related value the same | Confirm (the model's note is shown) |
| Term clause with a cap | Decide |
| Uncapped term clause, GPT reads the same effect | Confirm |
| Anything else (missing schedule, loan step) | Decide |

Each rule also requires its kind of check to be at least `CONFIRM_AT` (90%) accurate in `ACCURACY`. Rates marked
"illustrative" are placeholders until a person grades a sample: changing a number changes what is asked, no code change.
"Accept all" records one accepted decision per item under the reviewer's name. Nothing is auto-resolved.

## Comparison fix (2026-11-03): label clashes
One agreement clause can be stored as several parts (CARS 7.11 definition: clause (a) = secured debt, stored as
'ratio (a)', plus 'minus Unrestricted Cash', stored as '(a)'). Asked about '(a)', GPT named the debt, and the blind
comparison flagged "refers to Unrestricted Cash vs Indebtedness". Same reading, different label. Rule now: if GPT's
term is the term of another part in the same clause, and Claude's own quote contains Claude's term, it is the same
reading. Effect: GPT items 24 -> 23 (CARS clause (a) only). Planted-error probe, all 9 deals, before -> after:
levels 43/46 -> 43/46, add-back/deduction flips 504/529 -> 504/529, wrong defined term 100/100 -> 101/101 (quote
blanked). Known gap, unchanged: a wrong term whose quote still names GPT's term passes both before and after (5/100).
