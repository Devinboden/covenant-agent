# Intake: pre-chewing a document for the spec agent

Everything between "PDF lands in S3" and "spec agent starts". Code only; a small model is a
switched-off fallback for anything code can't decide.

| Step | What | Module |
|---|---|---|
| 1 Extract | PDF -> clean blocks. Struck (deleted) text detected from drawn strike lines and dropped from the live text; page numbers and repeated headers removed; paragraphs split by page breaks rejoined; bordered tables rebuilt as rows | `extract.py` |
| 2 Structure | Sections (table of contents ignored), definitions, terms defined in passing, cross-references | `structure.py` |
| 3 Locate | Financial covenant section(s); falls back to ratio-titled sections; adds neighbouring tests (e.g. capex) | `locate.py` |
| 4 Classify | Agreement / amendment (+ full conformed agreement?, blackline?) / 10-Q / 10-K / 8-K / certificate | `locate.py` |
| 5 Packet | Covenant sections, the definitions they use (calculation path), paragraphs elsewhere that use the ratios (incurrence tests, pricing, cure), loan basics (cover, reporting, cure, fiscal year). Everything keeps page and block ids | `packet.py` |
| Report | Doc type, extraction stats, covenant location + confidence, missing schedules/exhibits, amendments recited, undecided items | `run.py` |

```
python -m covenant_engine.tools.html_to_pdf              # EDGAR HTML -> PDF (data/pdf/)
python -m covenant_engine.ingest.run --all-current       # bundles in data/intake/<borrower>/<file>/
python -m covenant_engine.ingest.coverage                # packet vs answer-key evidence
```

Bundle files: `report.json`, `packet.md` (what the agent reads first), `packet.json`, `sections.json`,
`definitions.json`, `blocks.jsonl` (page/bbox per block, for citations), `text_live.txt` (current agreement,
what quotes are checked against), `text_raw.txt` (with deleted text, for blackline history).

## Exception path: code detects -> Haiku proposes -> code verifies and executes
`exceptions.py`. Off by default; `--model` turns it on. Haiku (`claude -p --model haiku --json-schema`, swap for
Bedrock later) never writes a final value: it returns a judgment plus a pointer into the text. Code checks the
pointer, then takes the action with its own rules. Disagreement or a failed check -> needs review.

| Code detects | Haiku proposes | Code verifies | Code executes |
|---|---|---|---|
| Classification undecided / low | type + the cover phrase that shows it | phrase is on the cover | classifies the phrase with its own patterns (must agree); sets full-agreement / blackline from its own counts |
| Covenant section not found / fallback only | section ids + a quote from each | ids exist, quote in section, test language | adopts verified ids; if code had an answer, they must match |
| MISSED DEFINITION USED IN PACKET | is it a definition + where it starts | start found, opens with the quoted term | cuts the text to the next definition/heading, rebuilds cross-refs and packet |

Lesson baked in: the model decides *whether* and *where it starts*; code measures *where it ends*. Haiku stopped
long definitions early at a sub-clause (2 of 6) while quoting real text, which a quote check alone can't catch.

Benchmark (`python -m covenant_engine.ingest.test_exceptions`, 3 runs 2026-10-11/12 + 1 run after the propose/execute refactor; identical results each run):
- Classify 45 filings: 45/45 correct and verified in all 3 runs (135/135), $0.04-0.09 per run, ~7s each.
- Covenant sections, blind (no code hint), 5 agreements: 5/5 identical to code in all 3 runs (15/15).
- CARS with the mid-paragraph bug injected (`run.py --inject-midblock-bug --model`): loud check flagged 5 missed
  definitions incl. Indebtedness (a 6th, an "A or B means" alias, is now caught by code); Haiku + code repaired all 5,
  text identical to the correct code capture in all 3 runs (15/15); Haiku stopped the same 2 long definitions early every
  run, so the code-measured end boundary is doing real work; $0.005-0.01 per run.
- Normal runs: TRS covenant fallback confirmed by Haiku; 2 real missed definitions repaired (ROAD Revolving Credit
  Commitments, MEC Loan).

AWS: `handler.py` (S3 -> EventBridge -> Lambda container -> bundle in S3 -> `IntakeReady` event -> AgentCore).

## Results on the 5 agreements (2026-10-10)
- Extraction fidelity: 653/655 answer-key quotes found in the PDF text; the 2 misses are HTML page numbers inside the old quotes.
- Classification: 45/45 filings.
- Covenant section found for all 5 (TRS by fallback, medium confidence; MEC also picks up 9.14 capex).
- Packet = 13-29% of the agreement and holds 82-96% of the answer-key evidence (maintenance covenants and their terms ~95-100%; misses are scattered incurrence gates and loan facts, reachable through the agent's read tools).

## Holdout: Quad/Graphics (QUAD) and Stoneridge (SRI), 2026-10-12
Full packs (agreement + two 2026 10-Qs + 2025 10-K), run with rules untouched first.
- QUAD passed untouched: covenant section 6.11 found (high), Haiku repaired 1 missed definition, packet 15% of agreement,
  thresholds match the 10-Q (max total leverage 3.50x).
- SRI failed LOUD (needs_review, not silent): covenants are numbered three levels deep (13.2.16) and the printed
  headings are offset +5 from the agreement's own cross-references (auto-numbering continued from the amendment).
  Fixed in code, generally: 3-level numbering; `reconcile_numbering` (8% of cited sections resolved as printed -> 73%
  after offset 5; 225/325 bracketed titles confirm); test-language window widened (code missed 8.2.17 interest
  coverage, Haiku caught it and the disagreement went to review). After fixes: 8.2.16 + 8.2.17 found, confirmed by
  Haiku, schedules match the 10-Q relief terms (leverage 3.75 -> 6.75 -> 4.00x; coverage 2.50 -> 1.60 -> 2.50x).
  SRI is no longer a clean holdout for these fixes; QUAD still is.
- Silent-miss audit (no answer key): every defined term the covenant sections use has a complete definition in the packet.
- Classification of the 8 holdout docs: code 8/8, Haiku+code 8/8. Original 5 deals: no regressions; numbering check
  does not fire on them (89-95% of cross-references resolve as printed).
