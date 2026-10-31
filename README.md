# Covenant Agent

Turns a commercial credit agreement (PDF) into a covenant abstract a bank can rely on: every financial covenant, its
threshold schedule and the regimes that change it (relief periods, step-ups, elections), every defined term the test
depends on broken into parts with caps and quotes, plus the loan facts, events from the borrower's filings,
calculation rules and the pricing grid. Every value carries the exact words it came from, and nothing is final until
a person signs it off.

Status: the abstraction engine is built and measured. Next: the MCP server and MCP App interface, then the AgentCore
agent, then deployment on Bedrock / AgentCore.

## How it works
The model fills fixed structures; code decides everything deterministic, checks everything, and asks the model to
redo only what failed. What can't be resolved goes to a person.

1. **Ingest** (`covenant_engine.ingest`): live text from the PDF (struck text in blacklines dropped), sections,
   definitions, cross-references; repairs common filing quirks; a reading packet of the covenant sections and every
   definition they use. If no financial covenant section is found, the run stops for a person.
2. **Covenants and regimes**: metric, direction, frequency and event tests; dated schedules; relief periods,
   step-ups, elections. Code checks quotes, dates (first test dates, relative dates resolved from the Closing Date)
   and overlaps.
3. **Defined terms**: ratio, EBITDA, debt, cash and interest definitions decomposed into parts (clause, category,
   sign, references, caps, data source). Code enumerates every clause and every unnumbered proviso and requires each
   to be covered; checks quotes, references and cap shapes; up to two repair rounds.
4. **Loan, events, calculation rules, pricing** (in parallel); events come from the 10-Q / 10-K / 8-Ks and are
   cross-checked against the thresholds.
5. **Second-model check** (optional): GPT verifies each claim against the agreement; only disagreements backed by a
   verbatim quote count.
6. **Sign-off**: draft -> verified -> signed off. Every flag and review item must be decided, the signer must differ
   from the verifier, and any later change drops the abstract back to draft.

## Results (spec v1.1, five development deals with answer keys)
| | Score |
|---|---|
| Covenant schedules, every quarter 2022-2030 | 350 / 351 test dates |
| Regime fields | 27 / 27 |
| Loan facts | 51 / 51 |
| Events and reported results | 15 / 15 |
| Calculation rules / deemed values | 9 / 9, 5 / 5 |
| Pricing grids | 36 / 36 |

Deals: TriMas, Cars.com, Construction Partners, Westrock Coffee, Mayville Engineering; holdouts Quad/Graphics,
Stoneridge; new deals Crane NXT and Ultra Clean. All inputs are public SEC EDGAR filings (`data/manifest.csv`).
The single schedule miss is a gap in the agreement itself (a first test date its levels don't cover), sent to review.
These are scores on the deals the rules were developed against, not a holdout accuracy.

## Run it
```bash
pip install -e ".[dev,render]" && playwright install chromium
SEC_USER_AGENT="Your Name you@example.com" python scripts/fetch_filings.py TRS   # download, render, ingest
python -m covenant_engine.abstract.new_deal TRS data/pdf/TRS/amendment_10th-amendment-2026-03-conformed.pdf
python -m covenant_engine.abstract.signoff status TRS
pytest -q                    # fast regression tests on the stored abstracts (no model calls)
pytest -m live               # end-to-end rebuild against the keys (calls models)
```
Model calls use the local `claude -p` CLI today (`covenant_engine/model.py`); Bedrock comes at deployment.

## MCP server
The engine's tools over MCP (Python SDK 2.x), local first:
```bash
pip install -e ".[mcp]"
COVENANT_USER="Reviewer 1" covenant-mcp            # stdio; --http for streamable HTTP on 127.0.0.1:8000/mcp
```
| Tool | Does |
|---|---|
| `list_deals`, `get_abstract`, `list_review_items`, `signoff_status` | read (abstract summary, any section, open items with evidence) |
| `start_deal`, `run_status` | abstract a PDF under `data/pdf/<TICKER>/` in the background and follow progress |
| `start_abstraction`, `find_credit_agreements` | "start the covenant abstraction for Cars.com": company name or ticker -> SEC EDGAR -> its credit agreements among the exhibits to its 8-K / 10-Q / 10-K filings (typed by code from the cover) -> code picks the newest full-text agreement -> download, render, background abstraction. `find_credit_agreements` lists the options. Needs `SEC_USER_AGENT`, Playwright (`.[render]`) and the model backend; refuses companies already abstracted |
| `resolve_item`, `verify`, `sign_off` | the person answers a form (MCP elicitation); the model can only propose |

| `open_review` | shows the review screen (MCP App view): open items with evidence, covenant schedules, the agreement page with the quote highlighted |
| `review_page`, `review_decide`, `review_verify`, `review_sign` | screen only (`_meta.ui.visibility: ["app"]`): the model never sees them, so a decision is a person's click |

Who is acting comes from the connection (the authenticated principal, or `COVENANT_USER` locally), never from a tool
argument, so four eyes holds through the agent. With no person to answer (a headless client), the form is cancelled
and nothing is recorded.

### Try the review screen locally
```bash
pip install -e ".[mcp]"
SEC_USER_AGENT="Your Name you@example.com" python scripts/fetch_filings.py MEC --no-ingest   # optional: PDF pages
COVENANT_USER="Reviewer 1" covenant-mcp --http --dev-host                 # open http://127.0.0.1:8000/
COVENANT_USER="Reviewer 2" covenant-mcp --http --dev-host --port 8001     # a second reviewer, for sign-off
```
The dev host is a minimal MCP Apps host (`ui-src/dev_host.js`): it calls `open_review`, renders the screen in a sandboxed
iframe and logs every call the screen makes. Without the PDF the screen works but shows no pages. The view is built
from `ui-src/` with `npm install && npm run build:ui`; the built files in `src/covenant_engine/ui/` are committed.

### Hosted demo (Fly)
`covenant_engine.hosted` serves one MCP endpoint per reviewer at `https://<app>.fly.dev/r/<secret>/mcp`, for trying
the review screen in claude.ai and ChatGPT as custom connectors. The secret in the URL is the access control and the
identity (four eyes still applies); secrets are made on first boot in `/data/home/reviewers.json` and never logged.
`.github/workflows/fly.yml` runs the tests and deploys on push to main once the repo has a `FLY_API_TOKEN` secret
(app-scoped deploy token); with an `SEC_USER_AGENT` secret it also renders each deal's agreement from EDGAR for the
page images. Real sign-in (Entra / Cognito) replaces the URL secrets at the AgentCore step.

## Layout
- `src/covenant_engine/` ingest, abstract (steps, checks, scorers, sign-off), core (shared library parts), config,
  model gate, store
- `data/` answer keys (`keys_v1/`, `abstracts/`), filing manifest, stored abstracts used by the tests
- `docs/` spec design and the measured build history
- `tests/` regression and rule tests

Not affiliated with any lender, borrower or loan-system vendor. Public data only.
