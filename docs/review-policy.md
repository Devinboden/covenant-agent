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

## Question fix (2026-11-03): ambiguous clause labels
One agreement clause can be stored as several parts (CARS Senior Secured Net Leverage Ratio: clause (a) = the secured
debt, stored as 'ratio (a)', plus 'minus Unrestricted Cash', stored as '(a)'). The blind question for '(a)' named only
the label, so GPT answered for the start of clause (a) (the debt) and the comparison flagged a difference that wasn't
one. Fix at the source: a part whose label shares its exact clause letters with another part is now located by its
opening words in the question ("the part labelled (a) (it begins: "minus Unrestricted Cash of the Borrower..."). Those
are the agreement's own words, a location, not Claude's answer. 25 questions across 9 deals changed and were re-asked
(not all 917). A first attempt that patched the comparison instead was removed: it hid planted wrong-term errors
(46/58 caught vs 57/57) until tightened, and it treated the symptom.

## Caps reached through a definition (2026-11-04)
The $125M limit is written in the "Unrestricted Cash" definition. Asked about the "minus Unrestricted Cash" part of
the leverage ratio, GPT carried the cap through to that part; the abstract stores a cap where the agreement writes it.
The comparison flagged both the ratio's part and the definition, so one miss made two cards. Rule: a cap that sits on
the definition a part points to (in the abstract, or in GPT's answer for that definition) is compared on that
definition only. CARS: 2 GPT items -> 1 (the Unrestricted Cash card stays). Planted-error probe before -> after:
cap deleted 27/28 -> 27/28, fake cap added 681/681 -> 682/682, levels 43/46 -> 43/46, signs 503/528 -> 503/528.
