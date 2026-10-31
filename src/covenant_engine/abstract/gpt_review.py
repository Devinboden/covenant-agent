"""Second-model check (#30): an independent GPT pass over each abstract through the Codex CLI (`codex exec
--output-schema`), run with a ChatGPT sign-in for the Codex CLI (CODEX_HOME).

Code turns the abstract into numbered claims (covenant schedule rows, regimes, every term part with sign / reference /
caps, loan fields, pricing levels). Codex gets the agreement as a file it can search (read-only sandbox) and returns a
verdict per claim: agree / disagree (with the value it reads) / not_in_text, each with a verbatim evidence quote.
Code then decides: a disagreement counts only if its evidence is verbatim in the agreement; verified disagreements go
to review (step "gpt"); unsupported ones and missing verdicts are counted, not acted on. GPT never edits the abstract.

  python -m covenant_engine.abstract.gpt_review prepare --out data/gpt_review WEST TRS ...   (container: writes job folders)
  (device)  codex exec --skip-git-repo-check --ephemeral --output-schema schema.json -o verdicts.json - < prompt.md
  python -m covenant_engine.abstract.gpt_review score --out data/gpt_review WEST TRS ...     (container: checks + review)
"""
import argparse
import json
import re
from pathlib import Path

from ..ingest.exceptions import squash
from ..ingest.run import ROOT
from ..core.decompose_term import bundle_dir

VERDICT = {"type": "object", "additionalProperties": False,
           "required": ["id", "verdict", "value_in_text", "evidence", "note"],
           "properties": {"id": {"type": "string"},
                          "verdict": {"type": "string", "enum": ["agree", "disagree", "not_in_text"]},
                          "value_in_text": {"type": "string"}, "evidence": {"type": "string"}, "note": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdicts", "missed"],
          "properties": {"verdicts": {"type": "array", "items": VERDICT},
                         "missed": {"type": "array", "items": {
                             "type": "object", "additionalProperties": False, "required": ["what", "evidence"],
                             "properties": {"what": {"type": "string"}, "evidence": {"type": "string"}}}}}}
PROMPT = """You are checking a structured abstract of a credit agreement, made by another system, against the agreement.
The full agreement text is in the file agreement.txt in the current directory (search it with grep / rg / sed; it is
long). The claims to check are in claims.json: each has an id, a plain-English claim and the structured value.

For EVERY claim return one verdict:
- agree: the agreement says this.
- disagree: the agreement says something different; value_in_text = what it actually says (same format as value).
- not_in_text: you cannot find support for it.
evidence: an exact quote (10-40 words) copied from agreement.txt that decides the verdict ("" for agree on a claim that
something is absent). note: one short sentence when useful.
Do not compute ratios or amounts; check what the text says. Dates: test dates are fiscal quarter ends.

CONVENTIONS OF THE ABSTRACT (these are representation choices, not errors; do not disagree because of them):
- Signs: '+' on a ratio part means it is in that side of the ratio (numerator / denominator is in the clause name).
  Check signs only for amount parts (added vs subtracted). Do not dispute the sign of eligibility conditions, scope
  filters, measurement periods, cap parts ('cap_on_total') or calculation rules.
- Clause labels: "excl(i)" = an item of a list that applies to the whole definition; labels such as "proviso 1",
  "condition 2", "base", "ratio" are names for unnumbered text. Dispute a label only if the text belongs to a
  different clause than the one named.
- Caps: a limit stated in a definition that a part references may be carried on that part. But CHECK EVERY CAP VALUE:
  it must be the limit the text states (a dollar amount, a percentage of EBITDA written as a fraction, or
  'excess_over:N'). If the text's limit is relative to another amount (e.g. 'the amount added back under clause
  (a)(v)') and the value is a bare number, disagree.
- Schedules: a row's start may be the day after the previous row ends rather than the first test date; that is the
  same row if it covers the same test dates. Relief levels may sit in the standard schedule while a relief regime
  exists; dispute a schedule value only if the level for a test date is wrong.
- A claim that something is absent ("has no equity cure"): agree if you find no such provision.

Then list in "missed" (at most 8) only provisions the claims leave out that CHANGE A COVENANT RESULT: a threshold or
test date, an amount that is added or subtracted, a cap, or a mechanism that changes which threshold applies. Do not
list conditions, wording or no-duplication language already implied by the claims. Each needs an exact evidence quote.

Return only the JSON."""


def _num(v):
    return str(v).strip()


def claims(ab):
    out = []

    def add(cid, claim, value):
        out.append({"id": cid, "claim": claim, "value": value})

    for c in ab["covenants"]:
        rg = next((g["name"] for g in ab["regimes"] if g["id"] == c.get("applies_during")), "")
        add(f"cov:{c['id']}:metric", f"Section {c['ref']} tests the {c['metric']} ({c['direction']} the threshold), "
                                     f"tested {c['frequency']}"
                                     + (f" plus on {', '.join(c.get('event_tests', []))} dates" if c.get("event_tests") else "")
                                     + (f"; skipped when {c['skip_when']}" if c.get("skip_when") else "")
                                     + (f"; the covenant applies only during '{rg}'" if rg else ""),
            {"ref": c["ref"], "metric": c["metric"], "direction": c["direction"], "frequency": c["frequency"],
             "event_tests": c.get("event_tests", []), "applies_during": rg})
        for i, r in enumerate(c["schedule"]):
            s, e = r["start"], r["end"]
            when = (s["date"] if s["kind"] == "date" else f"{s['count']} {s['kind']} {s['anchor']}".strip()) + " to " + \
                   (e["date"] if e["kind"] == "date" else (e["kind"] if e["kind"] == "open" else f"{e['count']} {e['kind']} {e['anchor']}"))
            add(f"cov:{c['id']}:row{i}", f"Under Section {c['ref']} the standard level for test dates {when} is {r['value']}"
                + (f" (while '{rg}' lasts; 'open' means until it ends)" if rg else ""),
                {"from": s.get("date") or s, "to": e.get("date") or e, "value": r["value"]})
    for g in ab["regimes"]:
        add(f"reg:{g['id']}", f"The agreement has a {g['type'].replace('_', ' ')} called '{g['name']}' applying to "
                              f"{', '.join(g['applies_to'])}", {k: g.get(k) for k in ("type", "name", "level", "increment",
                                                                                         "ceiling", "duration", "max_uses")})
        for s in g["schedules"]:
            for i, r in enumerate(s["rows"]):
                add(f"reg:{g['id']}:{s['covenant_id']}:row{i}",
                    f"During '{g['name']}' the level for {s['covenant_id']} from {r['start'].get('date') or r['start']} "
                    f"to {r['end'].get('date') or r['end']} is {r['value']}", r["value"])
    for t in ab["terms"]:
        for i, p in enumerate(t["components"]):
            sign = {"+": "is added", "-": "is subtracted", "+/-": "is added or subtracted"}.get(p["sign"], p["sign"])
            cl = f"In the definition of {t['name']}, clause {p['clause'] or '(unnumbered)'} {sign}"
            if p["ref_term"]:
                cl += f" and refers to the defined term {p['ref_term']}"
            for cap in p["caps"]:
                cl += f"; capped: {cap['type']} {cap['value']}" + (f" from {cap['from']}" if cap["from"] else "") + \
                      (f" to {cap['to']}" if cap["to"] else "") + (f" during {cap['during']}" if cap["during"] else "")
            add(f"term:{t['name']}:{i}", cl, {"clause": p["clause"], "sign": p["sign"], "ref_term": p["ref_term"],
                                             "caps": [{k: cap[k] for k in ("type", "value", "from", "to", "during")} for cap in p["caps"]],
                                             "quote": p["quote"][:200]})
    ln = ab.get("loan") or {}
    if ln:
        for f in ("agreement_date", "closing_date", "fiscal_year_end"):
            if ln.get(f, {}).get("value"):
                add(f"loan:{f}", f"The {f.replace('_', ' ')} is {ln[f]['value']}", ln[f]["value"])
        rp = ln.get("reporting") or {}
        for f in ("annual_days", "quarterly_days"):
            if rp.get(f):
                add(f"loan:{f}", f"{'Annual' if f[0] == 'a' else 'Quarterly'} financial statements are due within {rp[f]} days", rp[f])
        cu = ln.get("equity_cure") or {}
        add("loan:cure", "The agreement " + ("has" if cu.get("present") else "has no") + " equity cure right for the financial covenants"
            + (f" (deadline {cu.get('deadline_count')} {cu.get('deadline_unit')}; limits "
               + "; ".join(f"{l['type']} {l['value']}" for l in cu.get("limits", [])) + ")" if cu.get("present") else ""),
            {k: cu.get(k) for k in ("present", "deadline_count", "deadline_unit")})
    pg = ab.get("pricing_grid") or {}
    if pg.get("present"):
        for i, L in enumerate(pg["levels"]):
            add(f"price:{i}", f"Pricing level {L['level']} applies when the {pg['metric']} is "
                              f"{'>=' if L['lower_inclusive'] else '>'} {L['lower'] or '-inf'} and "
                              f"{'<=' if L['upper_inclusive'] else '<'} {L['upper'] or 'inf'}, with margins "
                              + ", ".join(f"{m['type']} {m['value']} {m['unit']}" for m in L["margins"]),
                {"lower": L["lower"], "upper": L["upper"], "margins": L["margins"]})
    return out


def prepare(borrowers, out_dir, abs_dir="data/abstract/v1"):
    out = ROOT / out_dir
    for b in borrowers:
        ab = json.loads((ROOT / abs_dir / f"{b}.abstract.json").read_text(encoding="utf-8"))
        d = out / b
        d.mkdir(parents=True, exist_ok=True)
        (d / "agreement.txt").write_text((bundle_dir(b) / "text_live.txt").read_text(encoding="utf-8"), encoding="utf-8")
        cl = claims(ab)
        (d / "claims.json").write_text(json.dumps(cl, indent=1, ensure_ascii=False), encoding="utf-8")
        (d / "prompt.md").write_text(PROMPT, encoding="utf-8")
        (d / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
        print(f"{b}: {len(cl)} claims -> {d}")


def score(b, out_dir, abs_dir="data/abstract/v1", write=False, include_missed=False):
    d = ROOT / out_dir / b
    cl = {c["id"]: c for c in json.loads((d / "claims.json").read_text(encoding="utf-8"))}
    res = json.loads((d / "verdicts.json").read_text(encoding="utf-8"))
    live = squash((d / "agreement.txt").read_text(encoding="utf-8"))
    tally = {"claims": len(cl), "agree": 0, "disagree_verified": 0, "disagree_unsupported": 0, "not_in_text": 0,
             "missing_verdict": 0, "unknown_id": 0, "missed_verified": 0, "missed_unsupported": 0}
    review, seen, per_claim = [], set(), {}
    for v in res["verdicts"]:
        if v["id"] not in cl:
            tally["unknown_id"] += 1
            continue
        seen.add(v["id"])
        ok_ev = bool(v["evidence"]) and squash(v["evidence"])[:120] in live
        # every verdict is kept per claim (agreements too), so the review screen can show what GPT read for an item
        per_claim[v["id"]] = {"verdict": v["verdict"], "evidence_verbatim": ok_ev, "value_in_text": v["value_in_text"][:200],
                              "evidence": v["evidence"][:300], "note": v["note"][:200]}
        if v["verdict"] == "agree":
            tally["agree"] += 1
        elif v["verdict"] == "disagree":
            if ok_ev:
                tally["disagree_verified"] += 1
                review.append({"step": "gpt", "id": v["id"], "claim": cl[v["id"]]["claim"][:200],
                               "gpt_reads": v["value_in_text"][:200], "evidence": v["evidence"][:300], "note": v["note"][:200]})
            else:
                tally["disagree_unsupported"] += 1
        else:
            tally["not_in_text"] += 1
            review.append({"step": "gpt", "id": v["id"], "claim": cl[v["id"]]["claim"][:200], "gpt_reads": "not found",
                           "note": v["note"][:200]})
    tally["missing_verdict"] = len(set(cl) - seen)
    for m in res["missed"]:
        if m["evidence"] and squash(m["evidence"])[:120] in live:
            tally["missed_verified"] += 1
            if include_missed:          # off by default: 'missed' is too loose (15/15 on every deal) until the prompt is tightened
                review.append({"step": "gpt", "id": "missed", "claim": m["what"][:200], "evidence": m["evidence"][:300]})
        else:
            tally["missed_unsupported"] += 1
    if write:
        p = ROOT / abs_dir / f"{b}.abstract.json"
        ab = json.loads(p.read_text(encoding="utf-8"))
        ab["review"] = [x for x in ab["review"] if x.get("step") != "gpt"] + review
        ab["second_model_review"] = {"tool": "codex exec", "tally": tally, "claims": per_claim}
        p.write_text(json.dumps(ab, indent=1, ensure_ascii=False), encoding="utf-8")
    return tally, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "score"])
    ap.add_argument("deals", nargs="+")
    ap.add_argument("--out", default="data/gpt_review")
    ap.add_argument("--write", action="store_true", help="score: add verified disagreements to the abstracts' review")
    a = ap.parse_args()
    if a.cmd == "prepare":
        prepare(a.deals, a.out)
    else:
        for b in a.deals:
            t, rv = score(b, a.out, write=a.write)
            print(b, t)
            for r in rv:
                print("   -", r["id"], "|", r["claim"][:110], "| GPT:", r.get("gpt_reads", "")[:80], "|", r.get("note", "")[:80])


if __name__ == "__main__":
    main()
