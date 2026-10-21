# Abstract spec: design

The spec is the blank, rule-bearing form every covenant abstract fills in. One spec; one abstract per deal; one block per covenant. Agents pick library parts; code assembles and checks.

## Sections
| # | Section | Library vs custom |
|---|---|---|
| 1 | Header: borrower, as-of, sources, status (draft > verified > signed_off) | Fixed form |
| 2 | Loan: parties, dates, facilities, amendments, reporting deadlines, test period, cure, relief/election rules | Fixed form + small pattern library (relief, step-up, holiday, cure) |
| 3 | Covenants: one block each; kind (ratio / level / incurrence), type, direction, thresholds by date, overlays, formula -> terms | Library (kinds + common types); values custom |
| 4 | Terms: shared definitions (EBITDA, debt, cash netting, interest) as components with caps | Library parts (component categories, cap types), custom assembly; oddities = `custom` component with quote |
| 5 | Events: dated facts (relief ended, step-up elected, holiday, cure, acquisition) | Library of event types |
| 6 | Flags: judgment calls + resolution (decision, by, date) | Mostly custom |

Out of the spec: replay (one quarter's numbers) becomes the test-inputs spec; lookups fold into Terms (type `lookup`).

## How agents write it
- One small strict tool per library part (add_covenant, add_component, add_event, mark_unmapped ...), enums for every library choice, readable IDs (e.g. `ebitda.addback.restructuring`). Structured outputs enforce shape at generation.
- Every part carries value + verbatim quote + section.
- `mark_unmapped` is the escape hatch: never force a clause into the wrong part.
- Code compiles parts into the abstract (YAML), the Excel Rules sheet, and cHene records.

## Validation (each data point gets a status)
1 Shape (spec) · 2 Evidence (quote in live text) · 3 Value-in-quote · 4 Internal consistency (refs resolve, dates continuous, units) · 5 Replay · 6 Second model (GPT) · 7 Human.
Status per data point: verified by code / verified by model only / needs a person.

## Routing failures (cheapest fixer)
| Failure | Goes to |
|---|---|
| Shape | Prevented by structured outputs; else the agent |
| Quote missing / value not in quote | Agent, one retry, then flag |
| Doesn't fit spec | Unmapped -> person (and maybe the library) |
| Replay mismatch | Flag -> analyst |
| Reviewer dispute | Analyst |
Track retries per abstract as a quality metric.

## Seen in the 5 deals (spec must cover)
Thresholds relative to another covenant (cap - 0.25x); event-triggered overlays with count/spacing/duration; relief ending on a delivery, open-ended; early-exit override; per-use term parameters; lifetime and per-deal add-back caps (ledger); greater-of / lesser-of caps; secured vs unsecured thresholds; covenant type change over time; pricing grid (candidate kind).
