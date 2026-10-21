# Step 3: term decomposition with a code-driven loop (Method 1)

The model only assembles parts from the library. Code checks every part, fetches any context the model asks for, and
re-asks only for the parts that failed. The model gets no exploration tools; code is its only way to get more text.

| Round | Who | What |
|---|---|---|
| 1 | model | every component of the term, plus `needs_context` (definition or section names) and `search_for` (phrases) |
| check | code | quotes verbatim in the agreement; cap values and schedules; lesser_of/greater_of give both limits; duplicates; clause coverage against code's own enumeration (`clauses.py`); "custom" parts go to review and are never retried |
| fetch | code | requested definitions and sections from the intake bundle; text search; defined terms the model mentioned but wasn't given |
| 2-3 | same model | only the failing or missing clauses, with the errors and the fetched context |
| escalate | Opus | one round, and only if a problem or missing clause remains. Context alone never escalates |
| review | person | whatever is left, with the reason; `starts_from` must be a defined term |

```
python -m covenant_engine.abstract.decompose ROAD "Consolidated Adjusted EBITDA" --model haiku [--strip-context]
python -m covenant_engine.abstract.test_loop --models haiku sonnet --strip TRS ROAD QUAD
```

## Results (2026-10-12, 7 deals, `data/abstract/loop_run.json`)
- **Round 1 was already clean.** After round 1: 0 missing clauses on every run and 1 bad quote (SRI Haiku), which the
  loop fixed. Escalation never fired. Nothing was left open except "custom" parts (0-4 per deal, by design).
- **Answer-key recall did not move.** It matches the one-shot runs: WEST 23/25, ROAD 11/12, TRS 13/15, CARS Haiku 29/30
  vs Sonnet 30/30, MEC Haiku 15/17 vs Sonnet 16/17. Every remaining miss is outside the lettered clauses: pro forma
  paragraphs, provisos, a split of (xv) into (x)/(y), and unnumbered final sentences. Code doesn't enumerate those, so
  the loop can't see they're missing. QUAD Haiku returned 15 parts against Sonnet's 20, dropping the same unnumbered
  text, and nothing flagged it.
- **Missing context: the models asked; they didn't guess.** With no referenced definitions given (TRS, ROAD, QUAD),
  both models used `needs_context`. Sonnet asked for 2-7 targeted terms. Haiku asked for 12-22, nearly every
  capitalised term. Code supplied them, and recall came out identical to the full-context runs. Cost: Haiku ~2x,
  Sonnet about the same.
- **Cost per term:** Haiku $0.01-0.03; Sonnet $0.05-0.26. Most runs took a second round only because code auto-fetched a
  term the model mentioned. Those rounds changed no measured score here.

## What this says
On these 7 deals the loop doesn't close a Haiku/Sonnet gap, because the gap isn't in the lettered clauses the loop
checks. Next fix: have code also enumerate the unnumbered parts (provisos, "For purposes of", pro forma paragraphs,
final sentences), so a dropped part gets flagged as missing. Pro forma text should map to the spec's pro forma rules,
not to components.

## Full abstract build (spec v1 draft, 2026-10-12): `python -m covenant_engine.abstract.build [--reuse-step2]`
intake bundle -> step 2 (covenants, regimes, flags) -> code links each covenant's metric to its ratio definition ->
breadth-first through the terms it uses (ratio -> numerator/denominator -> referenced terms) -> one abstract JSON,
validated against the spec schema. Every term is decomposed by the same code-driven loop with a parts library per
family, picked by code from the term name:

| Family | Parts (examples) |
|---|---|
| ratio | numerator_term, numerator_less_term (cash netting), denominator_term, measurement_period |
| ebitda | 35 add-back / deduction parts (spike library) |
| debt | borrowed money, LCs drawn/undrawn, capital & synthetic leases, guarantees, earn-outs, hedges, receivables financing, interest-bearing catch-all, JV debt, secured-only filter, exclusions |
| cash | unrestricted cash, cash equivalents, permitted investments, exclusions (restricted, foreign, liened, earmarked), account rules, cap_on_total |
| interest | GAAP / cash interest, capitalized, LC & commitment fees, hedge costs, OID/PIK, receivables costs, preferred dividends, interest income, exclusions |
| capex | PP&E additions, capital-lease additions, carve-outs, carry-forward |
| all | term_reference (+ ref_clauses: "clauses (a)-(d) of Indebtedness"), calculation_rule (floor at zero, annualization), custom -> review |

Library additions came from customs that recurred across deals (interest-bearing catch-all in 4 deals, JV debt in 3,
hedge breakage in 2). One-off customs stay custom and go to review.

Code checks added for terms: every term a ratio names must be referenced; ref_term must be a defined term; ref_clauses
must exist in that term; one cash cap (identical repeats merged by code, differing ones to review); each quote must
touch its own clause (siblings that share an 80-char opening are handled: TRS (b)(ii)-(iv)).

Results (7 deals, Sonnet, step 2 reused): 5-10 terms per deal, every covenant linked to its terms, 0-1 open review
items per deal (WEST's genuinely two-limb, time-based cash cap; MEC one missing ref_term), $0.26-0.80 per deal for
terms. Scores against answer keys where comparable: EBITDA unchanged (23/25, 11/12, 13/15, 30/30, 16/16); other terms
can't be scored cleanly: the keys are free-form (ROAD Funded Debt 3/8 is a representation difference: the key copied
Indebtedness (a)-(d) in, the model references them; TRS interest 6/8 is partly a key quote with an HTML page number and
the shared-opening issue above).

## Accuracy of non-EBITDA terms vs independent v1 keys (2026-10-12)
Keys: data/keys_v1/{WEST,TRS,MEC}.terms.json, built by 3 independent Opus agents from the agreement text only (no
access to model output), validated by the pipeline's own code checks (`validate_key`). Scorer: `score_keys`.
Referee: Fable 5.1 ruled on every disagreement blind to which side was the key (`adjudicate`, $3.30): key right 19,
model right 12, both wrong 2. 12 key fixes applied where Fable and review agreed (log in each term's notes).

| | Before new checks | After new checks (rerun) |
|---|---|---|
| Key terms decomposed | 22/24 | 24/24 |
| Parts found | 154/160 | 171/182 |
| Category | 142/154 | 147/171 (163/171 = 95% excluding one library ambiguity*) |
| Sign | 148/154 | 167/171 |
| Term references | 27/31 | 28/31 |

*cash_equivalents vs permitted_investments for list items inside the Permitted Investments / Cash Equivalents
definitions (16 of 24 category misses): same engine result; the library needs one convention.

Silent material errors the keys exposed, and the code check added for each (all now fire):
- TRS 6.13 cash cap $150M instead of $240M ("solely with respect to ... Section 6.13") -> any covenant-specific variant
  in a definition goes to review naming the section.
- TRS Total Indebtedness (a) tagged as scope, so the main debt amount wasn't counted -> a clause with only qualifier parts
  (scope / filter / account rule / period) is a problem.
- MEC Unrestricted Cash: only the lien exclusion, no base amount -> every debt/cash/interest/capex term needs a '+' amount.
- '+/-' on ratio sides (WEST, MEC) -> not allowed on ratio parts.
- WEST Permitted Investments / MEC Cash Equivalents never decomposed -> cash terms now follow their cash refs.
Library addition: ratio *_amount parts for amounts that aren't defined terms (MEC "income tax expense").
Still open: WEST two-limb time-based cash cap (to review), cap scoring is format-sensitive, MEC valuation sentences and
UCC account/lien/proceeds parts still missed (5 parts).

## Diagnosing errors (2026-10-12, `diagnose.py`): repeat runs + one-change counterfactuals, single calls, no loop
| Error | As-is (5 runs) | Change | With change | Cause |
|---|---|---|---|---|
| TRS 6.13 cash cap $150M (should be $240M) | 0/5, but the note names $240M for 6.13 in 5/5 | tell it the covenant / allow a two-value cap | 5/5 / 5/5 | spec + missing information: one cap field, and the model isn't told which covenant it serves. Not a misreading |
| TRS Total Indebtedness (a) as scope | 5/5 correct | (n/a) | | OUR PIPELINE: the old one-part-per-clause merge overwrote the amount part with the scope part (fixed earlier) |
| MEC cash with one part | 5/5 correct (6 parts) | (n/a) | | OUR PIPELINE: a context-only repair round returned one part with the shared blank clause label and the merge deleted the other 5. Fixed: only code-flagged clauses are replaced; other returns update or add. Rerun 3x: 6 parts every time |
Lesson: read the model's own note before blaming the model; 2 of 3 "model errors" were pipeline bugs, the third was the spec.

## Baseline after merge fix + dedupe + format-insensitive cap scoring (2026-10-12, tasks 1.1-1.3)
All 7 rebuilt (step 2 reused): $0.37-0.93 per deal for terms; open review 0-1 per deal (WEST two-limb cash cap;
TRS 6.13 variant). Covenants / regimes / EBITDA unchanged (all key schedule points and regime fields; EBITDA 23/25,
11/12, 13/15, 30/30, 16/16). Non-EBITDA terms vs v1 keys (WEST/TRS/MEC):

| | Before fixes | Baseline now |
|---|---|---|
| Terms | 24/24 | 24/24 |
| Parts found | 171/182 (94%) | 175/182 (96%) |
| Category (excl. cash_equivalents/permitted_investments ambiguity) | 163/171 (95%) | 166/175 (95%) |
| Sign | 167/171 | 171/175 (98%) |
| Term references | 28/31 | 28/32 |
| Caps | 2/8 (strict scorer) | 4/8: remaining 4 are real (WEST two-limb cap, TRS 6.13 $240M, 2 MEC) |

Run-to-run variance is visible: individual parts flip between runs (e.g. WEST Consolidated Debt (x) term_reference vs
borrowed_money; WEST Indebtedness (d)(i)-(iii) exclusions found in one run, not the next). Single-run scores move by a
few parts; repeat runs needed before reading small differences.
Key fix: MEC (a)(ii) had a dateless cap_schedule row copied from model output; removed, and code now rejects dateless
schedule rows.

## Run-to-run variance (2026-10-12, `variance.py`, 4 runs of terms on WEST/TRS/MEC, $2.10 per run)
| Metric | Runs | Range |
|---|---|---|
| Parts found | 175, 172, 171, 171 /182 | 94-96% |
| Sign | 171/175, 169/172, 168/171, 168/171 | 98% every run |
| Term references | 28, 31, 32, 32 /32 | 88-100% |
| Category (raw, incl. label ambiguity) | 83-88% | |
| Non-EBITDA caps | 4, 4, 5, 2 /8 | small n: noise dominates |
| EBITDA caps (WEST/TRS/MEC, 14) | values 7/7 and basis 3/3 every run; type 12/14 then 7/14 x3 | type flips are representation (MEC shared 15% pool written as shared_pool in one run, pct_of_ebitda + cap_shared_with in three: same meaning) |

24 disagreements appear in every run (systematic) vs 49 that come and go (noise). Systematic and real: TRS 6.13 cap never
$240M (4/4 runs; one run gave both values), WEST two-limb cash cap mis-structured every run, WEST Indebtedness (d)(i)-(iii)
exclusions missed every run, MEC valuation rule (h) missed every run, WEST (l)(vii). The rest of the systematic list is the
cash_equivalents/permitted_investments label convention (16). Noise: nested (ii)/(iii) list items in WEST Permitted
Investments missed in 3/4 runs; cap formats; a few references.
Reading rule: a single-run change of <=3 parts found, <=5 pts category or 1-2 caps is within noise; decide on 3-run means.
EBITDA caps (all 5 deals, baseline run): 19 caps, values 12/12, basis 4/4, type 15/19 with every type miss a spec
limit (one cap field per part: shared + % cap, per-acquisition + pool, two limbs, per-covenant value).

## Multi-cap design (2026-10-12, task 2.1 incl. old 2.2)
A part now carries `caps: [ {type, value, from, to, during, covenant, pool, basis, quote} ]` instead of one cap.
- type: pct_of_ebitda, fixed_amount, per_transaction, lifetime_amount, greater_of / lesser_of (limbs "a|b"; "excess_over:N"
  for "the amount above N"), other.
- from/to: a date, or relative "24 months after Closing Date" (no date arithmetic by the model; code converts later).
- during: an event-bounded period ("Covenant Adjustment Period"). covenant: "all", or sections / ratio names; code maps
  ratio names to the covenants' sections after the run. pool: only for one limit shared by several clauses.
- Step 3 is told which covenant(s) each term serves ("This term is used by: Section 6.13 (Total Net Leverage Ratio)").
- Checks: limbs and values parse; % in (0,1]; windows parse and anchor on a defined term; a covenant-specific variant in
  the definition requires a cap scoped to that section; a pool has one limit per scope and at least two clauses; cash
  cap_on_total parts agree.
Results, 3 runs WEST/TRS/MEC: the material caps are right in every run: TRS $150M all + $240M for 6.13 + $10M threshold
(was wrong 4/4 runs before); WEST two-limb window ("greater of (cash above $20M, 100% EBITDA)" to 24 months after
Closing Date, then 100% EBITDA) scoped to 6.09(a) (was wrong 4/4); MEC $25M + $10M during the Covenant Adjustment Period.
Key caps present at term level: 8/12 in all 3 runs; the 4 misses are labelling (WEST Indebtedness (k) valuation rule
not called a cap; WEST Permitted Investments 0.5% value with words attached; MEC two CD limits typed fixed_amount vs
other). EBITDA caps: values 7/7, basis 3/3 every run; ROAD/CARS 5/5. Other metrics unchanged within noise (found 94-95%,
sign 96-99%, refs 94-97%). The per-part cap score (2-3/8) understates: limbs written on their own clause parts count
as mismatches; read the term-level number.

## Label conventions (2026-10-12, task 2.3, 3 runs WEST/TRS/MEC, $6.73)
Rules added to the library text Sonnet sees, with code enforcing what's deterministic:
- List items inside "Permitted Investments" / "Cash Equivalents" take that definition's category (code relabels).
- Excluded / deducted amounts: the item's own category with "-" when one exists; else the most specific excluded_*;
  excluded_other last.
- Valuation rules ("deemed to be the lesser of ...") are calculation_rule parts, not caps.
- Cap types: dollar limit per test period = fixed_amount; any other dollar limit = other; values are bare numbers
  (code rejects descriptions in the value).
Keys updated to the same conventions (WEST/TRS Permitted Investments items; WEST Indebtedness (k) valuation).

| | Multi-cap runs (before) | Label conventions (3 runs) |
|---|---|---|
| Parts found | 94-95% | 97-100% |
| Category | 85-88% | 94-97% |
| Sign | 96-99% | 97-98% |
| Term references | 94-97% | 88-100% (noise) |
| Caps, term level | 8/12 | 11/11 every run |
| EBITDA caps | values 7/7, basis 3/3 | unchanged |
Remaining category misses are genuine judgment calls, mostly which excluded_* label is most specific (TRS (b)(ix), (b)(x),
WEST (d)(vii), (d)(x)), plus noise (WEST interest coverage lead-in labels shuffled in 1 of 3 runs).

## Unclaused definitions (#18) and reference check (#36), 2026-10-12
- #18: `clauses.segments_unclaused` splits a definition with no clause markers at operative phrases (provisos,
  exclusions, conditions such as "that are" / "in each case" / "located in", arithmetic "plus / minus / net of"); every
  segment must be touched by some part's quote. Pointer definitions ("has the meaning assigned in Section x") skipped.
  Rule added: one part per amount and per condition / exclusion / proviso. Tested on 13 unclaused terms: keys clean;
  current output clean except the pointer case (now skipped). The MEC failure it targets came from the old merge bug, so
  today it is a regression guard.
- #36: a part in a debt / cash / interest / capex term whose clause IS another defined term of the same family (opens
  with it: "Indebtedness for borrowed money", "the aggregate principal amount of Indebtedness", "the Attributable
  Indebtedness") must reference it. First version (any mention) fired 19x on the keys (descriptive mentions such as
  "discount from the issuance of Indebtedness"); tightened to 0 on keys while still catching WEST Consolidated Debt (x)
  and MEC Attributable Indebtedness from earlier runs.
- Live run, all 7 deals (one run, $6.61): both checks fired (WEST Attributable Receivables Indebtedness ref; QUAD
  Consolidated Net Indebtedness uncovered "other than Liens ..." + 2 refs) and both were resolved by repair rounds.
  WEST/TRS/MEC: found 95%, category 97%, sign 98%, refs 94%, caps 11/11 term level; covenants/regimes/EBITDA unchanged.
- Next coverage gap found: nested sub-items under lettered list items ((b)(ii), (d)(i)-(iii)) are not in the code's
  clause list, so dropping them is silent; they are the recurring "found" misses across runs.

## Nested sub-items (#37) and source field (#19), 2026-10-12 ($13.06: 3+1 runs WEST/TRS/MEC, 1 run others)
#37 clause list (`clauses.enumerate_clauses` + `coverage`):
- "(b) (i) time deposits ... (ii) federal funds": a marker right after an accepted list marker opens a sub-list (was
  mistaken for a chained cross-reference like "clause (a)(vii)").
- A part for the parent "(d)" no longer covers its sub-items "(d)(i)..."; they must be returned.
- Condition lists stay with their parent ("money market funds that (i) comply ... (ii) are rated ..." = one item).
- Exclusion lists make the parent a leaf too ("(c) off-balance-sheet Indebtedness ...; provided that ... shall not
  include (i) ... (ii) ..." = the (c) amount + two exclusions).
- Verified: 3 keys clean; no EBITDA definition's clause list changed; 11 non-EBITDA terms changed, each checked.
Result: parts found 98-99% over 4 runs (was 94-97%); sign 97-99%; category 94-98%; covenants/regimes/EBITDA unchanged.
Remaining misses: TRS interest coverage measurement-period part; WEST cash second proviso (unnumbered text, #25).

#19 source field per part: income_statement / balance_sheet / cash_flow_statement / footnote / borrower_schedule /
agreement_fixed / not_disclosed / n/a. Checks: structural parts (references, ratio sides, qualifiers, rules, cap_on_total)
must be n/a and amount parts must not; flows can't come from the balance sheet and balances can't come from a flow
statement. No answer key exists for this, so it is scored for plausibility (category-level acceptable sources) and
stability, not accuracy: plausible 97-99% (all 7 deals); same source in every run for 89% of parts; flips are mostly
borrower_schedule vs footnote (genuinely ambiguous). Mix: ~11-13% of parts need the borrower or aren't public.

## Group 3: loan, events, calculation rules, deemed values, pricing (#20-#24), 2026-10-13
New abstract sections (spec v1-draft-2026-10-13): loan, events, reported, calculation_rules, deemed_values, pricing_grid.
All wired into build.py (run in parallel after step 3); review items tagged by step.
- loan.py (#20): parties, dates (agreement vs latest amendment vs closing), facilities, reporting deadlines, test
  period, equity cure. Code decides whether a cure exists (cue words in the text) and sets closing = agreement date
  when the definition says so. Fiscal year end falls back to the 10-K cover when the agreement is silent (WEST).
- events.py (#23): latest 10-Q, 10-K, 8-Ks. Code picks passages; Sonnet extracts events (regime ended, election,
  cure, amendment, waiver) linked to covenant/regime ids, plus the latest reported ratio per covenant. Code checks
  quotes against that filing and cross-checks each reported threshold against the thresholds the abstract allows on
  that date (#29): a mismatch goes to review. None fired on any of the 7 deals.
- calc_rules.py (#21): typed rules (pro forma, cost savings, GAAP / lease freeze, debt at principal, rounding, LCT,
  post-period events, ...) from Article I calculation sections, pro forma definitions and EBITDA pro forma paragraphs.
- deemed values (#22): code only (fixed wording "$X for the fiscal quarter ended <date>"); a deemed table on a
  missing schedule goes to review (CARS Schedule 1.01(c)).
- pricing.py (#24, grid only; incurrence tests later): levels, inclusive bounds, every margin column; code checks
  overlaps/gaps, metric is a defined term, margins fall with leverage; converts to fractions. A grid on a missing
  schedule goes to review (SRI Schedule 1.1(A)).
Results (Sonnet, 3 standalone runs each, scored vs keys): loan 51/51 x3; events 15/15 x3 (reported values +
thresholds, regime state); calc rules 9/9 x3; deemed values 5/5 deals x3; pricing 36/36 x3. Integrated build
(all 7, one run, $9.99 incl. step 3 terms): same scores (51/51, 15/15, 9/9 + 5/5, 36/36), schema-valid on all 7.
Standalone cost per 7-deal run: loan ~$1.6, events ~$0.35, calc ~$0.7, pricing ~$0.5.

## Group 4: robustness (#25-#28), 2026-10-13
- #25 unnumbered text (clauses.unnumbered_spans + decompose.check): every proviso ("provided (however/further) that")
  and every sentence no clause marker owns ("For purposes of", "avoidance of doubt", "Notwithstanding", "If ...",
  closing sentences after the clause list) must be touched by some part quote or cap quote; else the loop asks for it
  (limit -> cap on its part; condition / rule -> a part). Parenthetical provisos and deemed-value lists (#22) skipped.
  Keys: 0 fires on 18 spans (WEST/TRS/MEC v1 keys). Live (WEST/TRS/MEC): fired once (WEST projected-savings
  conditions), fixed by the loop, nothing left in review; WEST parts found 81/81.
  Finding: the last EBITDA "misses" were scorer artifacts, not drops: WEST (y) $1M proviso is held as a cap (old scorer
  counted only part quotes; score.py now counts cap quotes), ROAD's key quote is not in the definition, TRS's key
  quotes the tail of a sentence the model quotes from its start.
- #27 run-together headings (ingest structure.split_run_together_headings): a "x.y Title." heading inside a paragraph
  (same article as the last heading) splits the block. Only SRI has any (13 blocks, 24 headings); 0 candidates on the
  other 6. SRI sections 241 -> 265 incl. 1.3 Accounting Principles, 1.4 Currency, 1.5 Pro Forma, 1.6 LCT. Packet
  calc-rules filter also takes "Accounting Principles".
- #26 step-2 context outlet (covenants.run): needs_context / search_for like step 3, plus code fetches any section a
  flag says is missing ("Section 9.3(i) was not included"); cited clause is cut from the section. MEC: 9.3(i) fetched,
  the not-in-packet flag gone, covenants 2/2, schedule 32/32, regimes 9/9 unchanged ($0.43).
- #28 amendment stacking (stack.py): Sonnet reads a standalone amendment into typed ops (add / restate / delete
  definition, restate definition clause, restate section / clause, replace / insert / delete text, other document);
  code checks every text verbatim against the amendment, applies ops to the latest conformed copy (whitespace-tolerant
  location), re-runs ingest on the edited blocks (a normal bundle, provenance in report.json "stacked"), and flags
  failures and conflicts (target already defined, target changed by an earlier amendment). Amendment text read with
  the ingest extractor (page ids / footers removed).
  Test beds (instruction-style amendments we have): TRS 8th conformed + 9th -> 5/5 targets identical to the 10th
  conformed copy, no other text changed (350 -> 354 definitions). MEC 1st conformed + 2nd -> Section 8.15 matches the
  3rd conformed copy except "Permitted" x2, which the 3rd amendment's blackline strikes (18 struck chars): correct
  pre-3rd state. Stacking the 9th twice flagged all 5 ops as conflicts. Cost ~$0.10 per amendment.
  Not yet: build.py reads bundles by the CURRENT map; pointing it at a stacked bundle is part of #33.

## Group 5: review and sign-off (#29-#31), 2026-10-13
- #29 done inside events (#23).
- #31 sign-off record (signoff.py, <B>.signoff.json next to the abstract): draft -> verified -> signed_off. Every flag
  and review item has a stable key (hash of what it is about), so resolutions survive rebuilds; resolve = accept /
  correct (needs the corrected value) / reject / defer, by, date, note. verify needs every item resolved, none
  deferred, no schema errors; records the content hash. sign needs verified + unchanged hash + a different person.
  Any content change afterwards drops the status to draft (logged). build.py takes status from the record.
- #30 GPT second-model check (gpt_review.py): code turns the abstract into numbered claims (covenant rows, regimes,
  every term part with sign / ref / caps, loan fields, pricing levels); Codex (`codex exec --output-schema`, read-only
  sandbox, agreement.txt in its folder to search) returns agree / disagree / not_in_text per claim with a verbatim
  quote. Code keeps a disagreement only if its evidence is verbatim in the agreement; verified disagreements and
  "missed" items go to review (step "gpt"). GPT never edits the abstract. Runs on Devin's machine with his ChatGPT
  sign-in (device-code login).
- Build guard: if any model call fails (e.g. usage limit), the build keeps the previous abstract and writes the
  partial one to <B>.abstract.failed.json (a limit hit on 2026-10-13 had overwritten WEST and MEC).

## GPT findings fixed, 2026-10-13
First full GPT run (#30, 709 claims): 658 agree, 48 verified disagreements. Triage:
- Clause enumerator (24 disagreements, WEST CIE / WEST Indebtedness / CARS EBITDA): a roman list after the LAST
  letter that starts a new sentence or follows "minus" / "less" (not "less than") / "but excluding" / "for the
  avoidance of doubt" belongs to the whole definition: labelled excl(i).. instead of (d)(i)... Diff over all 143
  family terms: exactly those 3 changed (CARS (u) now its own part). WEST v1 key relabelled (18 parts, noted in key).
- Definition boundary (QUAD): “Total | Net Leverage Ratio” split across blocks hid the definition start, so the
  pointer “Total Leverage Ratio” swallowed the Total Net Leverage Ratio text and the covenant was decomposed as NET
  debt / EBITDA. Ingest now joins a split opening quote (fires only on QUAD); a pointer definition ("has the meaning
  assigned ... in Section 6.11(a)") is decomposed from the cited clause: Consolidated Total Indebtedness / EBITDA.
- Duplicate parts under two labels after relabelling (WEST CIE (iv)/(vii), Permitted Investments parents): code
  keeps the part with the code's clause label when category, sign and opening words match (non-ratio terms).
- Prompt: abstract conventions stated (ratio sides, cap / condition parts, labels, schedule row starts, relief in
  standard table, absence claims); cap values checked strictly (keeps the SRI catch); "missed" limited to 8 items
  that change a covenant result (still loose: kept out of review).
Second run (715 claims): 704 agree, 6 verified disagreements, 1 not found (was 658 / 48). Remaining: WEST minimum
liquidity and anti-cash-hoarding covenants end with the relief period (abstract: open-ended) - likely real; SRI
EBITDA (b)(ii) relative cap - real (spec gap); 3 label / reference nits. GPT also varies run to run: MEC
Indebtedness (h) Hedge Agreement reference was flagged in run 1b, not in run 2 (the v1 key agrees with GPT).

## Spec v1.1 (2026-10-13): from the Claude-vs-GPT adjudication
Adjudicated against the text, GPT run 2's 8 points: GPT right on 6 (WEST liquidity first test date, WEST
anti-cash-hoarding Credit Event tests, WEST ICR spurious Test Period ref, CARS / QUAD labels, SRI relative cap,
MEC (h) reference); Claude right on 2 (WEST liquidity / anti-cash-hoarding end with the relief period: the
covenants carry applies_during, which the GPT claims omitted - claim generator fixed).
Spec changes (SPEC_VERSION v1.1-2026-10-13):
- Covenant rows: start = the FIRST TEST DATE the row applies to (code: monthly / quarterly starts must be test
  dates). Covenant gets event_tests (credit_event, acquisition, restricted_payment, incurrence, other) and skip_when.
- Caps: type "relative" = a limit equal to another clause's or defined term's amount (value "(a)(v)").
- Terms: ref_term only for a defined term the part's own clause names (plural allowed; 0 fires on keys, 5 on
  current output incl. both GPT catches). A label that starts with a clause id must quote that clause; opening text
  is "base" / "lead-in". Pointer-cited clause names: longest match first.
- GPT claims now state applies_during, event tests and skip conditions.
Rebuild (all 7): covenants / schedules / regimes unchanged vs keys; WEST 80/81 found, TRS 56/57, MEC 44/45.
GPT run 3 (708 claims): 698 agree, 10 disagreements; all 8 earlier points resolved. New, checked: MEC first row
starts 2023-09-30 but "Closing Date through ..." makes June 30, 2023 the first test (GPT right; the new start rule
made the model pick a test date and it picked the wrong one); CARS (s) cap is the replaced earnings, not 0 (GPT
right); WEST Permitted Investments (h) 0.5% of total assets lacks its base (GPT right; spec gap). Not yet checked:
ROAD (a)(xi) 25% step-down vs cap, CARS (x) +/- sign, QUAD (vii) cap scope, SRI per-category nuance, MEC capex name.
MEC 2026 rows: representation dispute (relief levels in standard table), as before.

## v1.1 follow-ups (2026-10-13 evening)
- Closing-date rows: build.py (after the loan step, which knows the Closing Date) sets a row "from the Closing Date" to
  the first quarter end on/after it, and resolves "n quarters after the Closing Date" to a date (n-th FULL quarter
  when the row says "full"); each change logged in loan_code_notes. MEC first rows 2023-09-30 -> 2023-06-30; WEST
  6.09(a) rows dated 2022-12-31 / 2024-03-31 / 2024-06-30. (A cover-page "dated as of" fallback was rejected: on a
  conformed copy it is the amendment's date.)
- Caps: pct_of_total_assets, pct_of_other (base in note), relative may be a short description, step_down (an
  add-back that shrinks per period), cap on some clauses of a referenced term = split the part by ref_clauses.
  After rebuild: WEST PI (h) pct_of_total_assets 0.005; CARS (s) relative 'earnings the proceeds replace'; ROAD
  (a)(xi) step_down 0.25 + lesser_of; QUAD (vii) split, $10M only on Transaction Charges clause (ii); SRI (b)(ii)
  relative to (a)(v).
- Scorer: every quarter end 2022-2030 (was 2025-2028); uses code-resolved dates; respects the key's own start date.
  WEST key fix: anti-cash-hoarding applies from 2024-02-29. Results: WEST 138/139 (Sep 30 2022: the text starts
  testing that quarter but its levels only cover FULL quarters after closing - text gap, key assumes 4.50), ROAD
  46/46, TRS 50/50, CARS 54/54, MEC 62/62.
- Last 5 GPT points adjudicated: ROAD step-down (GPT right, spec gap -> step_down), QUAD (vii) scope (GPT right, model
  error -> split rule), MEC name (GPT right, cosmetic), CARS (x) sign (equivalent), SRI per-category (nuance).
- Open: QUAD (vii) cap scope. Code checks added (a cap quoted from a referenced definition that names "clause (ii)"
  must sit on a part with ref_clauses (ii); a cap-driven split must take every clause), 0 fires on keys. But the
  model met the check by dropping the cap instead of splitting. Put in review with the correct answer; next fix: code
  makes the split itself (deterministic: the cap's clause list is in the text).
