"""Blind second-model check: GPT answers the same questions Claude's abstract answers, without seeing Claude's answers.

The confirm check (gpt_review.py) shows GPT each claim with Claude's value and quote and asks agree/disagree; GPT can be
anchored by a plausible reading. Here GPT gets only the question (e.g. "Section 7.11(a): what is the required level for
test dates X to Y?", "Definition of Consolidated EBITDA, clause (j): added or subtracted? refers to a defined term? any
caps?"), answers from the agreement with a verbatim quote, and CODE compares GPT's answer to Claude's. Code decides
agree / disagree; a disagreement becomes a review item only if GPT's quote is verbatim in the agreement. GPT never edits.

Same claim ids as the confirm check, so results land in the same place (second_model_review, mode "blind").
  python -m covenant_engine.abstract.gpt_blind prepare --out data/gpt_blind CARS TRS ...
  (cd data/gpt_blind/<B>; codex exec --skip-git-repo-check --ephemeral --output-schema schema.json -o answers.json - < prompt.md)
  python -m covenant_engine.abstract.gpt_blind score --out data/gpt_blind --write CARS TRS ...
"""
import argparse
import json
import re
from pathlib import Path

from ..core.decompose_term import bundle_dir
from ..ingest.exceptions import squash
from ..ingest.run import ROOT
from .gpt_review import claims

ANSWER = {"type": "object", "additionalProperties": False,
          "required": ["id", "found", "value", "sign", "ref_term", "caps", "fields", "evidence", "note"],
          "properties": {"id": {"type": "string"}, "found": {"type": "boolean"}, "value": {"type": "string"},
                         "sign": {"type": "string", "enum": ["+", "-", "+/-", ""]}, "ref_term": {"type": "string"},
                         "caps": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                             "required": ["type", "value"],
                                                             "properties": {"type": {"type": "string"}, "value": {"type": "string"}}}},
                         "fields": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                               "required": ["name", "value"],
                                                               "properties": {"name": {"type": "string"}, "value": {"type": "string"}}}},
                         "evidence": {"type": "string"}, "note": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["answers"],
          "properties": {"answers": {"type": "array", "items": ANSWER}}}

PROMPT = """You are reading a credit agreement to answer questions about it. The full agreement text is in agreement.txt
in the current directory (search it with grep / rg / sed; it is long). The questions are in questions.json: each has an
id, a question, and the answer fields to fill.

Answer EVERY question from the agreement only, in one answer object per question id:
- found: false if the agreement doesn't contain what is asked (then leave the other fields empty).
- value: the main answer, short (a number for levels: "3.50" for 3.50 to 1.00; a date as YYYY-MM-DD).
- sign: for a part of a definition: "+" if it is added, "-" if it is subtracted / deducted / excluded from the amount,
  "+/-" if either; "" for any other question.
- ref_term: the defined term the part refers to, exactly as capitalised in the agreement, or "".
- caps: every limit the text puts on that part: type ("fixed_amount", "pct_of_ebitda", "relative", "other") and value
  (dollars as a plain number, e.g. 10000000; a percentage as a fraction, e.g. 0.20). [] if none.
- fields: the named fields the question asks for, as {name, value} (use "" when the agreement is silent).
- evidence: an exact quote (10-40 words) copied from agreement.txt that supports the answer.
- note: one short sentence when useful.
Do not compute ratios or amounts. Test dates are fiscal quarter ends. Clause labels: "excl(i)" is item (i) of a list
that applies to the whole definition (things excluded from it); labels such as "proviso 1", "condition 2", "base",
"lead-in" are names for unnumbered text, and the question then gives the clause's opening words to locate it.
Return only the JSON."""

STD_LABEL = re.compile(r"^\(?[a-z0-9ivx]+\)?(\([a-z0-9ivx]+\))*$", re.I)


def _when(v):
    f, t = v.get("from"), v.get("to")
    f = f if isinstance(f, str) else (f or {}).get("date") or "the start"
    t = t if isinstance(t, str) else (t or {}).get("date") or "thereafter (no end date)"
    f = "the first test date" if f in ("", "the start", "open") else f
    t = "thereafter (no end date)" if t in ("open",) else t
    return f, t


def questions(ab):
    """One blind question per claim of the confirm check (same ids), without Claude's values or quotes."""
    cov = {c["id"]: c for c in ab["covenants"]}
    reg = {g["id"]: g for g in ab["regimes"]}
    comp = {f"term:{t['name']}:{i}": (t, p) for t in ab["terms"] for i, p in enumerate(t["components"])}
    out = []
    for c in claims(ab):
        cid, v = c["id"], c["value"]
        parts = cid.split(":")
        if parts[0] == "cov" and parts[-1] == "metric":
            k = cov[parts[1]]
            q, f = (f"Section {k['ref']} ({k['metric']}): is it a maximum or a minimum, and how often is it tested?",
                    ["direction (max or min)", "frequency (quarterly, annually, ...)"])
        elif parts[0] == "cov":
            k = cov[parts[1]]
            fr, to = _when(v)
            q, f = f"Section {k['ref']} ({k['metric']}): what is the required level for test dates from {fr} to {to}?", []
        elif parts[0] == "reg" and len(parts) == 2:
            g = reg[parts[1]]
            q, f = (f"The {g['type'].replace('_', ' ')} '{g['name']}' (it changes the covenant level for "
                    f"{', '.join(g['applies_to'])}): give its terms.",
                    ["increment", "ceiling", "level", "duration_count", "duration_unit", "max_uses"])
        elif parts[0] == "reg":
            g = reg[parts[1]]
            sch = next((x for x in g["schedules"] if x["covenant_id"] == parts[2]), {"rows": []})
            r = sch["rows"][int(parts[3][3:])] if int(parts[3][3:]) < len(sch["rows"]) else {"start": {}, "end": {}}
            fr, to = _when({"from": r["start"], "to": r["end"]})
            q, f = (f"During '{g['name']}', what is the required level for {cov[parts[2]]['metric'] if parts[2] in cov else parts[2]} "
                    f"for test dates from {fr} to {to}?", [])
        elif parts[0] == "term":
            t, p = comp[cid]
            # Locate the part by its opening words when its label alone is ambiguous: another part has the same label,
            # or has exactly the same clause letters (clause (a) stored as 'ratio (a)' = the debt and '(a)' = 'minus cash').
            letters = lambda c: set(re.findall(r"\([^)]+\)", c or ""))
            dup = any(x is not p and (x["clause"] == p["clause"] or (letters(p["clause"]) and letters(x["clause"]) == letters(p["clause"])))
                      for x in t["components"])
            loc = "" if STD_LABEL.match(p["clause"] or "") and not dup else f" (it begins: \"{' '.join((p['quote'] or '').split()[:8])}\")"
            q, f = (f"In the definition of \"{t['name']}\", the part labelled {p['clause'] or '(unnumbered)'}{loc}: is it added "
                    f"or subtracted, does it refer to another defined term, and what limits (caps) apply to it?", [])
        elif cid in ("loan:agreement_date", "loan:closing_date"):
            q, f = f"What is the {parts[1].replace('_', ' ')} of the agreement?", []
        elif cid == "loan:fiscal_year_end":
            q, f = "On what day and month does the borrower's fiscal year end? Answer MM-DD.", []
        elif cid in ("loan:annual_days", "loan:quarterly_days"):
            q, f = (f"Within how many days after period end are the {'annual' if 'annual' in cid else 'quarterly'} "
                    f"financial statements due?", [])
        elif cid == "loan:cure":
            q, f = "Is there an equity cure right for the financial covenants? If yes, the deadline.", ["present (yes or no)", "deadline"]
        elif parts[0] == "price":
            q, f = (f"In the pricing grid, level {int(parts[1]) + 1} counted from the first row: what are its lower and upper "
                    f"bounds of the pricing metric, and its margins?", ["lower", "upper", "margins"])
        else:
            continue
        out.append({"id": cid, "question": q, "fields": f})
    return out


WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "once": 1, "twice": 2}


def _nums(s):
    s = str(s or "")
    s = re.sub(r"\b(" + "|".join(WORDS) + r")\b", lambda m: str(WORDS[m.group(1).lower()]), s, flags=re.I)
    return [float(x.replace(",", "")) for x in re.findall(r"-?\d[\d,]*\.?\d*", s)]


def _n(s):
    n = _nums(s)
    return n[0] if n else None


def _same_num(a, b):
    a, b = _n(a), _n(b)
    if a is None or b is None:
        return a is None and b is None
    return any(abs(x - y) < 1e-6 * max(1, abs(y)) for x in (a, a / 100, a * 100) for y in (b,))


def _field(ans, name):
    for f in ans.get("fields", []):
        if f["name"].split(" ")[0].lower() == name.split(" ")[0].lower():
            return f["value"]
    return ""


# Parts whose '+'/'-' is a representation choice, not added vs subtracted (see the confirm check's conventions)
NO_SIGN = {"calculation_rule", "measurement_period", "scope_entities", "cap_on_total", "secured_only_filter", "term_reference",
           "numerator_term", "denominator_term", "numerator_less_term", "numerator_less_amount"}


def compare(cid, value, ans, meta=None):
    """(agree?, what differs) for Claude's structured value vs GPT's blind answer. Code only. meta = {family, category}
    for a term part (signs are compared only for amount parts)."""
    p = cid.split(":")
    diffs = []
    if p[0] == "cov" and p[-1] == "metric":
        d = _field(ans, "direction").lower()
        want = value["direction"].lower()
        if not d or ("max" in want) != ("max" in d):
            diffs.append(f"direction {want} vs {d or '—'}")
        fq = _field(ans, "frequency").lower()
        if fq and value["frequency"].lower()[:5] not in fq:
            diffs.append(f"frequency {value['frequency']} vs {fq}")
    elif p[0] in ("cov", "reg") and (p[-1].startswith("row")):
        want = value["value"] if isinstance(value, dict) else value
        if not _same_num(ans["value"], want):
            diffs.append(f"level {want} vs {ans['value'] or '—'}")
    elif p[0] == "reg":
        for k in ("increment", "ceiling", "level", "max_uses"):
            if value.get(k) not in (None, ""):
                if not _same_num(_field(ans, k), value[k]):
                    diffs.append(f"{k} {value[k]} vs {_field(ans, k) or '—'}")
        dur = value.get("duration") or {}
        if dur.get("count") and not _same_num(_field(ans, "duration_count"), dur["count"]):
            diffs.append(f"duration {dur['count']} {dur.get('unit')} vs {_field(ans, 'duration_count') or '—'} {_field(ans, 'duration_unit')}")
    elif p[0] == "term":
        meta = meta or {}
        amount = meta.get("family") != "ratio" and meta.get("category") not in NO_SIGN
        if amount and value["sign"] in ("+", "-") and ans["sign"] in ("+", "-") and ans["sign"] != value["sign"]:
            diffs.append(f"sign {value['sign']} vs {ans['sign']}")
        norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower()).rstrip("s")
        mine_ref, his_ref = norm(value.get("ref_term")), norm(ans["ref_term"])
        # A clause can name several defined terms; it's the same reading if either model's term is in the other's text.
        same_ref = (not mine_ref or mine_ref == his_ref or mine_ref in norm(ans["evidence"])
                    or (his_ref and his_ref in norm(value.get("quote"))))
        if not same_ref:
            diffs.append(f"refers to {value['ref_term']} vs {ans['ref_term'] or '—'}")
        # Only hard limits count as caps (a dollar amount or a % of EBITDA); GPT's 'other' limits are conditions.
        # Claude may carry a limit on a sibling part of the same definition (cap_on_total), so compare at term level.
        hard = ("fixed_amount", "pct_of_ebitda", "per_transaction", "lifetime_amount", "greater_of", "lesser_of")
        # Claude sometimes types a dollar limit 'other'; a bare number there is still a limit ('excess_over:N' is a
        # threshold, not a cap). GPT's 'other' entries are conditions, so only its typed limits count.
        mine_ok = lambda c: c.get("type") in hard or (c.get("type") == "other" and re.fullmatch(r"[\d.,]+", str(c.get("value", "")).strip()))
        vals = lambda caps, ok: sorted({round(x, 6) for c in caps if ok(c) for x in _nums(c["value"])[:1]})
        own, term = vals(value.get("caps", []), mine_ok), vals((meta or {}).get("term_caps", []), mine_ok)
        his = vals(ans.get("caps", []), lambda c: c.get("type") in ("fixed_amount", "pct_of_ebitda"))
        # A cap written in the definition this part points to (e.g. '$125M' in 'Unrestricted Cash', reached through
        # 'minus Unrestricted Cash') belongs to that definition: it is compared on that definition's own question,
        # not again here. GPT may carry it through; the abstract stores it where the agreement writes it.
        via = [round(x, 6) for x in (meta or {}).get("ref_caps", [])]
        his = [h for h in his if not any(_same_num(h, v) for v in via)]
        # Claude's own caps on this part, plus any of the term's caps GPT also attached here (a shared basket)
        mine = sorted(set(own) | {x for x in term if any(_same_num(x, h) for h in his)})
        if mine and not his:
            diffs.append(f"caps {mine} vs none")
        elif his and not mine:
            diffs.append(f"caps none vs {his}")
        elif mine and his and not all(any(_same_num(a, b) for b in his) for a in mine):
            diffs.append(f"caps {mine} vs {his}")
    elif cid in ("loan:agreement_date", "loan:closing_date"):
        if (ans["value"] or "")[:10] != str(value)[:10]:
            diffs.append(f"{p[1]} {value} vs {ans['value'] or '—'}")
    elif cid == "loan:fiscal_year_end":
        if re.sub(r"\D", "", ans["value"])[-4:] != re.sub(r"\D", "", str(value))[-4:]:
            diffs.append(f"fiscal year end {value} vs {ans['value'] or '—'}")
    elif cid in ("loan:annual_days", "loan:quarterly_days"):
        if not _same_num(ans["value"], value):
            diffs.append(f"days {value} vs {ans['value'] or '—'}")
    elif cid == "loan:cure":
        his = _field(ans, "present").lower().startswith("y") or (ans["found"] and "yes" in ans["value"].lower())
        if bool(value.get("present")) != his:
            diffs.append(f"equity cure {'yes' if value.get('present') else 'no'} vs {'yes' if his else 'no'}")
    elif p[0] == "price":
        for k in ("lower", "upper"):
            if value.get(k) not in (None, "") and not _same_num(_field(ans, k), value[k]):
                diffs.append(f"{k} {value[k]} vs {_field(ans, k) or '—'}")
        want = sorted(_n(m["value"]) for m in value.get("margins", []) if _n(m["value"]) is not None)
        got = _nums(_field(ans, "margins"))
        if want and not all(any(_same_num(w, g) for g in got) for w in want):
            diffs.append(f"margins {want} vs {got or '—'}")
    return not diffs, "; ".join(diffs)


def prepare(borrowers, out_dir, abs_dir="data/abstract/v1"):
    for b in borrowers:
        ab = json.loads((ROOT / abs_dir / f"{b}.abstract.json").read_text(encoding="utf-8"))
        d = ROOT / out_dir / b
        d.mkdir(parents=True, exist_ok=True)
        (d / "agreement.txt").write_text((bundle_dir(b) / "text_live.txt").read_text(encoding="utf-8"), encoding="utf-8")
        qs = questions(ab)
        (d / "questions.json").write_text(json.dumps(qs, indent=1, ensure_ascii=False), encoding="utf-8")
        (d / "prompt.md").write_text(PROMPT, encoding="utf-8")
        (d / "schema.json").write_text(json.dumps(SCHEMA), encoding="utf-8")
        print(f"{b}: {len(qs)} blind questions -> {d}")


def ref_caps(ab, ans, ref, own):
    """Every hard cap on the definition a part points to: the abstract's, and GPT's answers for that definition's parts."""
    if not ref or ref == own:
        return []
    out = []
    for t in ab["terms"]:
        if t["name"] != ref:
            continue
        for i, c in enumerate(t["components"]):
            out += [x for k in c.get("caps", []) if k.get("type") in ("fixed_amount", "pct_of_ebitda") or
                    re.fullmatch(r"[\d.,]+", str(k.get("value", "")).strip()) for x in _nums(k["value"])[:1]]
            a = ans.get(f"term:{ref}:{i}") or {}
            out += [x for k in a.get("caps", []) if k.get("type") in ("fixed_amount", "pct_of_ebitda") for x in _nums(k["value"])[:1]]
    return out


def score(b, out_dir, abs_dir="data/abstract/v1", write=False):
    d = ROOT / out_dir / b
    p = ROOT / abs_dir / f"{b}.abstract.json"
    ab = json.loads(p.read_text(encoding="utf-8"))
    cl = {c["id"]: c for c in claims(ab)}
    ans = {a["id"]: a for a in json.loads((d / "answers.json").read_text(encoding="utf-8"))["answers"]}
    meta = {f"term:{t['name']}:{i}": {"family": t["family"], "category": p_["category"],
                                       "term_caps": [k for x in t["components"] for k in x.get("caps", [])],
                                       "ref_caps": ref_caps(ab, ans, p_.get("ref_term"), t["name"])}
            for t in ab["terms"] for i, p_ in enumerate(t["components"])}
    live = squash((d / "agreement.txt").read_text(encoding="utf-8"))
    tally = {"claims": len(cl), "agree": 0, "disagree_verified": 0, "disagree_unsupported": 0, "not_found": 0,
             "missing_answer": 0}
    review, per = [], {}
    for cid, c in cl.items():
        a = ans.get(cid)
        if not a:
            tally["missing_answer"] += 1
            continue
        ok_ev = bool(a["evidence"]) and squash(a["evidence"])[:120] in live
        absent_claim = cid == "loan:cure" and not (c["value"] or {}).get("present")
        if not a["found"] and not absent_claim:
            verdict, why = "not_found", "GPT could not find it in the agreement"
            tally["not_found"] += 1
        else:
            same, why = compare(cid, c["value"], a, meta.get(cid))
            verdict = "agree" if same else "disagree"
            if same:
                tally["agree"] += 1
            elif ok_ev:
                tally["disagree_verified"] += 1
            else:
                tally["disagree_unsupported"] += 1
        reads = "; ".join(x for x in [a["value"], a["sign"] and f"sign {a['sign']}", a["ref_term"] and f"refers to {a['ref_term']}",
                                      a["caps"] and "caps " + ", ".join(f"{k['type']} {k['value']}" for k in a["caps"])] if x)
        per[cid] = {"verdict": verdict, "evidence_verbatim": ok_ev, "value_in_text": (why if verdict != "agree" else reads)[:200],
                    "evidence": a["evidence"][:300], "note": a["note"][:200], "mode": "blind"}
        if verdict == "disagree" and ok_ev or verdict == "not_found":
            review.append({"step": "gpt", "id": cid, "claim": c["claim"][:200], "gpt_reads": why[:200],
                           "evidence": a["evidence"][:300] if ok_ev else "", "note": a["note"][:200]})
    if write:
        ab["review"] = [x for x in ab["review"] if x.get("step") != "gpt"] + review
        ab["second_model_review"] = {"tool": "codex exec", "mode": "blind", "tally": tally, "claims": per}
        p.write_text(json.dumps(ab, indent=1, ensure_ascii=False), encoding="utf-8")
    return tally, review


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "score"])
    ap.add_argument("deals", nargs="+")
    ap.add_argument("--out", default="data/gpt_blind")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()
    for b in a.deals:
        if a.cmd == "prepare":
            prepare([b], a.out)
        else:
            t, rv = score(b, a.out, write=a.write)
            print(b, t)
            for r in rv:
                print("   -", r["id"], "|", r["gpt_reads"][:120])


if __name__ == "__main__":
    main()
