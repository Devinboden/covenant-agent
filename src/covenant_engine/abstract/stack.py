"""Amendment stacking (#28): apply standalone amendments to the latest conformed copy when no newer conformed copy
exists. Sonnet reads each amendment's instructions into typed operations; code checks every quote, applies the
operations to the base text (whitespace-tolerant location), records each one as applied / failed, flags conflicts (an
operation whose target or old text an earlier amendment already changed), and re-runs ingest on the edited blocks so
the result is an ordinary bundle the abstract pipeline reads. Provenance goes in report.json "stacked".

Measure: compare every target an amendment touched with the next real conformed copy (score_stack below).

Usage: python -m covenant_engine.abstract.stack MEC data/intake/MEC/<base> data/pdf/MEC/<amendment>.pdf [...] [--compare <dir>]
"""
import argparse
import difflib
import json
import re
from pathlib import Path

from ..ingest.exceptions import call_model, squash
from ..ingest.run import ROOT, run as ingest
from .clauses import enumerate_clauses

OPS = ["add_definition", "restate_definition", "delete_definition", "restate_definition_clause", "restate_section",
       "restate_section_clause", "replace_text", "insert_text", "delete_text", "other_document", "other"]
OP = {"type": "object", "additionalProperties": False,
      "required": ["op", "section", "definition", "clause", "old_text", "new_text", "after_text", "quote"],
      "properties": {"op": {"type": "string", "enum": OPS}, "section": {"type": "string"}, "definition": {"type": "string"},
                     "clause": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"},
                     "after_text": {"type": "string"}, "quote": {"type": "string"}}}
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["name", "effective_date", "conditions", "ops", "flags"],
          "properties": {"name": {"type": "string"}, "effective_date": {"type": "string"},
                         "conditions": {"type": "string"}, "ops": {"type": "array", "items": OP},
                         "flags": {"type": "array", "items": {"type": "string"}}}}
SYSTEM = ("You read an amendment to a credit agreement into a list of edit operations. Work only from the text given. "
          "Copy all text exactly; never paraphrase or retype numbers.")
PROMPT = """List every change this amendment makes, one operation per change, in the order given:
- add_definition: new_text = the whole new definition, starting with its quoted term (“X” means ...), without the
  amendment's outer quotation marks. One operation per definition.
- restate_definition / delete_definition: definition = the defined term; new_text = the whole restated definition.
- restate_definition_clause: definition + clause as cited (e.g. "(e)(ii)"); new_text = the restated clause text, starting
  with its marker ("(ii) ...").
- restate_section / restate_section_clause: section ("8.15") + clause if any; new_text = the restated text including the
  heading if the amendment gives one.
- replace_text / delete_text: old_text = the exact words being replaced / deleted (as quoted in the amendment);
  insert_text: after_text = the words it goes after; new_text = the inserted words. Give section or definition.
- other_document: a change to a different document (collateral agreement, guaranty...): section + quote only.
- other: anything you can't express (e.g. "Schedule 2.01 is replaced by Annex B").
section: the credit-agreement section ("1.01" for definitions). quote: the instruction's own words (8-30 words).
name: the amendment's name; effective_date: YYYY-MM-DD if stated, else ""; conditions: one line on what makes it effective.
flags: anything a reviewer must see (e.g. an amendment that refers to an attached conformed copy).

AMENDMENT:
<<<
{text}
>>>"""


# ---------------------------------------------------------------- base text and locating spans
class Base:
    def __init__(self, d):
        self.dir = Path(d)
        self.blocks = [json.loads(l) for l in (self.dir / "blocks.jsonl").read_text(encoding="utf-8").splitlines()]
        self.report = json.loads((self.dir / "report.json").read_text(encoding="utf-8"))

    def defs(self):
        return json.loads((self.dir / "definitions.json").read_text(encoding="utf-8"))["defined"]

    def sections(self):
        return json.loads((self.dir / "sections.json").read_text(encoding="utf-8"))


def joined(blocks):
    """Live text with each block's offset."""
    offs, parts, pos = [], [], 0
    for b in blocks:
        offs.append(pos)
        parts.append(b["text_live"] or "")
        pos += len(b["text_live"] or "") + 2
    return "\n\n".join(parts), offs


def locate(text, snippet, start=0, end=None):
    """Whitespace-tolerant find; also tolerant of curly/straight quotes. Returns (a, b) or None."""
    toks = snippet.split()
    if not toks:
        return None
    pat = r"\s*".join(re.escape(t).replace("“", "[“\"]").replace("”", "[”\"]").replace("’", "[’']") for t in toks)
    m = re.compile(pat).search(text, start, end if end is not None else len(text))
    return (m.start(), m.end()) if m else None


def edit(blocks, a, b, new):
    """Replace joined-text span [a, b) with new text, editing the blocks it crosses."""
    _, offs = joined(blocks)
    out = []
    for i, bl in enumerate(blocks):
        t = bl["text_live"] or ""
        s, e = offs[i], offs[i] + len(t)
        if e < a or s > b or (e == a and s != a):
            out.append(bl)
            continue
        pre = t[:max(0, a - s)] if s <= a else ""
        post = t[b - s:] if b <= e else ""
        first = s <= a <= e
        txt = (pre + (new if first else "") + post) if first or post else ""
        out.append(dict(bl, text_live=txt, text_raw=txt, struck_chars=0, edited=True))
    return out


def insert_block(blocks, before_idx, text, ref):
    nb = dict(blocks[before_idx], text_live=text, text_raw=text, struck_chars=0, id=f"{blocks[before_idx]['id']}-add",
              edited=True, added_by=ref)
    return blocks[:before_idx] + [nb] + blocks[before_idx:]


def def_span(blocks, term, defs):
    """Span of a definition in the current joined text: from its opening quoted term to the start of the next
    definition in Section 1.01 order (or to its own end)."""
    text, offs = joined(blocks)
    d = defs.get(term)
    if not d:
        return None
    a = locate(text, d["text"][:160])
    if not a:
        return None
    z = locate(text, d["text"][-160:], a[0])
    return (a[0], z[1]) if z else None


def sec_span(blocks, sid, sections):
    s = next((x for x in sections if x["id"] == sid or x.get("printed_id") == sid), None)
    if not s:
        return None
    text, offs = joined(blocks)
    a = offs[s["start"]]
    b = offs[s["end"] - 1] + len(blocks[s["end"] - 1]["text_live"] or "")
    return (a, b)


def clause_span(text, span, clause):
    """Span of a cited clause inside a definition/section span: from its marker to the next marker that isn't a
    sub-item of it."""
    body = text[span[0]:span[1]]
    leaves = enumerate_clauses(body)
    want = clause.replace(" ", "").lower()
    ids = [c for c, _, _ in leaves]
    hits = [i for i, c in enumerate(ids) if c == want or c.startswith(want + "(")]
    if not hits:
        return None
    first = leaves[hits[0]]
    # the marker of the cited level: the last marker in the clause id ("(e)(ii)" -> "(ii)")
    last = re.findall(r"\([a-z0-9]+\)", want)[-1]
    a = body.rfind(last, 0, first[1] + len(last) + 1)
    a = a if a >= 0 else first[1]
    nxt = next((leaves[i][1] for i in range(hits[-1] + 1, len(leaves))), None)
    b = nxt if nxt is not None else len(body)
    seg = body[a:b]
    seg_end = a + len(seg.rstrip())
    # stop before a trailing connector that belongs to the list ("; and", "; or", ",")
    m = re.search(r"(\s*(;|,)?\s*(and|or)?\s*)$", body[a:seg_end])
    return (span[0] + a, span[0] + seg_end - (len(m.group(0)) if m else 0))


# ---------------------------------------------------------------- reading the amendment
def amendment_text(pdf):
    """The ingest extractor (drops running headers / footers / page ids, keeps live text of a blackline)."""
    from ..ingest.extract import extract
    blocks, _ = extract(pdf)
    return re.sub(r"\s+", " ", " ".join(b["text_live"] for b in blocks if b["text_live"]))


def operative(text, limit=60000):
    """The part of the amendment that carries instructions: from the first 'hereby amended' paragraph to the
    conditions / representations section (whole text when short)."""
    if len(text) <= limit:
        return text
    i = max(0, text.find("hereby amended") - 3000)
    return text[i:i + limit]


def check(out, amend_sq, base_text, defs, sections):
    P = []
    secs = {s["id"] for s in sections} | {s.get("printed_id") for s in sections}
    for k, o in enumerate(out["ops"]):
        w = f"op {k + 1} {o['op']}"
        for f in ("new_text", "old_text", "after_text", "quote"):
            if o[f] and squash(o[f])[:200] not in amend_sq:
                P.append(f"{w}: {f} not found verbatim in the amendment: {o[f][:70]!r}")
        if o["op"] in ("restate_definition", "delete_definition", "restate_definition_clause") and o["definition"] not in defs:
            P.append(f"{w}: definition {o['definition']!r} is not defined in the agreement; give its exact name")
        if o["op"] == "add_definition":
            m = re.match(r"\s*[“\"]([^”\"]+)[”\"]", o["new_text"])
            if not m:
                P.append(f"{w}: new_text must start with the quoted defined term")
        if o["op"] in ("restate_section", "restate_section_clause") and o["section"].split("(")[0] not in secs:
            P.append(f"{w}: section {o['section']!r} is not a section of the agreement")
        if o["op"] in ("restate_definition_clause", "restate_section_clause") and not o["clause"]:
            P.append(f"{w}: give the clause")
        if o["op"] in ("replace_text", "delete_text") and not o["old_text"]:
            P.append(f"{w}: give old_text")
        if o["op"] == "insert_text" and not o["after_text"]:
            P.append(f"{w}: give after_text")
    return P


def read_amendment(pdf, base, model="sonnet", max_repairs=2):
    text = amendment_text(pdf)
    amend_sq = squash(text)
    base_text, _ = joined(base.blocks)
    defs, sections = base.defs(), base.sections()
    prompt = PROMPT.format(text=operative(text))
    trace = []
    r = call_model(SYSTEM, prompt, SCHEMA, model=model, timeout=900)
    trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
    if not r["ok"]:
        return {"error": r.get("error"), "trace": trace}
    out = r["output"]
    for _ in range(max_repairs):
        probs = check(out, amend_sq, base_text, defs, sections)
        trace[-1]["problems"] = probs
        if not probs:
            break
        r = call_model(SYSTEM, prompt + "\n\nYOUR PREVIOUS ANSWER FAILED THESE CHECKS; return the complete corrected answer:\n"
                       + "\n".join(f"- {p}" for p in probs) + "\n\nPREVIOUS ANSWER:\n" + json.dumps(out), SCHEMA,
                       model=model, timeout=900)
        trace.append({k: r.get(k) for k in ("model", "cost_usd", "seconds", "ok")})
        if not r["ok"]:
            break
        out = r["output"]
    probs = check(out, amend_sq, base_text, defs, sections)
    trace[-1]["problems"] = probs
    return {"output": out, "review": probs, "trace": trace, "source": Path(pdf).name,
            "cost": round(sum(t["cost_usd"] or 0 for t in trace), 4)}


# ---------------------------------------------------------------- applying
def _strip_outer(t):
    return t.strip().strip("“”\"").strip() if t.strip()[:1] in "“\"" and t.strip()[1:2] in "(“\"" else t.strip()


def apply(blocks, ops, defs, sections, ref, touched):
    """Apply one amendment's ops in order. Returns (blocks, log). touched: target -> earlier amendment (conflicts)."""
    log = []
    for k, o in enumerate(ops):
        tgt = o["definition"] or (f"Section {o['section']}" if o["section"] else "")
        tgt_full = tgt + (o["clause"] or "")
        entry = {"amendment": ref, "n": k + 1, "op": o["op"], "target": tgt_full, "quote": o["quote"][:160]}
        text, _ = joined(blocks)
        try:
            if o["op"] in ("other_document", "other"):
                entry["status"] = "recorded (not applied)"
            elif o["op"] == "add_definition":
                term = re.match(r"\s*[“\"]([^”\"]+)[”\"]", o["new_text"]).group(1).strip()
                entry["target"] = term
                if term in defs:
                    entry.update(status="conflict", why="term already defined: treated as restatement")
                    sp = def_span(blocks, term, defs)
                    blocks = edit(blocks, sp[0], sp[1], o["new_text"].strip()) if sp else blocks
                else:
                    later = sorted(t for t in defs if defs[t].get("section") in ("1.01", "1.1") and t.lower() > term.lower())
                    nxt = min(later, key=lambda t: defs[t]["start"]) if later else None
                    idx = defs[nxt]["start"] if nxt else max(d["end"] for d in defs.values())
                    blocks = insert_block(blocks, idx, o["new_text"].strip(), ref)
                    entry["status"] = "applied"
                    entry["placed_before"] = nxt
            elif o["op"] in ("restate_definition", "delete_definition", "restate_definition_clause"):
                sp = def_span(blocks, o["definition"], defs)
                if not sp:
                    raise ValueError("definition text not found in the current text")
                if o["op"] == "restate_definition_clause":
                    sp = clause_span(text, sp, o["clause"])
                    if not sp:
                        raise ValueError(f"clause {o['clause']} not found in the definition")
                new = "" if o["op"] == "delete_definition" else _strip_outer(o["new_text"]).rstrip()
                if o["op"] == "restate_definition_clause":
                    new = new.rstrip(",;").rstrip() if text[sp[1]:sp[1] + 2].strip()[:1] in ",;" else new
                blocks = edit(blocks, sp[0], sp[1], new)
                entry["status"] = "applied"
            elif o["op"] in ("restate_section", "restate_section_clause"):
                sp = sec_span(blocks, o["section"].split("(")[0], sections)
                if not sp:
                    raise ValueError("section not found")
                if o["op"] == "restate_section_clause":
                    sp = clause_span(text, sp, o["clause"] or "(" + o["section"].split("(", 1)[1])
                    if not sp:
                        raise ValueError(f"clause {o['clause']} not found in the section")
                blocks = edit(blocks, sp[0], sp[1], _strip_outer(o["new_text"]))
                entry["status"] = "applied"
            elif o["op"] in ("replace_text", "delete_text", "insert_text"):
                scope = (def_span(blocks, o["definition"], defs) if o["definition"]
                         else sec_span(blocks, o["section"].split("(")[0], sections) if o["section"] else (0, len(text)))
                if not scope:
                    raise ValueError("target not found")
                needle = o["after_text"] if o["op"] == "insert_text" else o["old_text"]
                sp = locate(text, needle, scope[0], scope[1])
                if not sp:
                    raise ValueError(f"text {needle[:60]!r} not found in the target (changed by an earlier amendment?)")
                if o["op"] == "insert_text":
                    blocks = edit(blocks, sp[1], sp[1], " " + o["new_text"].strip())
                else:
                    blocks = edit(blocks, sp[0], sp[1], "" if o["op"] == "delete_text" else o["new_text"].strip())
                entry["status"] = "applied"
        except (ValueError, AttributeError) as e:
            entry.update(status="failed", why=str(e))
        if entry.get("status") == "applied" and tgt_full in touched and touched[tgt_full] != ref:
            entry.update(status="applied_conflict", why=f"also changed by {touched[tgt_full]}: later amendment applied")
        if entry.get("status", "").startswith("applied"):
            touched.setdefault(tgt_full, ref)
        log.append(entry)
    return blocks, log


def stack(borrower, base_dir, amendments, model="sonnet", name=None):
    base = Base(base_dir)
    blocks, logs, reads, touched = base.blocks, [], [], {}
    for n, pdf in enumerate(amendments):
        rd = read_amendment(pdf, base, model=model)
        reads.append(rd)
        if "error" in rd:
            logs.append({"amendment": Path(pdf).name, "status": "failed", "why": f"model call failed: {rd['error']}"})
            continue
        ref = f"#{n + 1} {rd['output']['name'] or Path(pdf).stem}"
        blocks, log = apply(blocks, rd["output"]["ops"], base.defs(), base.sections(), ref, touched)
        logs += log
        # re-derive sections / definitions on the edited text before the next amendment
        tmp = ingest(ROOT / "data" / "pdf" / borrower / base.report["source_file"], borrower, blocks_in=blocks,
                     out_name="_stack_tmp")[0]
        base = Base(tmp)
        blocks = base.blocks
    review = [f"{l['amendment']} op {l.get('n')} {l.get('op')} {l.get('target')}: {l['status']} ({l.get('why', '')})"
              for l in logs if l.get("status") not in ("applied", "recorded (not applied)")]
    review += [f"{r['source']}: {p}" for r in reads if "output" in r for p in r["review"]]
    review += [f"{r['source']}: {f}" for r in reads if "output" in r for f in r["output"]["flags"]]
    extra = {"stacked": {"base": Path(base_dir).name, "amendments": [
        {"source": r.get("source"), "name": (r.get("output") or {}).get("name"), "effective_date": (r.get("output") or {}).get("effective_date"),
         "conditions": (r.get("output") or {}).get("conditions"), "ops": len((r.get("output") or {}).get("ops", [])),
         "cost": r.get("cost")} for r in reads], "log": logs, "review": review}}
    name = name or "stacked_" + "_".join(Path(p).stem.split("_")[-1][:30] for p in amendments)
    out, rep = ingest(ROOT / "data" / "pdf" / borrower / base.report["source_file"], borrower, blocks_in=blocks,
                      out_name=name, extra_report=extra)
    return out, extra["stacked"], reads


# ---------------------------------------------------------------- measuring against a real later conformed copy
def score_stack(stacked_dir, later_dir, log):
    S, L = Base(stacked_dir), Base(later_dir)
    sd, ld = S.defs(), L.defs()
    stext, _ = joined(S.blocks)
    ltext, _ = joined(L.blocks)
    rows = []
    for e in log:
        if not e.get("status", "").startswith("applied"):
            continue
        t = e["target"]
        term = t.split("(")[0].strip() if not t.startswith("Section") else None
        if term and term in sd and term in ld:
            a, b = squash(sd[term]["text"]), squash(ld[term]["text"])
        elif t.startswith("Section"):
            sid = re.sub(r"^Section\s+", "", t).split("(")[0]
            sa, sb = sec_span(S.blocks, sid, S.sections()), sec_span(L.blocks, sid, L.sections())
            if not sa or not sb:
                rows.append((t, None, "section missing in one copy"))
                continue
            a, b = squash(stext[sa[0]:sa[1]]), squash(ltext[sb[0]:sb[1]])
        else:
            rows.append((t, None, "target missing in one copy"))
            continue
        ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() if a != b else 1.0
        rows.append((t, round(ratio, 4), "identical" if a == b else "differs"))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("borrower")
    ap.add_argument("base")
    ap.add_argument("amendments", nargs="+")
    ap.add_argument("--compare")
    ap.add_argument("--name")
    a = ap.parse_args()
    out, st, reads = stack(a.borrower, ROOT / a.base, [ROOT / p for p in a.amendments], name=a.name)
    print(f"stacked bundle: {out}")
    for r in st["amendments"]:
        print(f"  {r['source']}: {r['name']} effective {r['effective_date']} ops {r['ops']} ${r['cost']}")
    for l in st["log"]:
        print(f"   - {l.get('n')} {l.get('op')} {l.get('target')}: {l['status']} {l.get('why', '')}")
    print("  review:", st["review"])
    if a.compare:
        for t, ratio, what in score_stack(out, ROOT / a.compare, st["log"]):
            print(f"  vs later conformed: {t}: {what} {ratio if ratio is not None else ''}")


if __name__ == "__main__":
    main()
