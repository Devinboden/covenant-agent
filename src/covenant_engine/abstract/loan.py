"""Loan section (#20): parties, dates, facilities, fiscal calendar, reporting deadlines, test period, equity cure.

Method 1 again: Sonnet fills a fixed structure; code decides what it can and checks the rest.
- Code decides whether an equity cure EXISTS (cure wording in the agreement); the model extracts its terms. A model that
  says otherwise is a problem, not a vote.
- Code assembles the input: the packet's cover + loan-basics sections, the section that holds the cure wording (even if
  its title doesn't say "cure"), and the definitions loan facts live in (Closing Date, Maturity Date, Fiscal Year ...).
- Checks: quotes verbatim; dates / days / amounts parse; cure covenants exist in step 2; cure limits typed.

Usage: python -m covenant_engine.abstract.loan WEST [--model sonnet]
"""
import argparse
import json
import re

from ..ingest.exceptions import call_model, squash
from ..core.decompose_term import bundle_dir

CURE_CUE = re.compile(r"Specified Equity Contribution|Cure Amount|Equity Cure|[Rr]ight to [Cc]ure|[Cc]ure [Rr]ight|"
                      r"Cure Expiration|Equity Contribution Amount")
DEF_CUE = re.compile(r"Closing Date|Effective Date|Maturity Date|Termination Date|Fiscal Year|Fiscal Quarter|"
                     r"Commitment|Test Period|Reference Period|Specified Equity|Cure", re.I)

DATE = {"type": "object", "additionalProperties": False, "required": ["value", "quote"],
        "properties": {"value": {"type": "string"}, "quote": {"type": "string"}}}
LIMIT = {"type": "object", "additionalProperties": False, "required": ["type", "value", "quote"],
         "properties": {"type": {"type": "string", "enum": ["max_total", "max_per_fiscal_year", "cure_free_quarters_in_four",
                                                            "max_consecutive_quarters", "amount_limited_to_shortfall",
                                                            "other"]},
                        "value": {"type": "string"}, "quote": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False,
          "required": ["borrower", "agent", "agreement_date", "latest_amendment_date", "closing_date", "fiscal_year_end", "facilities", "reporting",
                       "test_period", "equity_cure", "needs_context", "flags"],
          "properties": {
              "borrower": {"type": "string"}, "agent": {"type": "string"},
              "agreement_date": DATE, "latest_amendment_date": DATE, "closing_date": DATE,
              "fiscal_year_end": {"type": "object", "additionalProperties": False, "required": ["value", "quote"],
                                  "properties": {"value": {"type": "string"}, "quote": {"type": "string"}}},
              "facilities": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False,
                  "required": ["type", "commitment", "maturity", "quote"],
                  "properties": {"type": {"type": "string", "enum": ["revolver", "term_loan_a", "term_loan_b", "term_loan",
                                                                      "delayed_draw_term_loan", "incremental", "other"]},
                                 "commitment": {"type": "string"}, "maturity": {"type": "string"},
                                 "quote": {"type": "string"}}}},
              "reporting": {"type": "object", "additionalProperties": False,
                            "required": ["annual_days", "quarterly_days", "compliance_certificate", "quote_annual",
                                         "quote_quarterly", "quote_certificate"],
                            "properties": {"annual_days": {"type": "string"}, "quarterly_days": {"type": "string"},
                                           "compliance_certificate": {"type": "string", "enum": [
                                               "with_financials", "days_after_financials", "other", "not_stated"]},
                                           "quote_annual": {"type": "string"}, "quote_quarterly": {"type": "string"},
                                           "quote_certificate": {"type": "string"}}},
              "test_period": {"type": "object", "additionalProperties": False, "required": ["basis", "quote"],
                              "properties": {"basis": {"type": "string", "enum": ["trailing_four_quarters",
                                                                                   "fiscal_quarter", "other"]},
                                             "quote": {"type": "string"}}},
              "equity_cure": {"type": "object", "additionalProperties": False,
                              "required": ["present", "section", "covenants", "effect", "deadline_count", "deadline_unit",
                                           "deadline_anchor", "limits", "prepayment_required", "quote"],
                              "properties": {"present": {"type": "boolean"}, "section": {"type": "string"},
                                             "covenants": {"type": "array", "items": {"type": "string"}},
                                             "effect": {"type": "string", "enum": ["add_to_ebitda", "reduce_debt",
                                                                                  "add_to_ebitda_or_reduce_debt", "other",
                                                                                  "n/a"]},
                                             "deadline_count": {"type": "string"},
                                             "deadline_unit": {"type": "string", "enum": ["business_days", "days", "n/a"]},
                                             "deadline_anchor": {"type": "string", "enum": [
                                                 "financials_due", "financials_delivered", "quarter_end", "other", "n/a"]},
                                             "limits": {"type": "array", "items": LIMIT},
                                             "prepayment_required": {"type": "boolean"},
                                             "quote": {"type": "string"}}},
              "needs_context": {"type": "array", "items": {"type": "string"}},
              "flags": {"type": "array", "items": {"type": "string"}}}}

SYSTEM = ("You extract loan facts from commercial credit agreements into a fixed structure. Work only from the text given. "
          "Every quote must be copied exactly from the text. Never compute dates or amounts; copy them. Leave a field \"\" "
          "when the text doesn't state it (e.g. a Closing Date defined only by conditions).")
RULES = """- borrower: the borrower's name as written. agent: the administrative agent's name.
- agreement_date: the date of the CREDIT AGREEMENT itself (in an amendment: "Reference is made to that certain ... Credit
  Agreement, dated as of ..."), not the amendment's date. latest_amendment_date: the date of the most recent amendment in
  the text, else "". closing_date: the Closing Date only if the text states the date; YYYY-MM-DD or "".
- fiscal_year_end: MM-DD.
- facilities: each facility in the CURRENT agreement (as amended): type, commitment ("250000000"; "" if not stated in
  the text), maturity (YYYY-MM-DD or ""), quote.
- reporting: days after fiscal year end for annual financials, after quarter end for quarterly ("90", "45");
  compliance_certificate: when it is due.
- test_period: how covenant periods are measured.
- equity_cure: {cure_note}. If present: section, covenants it can cure (section refs, e.g. "6.09"), effect,
  deadline (count + unit + anchor), every limit (type + value + quote: total cures, per fiscal year, cure-free quarters in
  any four, consecutive quarters, amount limited to the shortfall), whether proceeds must prepay loans. If absent:
  present false, other fields "", [], "n/a" or false.
- needs_context: definitions or "Section x.y" you need but were not given. flags: anything a reviewer must see."""
PROMPT = """Extract the loan facts.

{rules}

COVENANT SECTIONS IN THIS AGREEMENT: {covenants}

TEXT:
<<<
{text}
>>>"""
REPAIR = """Your previous extraction (below) failed these code checks. Return the complete corrected extraction.

PROBLEMS:
{problems}

PREVIOUS EXTRACTION:
{previous}

{extra}{rules}

COVENANT SECTIONS IN THIS AGREEMENT: {covenants}

TEXT:
<<<
{text}
>>>"""


def gather(borrower):
    """Code assembles the loan text: cover + loan-basics sections from the packet, the section holding cure wording,
    and the definitions loan facts live in."""
    d = bundle_dir(borrower)
    P = json.loads((d / "packet.json").read_text(encoding="utf-8"))
    S = json.loads((d / "sections.json").read_text(encoding="utf-8"))
    blocks = [json.loads(l) for l in (d / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
    defs = json.loads((d / "definitions.json").read_text(encoding="utf-8"))["defined"]
    parts, have = [], set()
    for c in P:
        if c["kind"] in ("preamble", "loan_section"):
            parts.append(f"[{'Cover' if c['kind'] == 'preamble' else 'Section ' + c['id'] + ' ' + (c.get('title') or '')}]\n{c['text']}")
            have.add(c["id"])
    cure_secs = []
    for s in S:
        txt = "\n".join(blocks[k]["text_live"] for k in range(s["start"], s["end"]) if blocks[k]["text_live"])
        if s["id"] not in have and CURE_CUE.search(txt) and not s["id"].startswith("1.1") and len(txt) < 20000:
            cure_secs.append(s["id"])
            parts.append(f"[Section {s['id']} {s.get('title') or ''}]\n{txt}")
    for name, v in defs.items():
        if DEF_CUE.search(name) and len(v["text"]) < 4000:
            parts.append(f"[Definition] {v['text']}")
    live = (d / "text_live.txt").read_text(encoding="utf-8")
    return "\n\n".join(parts), squash(live), bool(CURE_CUE.search(live)), cure_secs, defs, S, blocks


def _num(x):
    try:
        return float(str(x).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def check(out, live_sq, cure_in_text, covenant_refs):
    P = []

    def q(where, s):
        if s and squash(s)[:80] not in live_sq:
            P.append(f"{where}: quote not found verbatim in the agreement: {s[:70]!r}")

    for k in ("agreement_date", "latest_amendment_date", "closing_date"):
        v = out[k]["value"]
        if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            P.append(f"{k} {v!r}: YYYY-MM-DD or ''")
        q(k, out[k]["quote"])
    fy = out["fiscal_year_end"]["value"]
    if fy and not re.fullmatch(r"\d{2}-\d{2}", fy):
        P.append(f"fiscal_year_end {fy!r}: MM-DD")
    q("fiscal_year_end", out["fiscal_year_end"]["quote"])
    for f in out["facilities"]:
        if f["commitment"] and _num(f["commitment"]) is None:
            P.append(f"facility {f['type']}: commitment {f['commitment']!r} must be a bare number")
        if f["maturity"] and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", f["maturity"]):
            P.append(f"facility {f['type']}: maturity {f['maturity']!r}: YYYY-MM-DD or ''")
        q(f"facility {f['type']}", f["quote"])
    r = out["reporting"]
    for k in ("annual_days", "quarterly_days"):
        if r[k] and _num(r[k]) is None:
            P.append(f"reporting {k} {r[k]!r} must be a number of days")
    for k in ("quote_annual", "quote_quarterly", "quote_certificate"):
        q(f"reporting {k}", r[k])
    q("test_period", out["test_period"]["quote"])
    c = out["equity_cure"]
    if cure_in_text and not c["present"]:
        P.append("the agreement contains equity cure wording (e.g. 'Cure Amount' / 'Specified Equity Contribution' / "
                 "'Right to Cure'): extract the cure, or flag why it is not an equity cure")
    if not cure_in_text and c["present"]:
        P.append("no equity cure wording found in the agreement: present must be false unless you quote it")
    if c["present"]:
        q("equity_cure", c["quote"])
        if _num(c["deadline_count"]) is None or c["deadline_unit"] == "n/a":
            P.append("equity_cure: give the deadline count and unit")
        refs = {x.split("(")[0] for x in covenant_refs}
        for cov in c["covenants"]:
            base = re.sub(r"^(Section\s+)", "", cov.strip(), flags=re.I).split("(")[0]
            if base not in refs and not any(r.startswith(base + ".") or base.startswith(r) for r in refs):
                P.append(f"equity_cure covenant {cov!r} is not one of the covenant sections {sorted(refs)}")
        for lim in c["limits"]:
            if lim["type"] in ("max_total", "max_per_fiscal_year", "cure_free_quarters_in_four", "max_consecutive_quarters") \
                    and _num(lim["value"]) is None:
                P.append(f"equity_cure limit {lim['type']}: value {lim['value']!r} must be a number")
            q(f"equity_cure limit {lim['type']}", lim["quote"])
    return P


def run(borrower, covenant_refs, model="sonnet", max_repairs=2):
    text, live_sq, cure_in_text, cure_secs, defs, S, blocks = gather(borrower)
    cure_note = ("the agreement CONTAINS equity cure wording (code found it" + (f" in Section(s) {', '.join(cure_secs)}" if cure_secs else "") +
                 "): extract it" if cure_in_text else "code found NO equity cure wording in this agreement: present false unless you can quote one")
    rules = RULES.replace("{cure_note}", cure_note)
    covs = ", ".join(covenant_refs) or "(none)"
    trace = []
    r = call_model(SYSTEM, PROMPT.format(rules=rules, covenants=covs, text=text), SCHEMA, model=model, timeout=900)
    trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
    if not r["ok"]:
        return {"error": r.get("error"), "trace": trace}
    out = r["output"]
    for _ in range(max_repairs):
        probs = check(out, live_sq, cure_in_text, covenant_refs)
        extra = []
        for n in out["needs_context"]:
            m = re.match(r"(?:Section\s+)?(\d+\.\d+[\d.]*)", n.strip(), re.I)
            if m:
                s = next((s for s in S if s["id"] == m.group(1)), None)
                if s:
                    extra.append(f"[Section {s['id']}]\n" + "\n".join(blocks[k]["text_live"] for k in range(s["start"], s["end"]) if blocks[k]["text_live"])[:8000])
            elif n.strip() in defs:
                extra.append(f"[Definition] {defs[n.strip()]['text'][:4000]}")
        trace[-1]["problems"] = probs
        trace[-1]["context_fetched"] = len(extra)
        if not probs and not extra:
            break
        ex = ("ADDITIONAL CONTEXT YOU ASKED FOR:\n<<<\n" + "\n\n".join(extra) + "\n>>>\n\n") if extra else ""
        r = call_model(SYSTEM, REPAIR.format(problems="\n".join(f"- {p}" for p in probs) or "- (none: review the added context)",
                                             previous=json.dumps(out), extra=ex, rules=rules, covenants=covs, text=text),
                       SCHEMA, model=model, timeout=900)
        trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
        if not r["ok"]:
            break
        out = r["output"]
    # code: "Closing Date means the date of this Agreement" -> the closing date IS the agreement date
    cd = defs.get("Closing Date", {}).get("text", "")
    code_notes = []
    if not out["closing_date"]["value"] and re.search(r"means the date (of this Agreement|hereof)", cd) and out["agreement_date"]["value"]:
        out["closing_date"] = {"value": out["agreement_date"]["value"], "quote": cd[:200]}
        code_notes.append("closing_date set by code: the definition makes it the agreement date")
    probs = check(out, live_sq, cure_in_text, covenant_refs)
    trace[-1]["problems"] = probs
    return {"output": out, "review": probs, "code_notes": code_notes, "cure_in_text": cure_in_text, "cure_sections": cure_secs, "trace": trace,
            "cost": round(sum(t["cost_usd"] or 0 for t in trace), 4), "seconds": round(sum(t["seconds"] or 0 for t in trace), 1),
            "input_chars": len(text)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("borrower")
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()
    from ..ingest.run import ROOT
    ab = json.loads((ROOT / "data" / "abstract" / "v1" / f"{a.borrower}.abstract.json").read_text(encoding="utf-8"))
    res = run(a.borrower, [c["ref"] for c in ab["covenants"]], a.model)
    print(json.dumps(res, indent=1)[:6000])


if __name__ == "__main__":
    main()
