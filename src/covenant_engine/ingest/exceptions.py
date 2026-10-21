"""Exception path, three stages:  CODE DETECTS -> HAIKU PROPOSES -> CODE VERIFIES AND EXECUTES.

Haiku never writes a final value. It returns a judgment plus a pointer into the text (a quote,
a section id, a start point). Code checks the pointer, then takes the action itself with its own
rules: classifies from the phrase Haiku pointed at, adopts verified section ids, measures where a
definition ends. If verification fails or code's own rule disagrees, the item goes to needs_review.

  trigger                                   model sees                        code verifies
  ---------------------------------------   -------------------------------   --------------------------------------
  classify_document: undecided / low        cover page text (~5k chars)       evidence quote is on the cover
  find_covenant_sections: undecided/medium  list of section ids + titles      ids exist; quote is in that section;
                                                                              section has test language
  repair_definition: “Term” means ...       the paragraph(s) around it        start/end quotes found, in order;
    not captured AND term used in packet                                      text starts with the quoted term

Every record: task, trigger, proposal (model output), checks (pass/fail), action (what code did),
decided_by = "applied" | "no_change" | "needs_review".

Backend: `claude -p --model haiku --json-schema ...` (local testing). Swap `call_model` for
Bedrock / Claude API in production; the handlers don't change.
"""
import json
import os
import re
import subprocess
import time

from .locate import TEST_LANG

MODEL = os.environ.get("INTAKE_MODEL", "haiku")


def squash(s):
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", "", s)


def call_model(system, user, schema, model=MODEL, timeout=180):
    """All model calls go through covenant_engine.model (one gate: claude -p today, Bedrock at deployment)."""
    from ..model import call_model as _call
    return _call(system, user, schema, model=model, timeout=timeout)


SYSTEM = ("You help pre-process commercial loan documents. Answer only from the text provided. "
          "Every quote you return must be copied exactly, character for character, from the text provided.")


# ---------------------------------------------------------------- 1. classify document
CLASSIFY_SCHEMA = {"type": "object", "additionalProperties": False,
                   "required": ["doc_type", "evidence_quote"],
                   "properties": {"doc_type": {"type": "string", "enum": ["credit_agreement", "amendment", "10-Q", "10-K", "8-K",
                                                                          "compliance_certificate", "consent_or_waiver", "other"]},
                                  "evidence_quote": {"type": "string"}}}


def classify_document(blocks, n_defs, struck_chars):
    from .locate import DOC_PATTERNS
    cover = "\n".join(b["text_raw"] for b in blocks[:40])[:5000]
    user = (f"Document cover page (first ~40 paragraphs):\n<<<\n{cover}\n>>>\n"
            "What kind of document is this? evidence_quote = ONE continuous phrase copied exactly from the cover that "
            "identifies it, e.g. its title (under 25 words; do not join separate phrases, do not drop symbols like ☒).")
    r = call_model(SYSTEM, user, CLASSIFY_SCHEMA)
    rec = {"task": "classify_document", "trigger": "code undecided / low confidence", "model_call": r,
           "proposal": r.get("output"), "checks": []}
    if not r["ok"]:
        return {**rec, "decided_by": "needs_review", "why": "model call failed"}
    out = r["output"]
    # VERIFY: the pointer is real
    on_cover = _fragments_in_order(out["evidence_quote"], cover)
    rec["checks"].append({"evidence phrase on cover": on_cover})
    if not on_cover:
        return {**rec, "decided_by": "needs_review", "why": "evidence quote not on the cover"}
    # EXECUTE: code classifies the phrase Haiku pointed at, with its own patterns; code supplies the facts
    code_type = next((name for name, rx in DOC_PATTERNS if rx.search(out["evidence_quote"])), None)
    agree = code_type == out["doc_type"]
    rec["checks"].append({"code reads the phrase as": code_type, "agrees with model": agree})
    if not agree:
        return {**rec, "decided_by": "needs_review", "why": f"model says {out['doc_type']}, code reads the phrase as {code_type}"}
    result = {"doc_type": code_type, "contains_full_agreement": n_defs >= 100, "is_blackline": struck_chars > 50,
              "confidence": "model_pointer_code_classified", "evidence": {"quote": out["evidence_quote"]}}
    return {**rec, "decided_by": "applied", "action": "code classified from the model's evidence phrase", "result": result}


# ---------------------------------------------------------------- 2. find covenant sections
LOCATE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["section_ids", "evidence"],
                 "properties": {"section_ids": {"type": "array", "items": {"type": "string"}},
                                "evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                        "required": ["section_id", "quote"],
                                                                        "properties": {"section_id": {"type": "string"},
                                                                                       "quote": {"type": "string"}}}}}}


def find_covenant_sections(sections, blocks, code_ids, code_conf):
    def stext(s):
        return " ".join(blocks[k]["text_live"] for k in range(s["start"], s["end"]))
    listing = "\n".join(f"{s['id']} | {s['title'][:70]} | {stext(s)[:220]}" for s in sections)
    user = ("Below is every section of a credit agreement: id | title | opening text.\n<<<\n" + listing + "\n>>>\n"
            "Which sections contain the FINANCIAL MAINTENANCE COVENANTS: tests the borrower must meet every period "
            "(e.g. maximum leverage ratio, minimum interest or fixed charge coverage, minimum liquidity, maximum capital "
            "expenditures)? Not representations, not incurrence tests, not definitions. For each id give one exact quote "
            "from its opening text that shows the test.")
    r = call_model(SYSTEM, user, LOCATE_SCHEMA)
    rec = {"task": "find_covenant_sections", "trigger": f"code confidence {code_conf}", "code_answer": code_ids, "model_call": r,
           "proposal": r.get("output")}
    if not r["ok"]:
        return {**rec, "decided_by": "needs_review", "why": "model call failed"}
    by_id = {s["id"]: s for s in sections}
    ids, problems = [], []
    for sid in r["output"]["section_ids"]:
        s = by_id.get(sid)
        if not s:
            problems.append(f"{sid}: no such section")
            continue
        ev = next((e["quote"] for e in r["output"]["evidence"] if e["section_id"] == sid), "")
        txt = stext(s)
        if not ev or squash(ev) not in squash(txt):
            problems.append(f"{sid}: quote not found in section")
        elif not (TEST_LANG.search(txt) or re.search(r"\d\.\d\d\s*(to|:)\s*1\.00|\$\d", txt)):
            problems.append(f"{sid}: no test language")
        else:
            ids.append(sid)
    rec["model_answer"] = r["output"]["section_ids"]
    rec["checks"] = {"verified ids": ids, "problems": problems}
    if problems or not ids:
        return {**rec, "decided_by": "needs_review", "why": "; ".join(problems) or "model found nothing"}
    # EXECUTE: code adopts only verified ids and keeps its own answer when they match
    if code_ids and sorted(ids) == sorted(code_ids):
        return {**rec, "decided_by": "applied", "action": "code answer confirmed; confidence raised", "result": sorted(code_ids),
                "confidence": "high (model confirms code)"}
    if code_ids:
        return {**rec, "decided_by": "needs_review", "why": f"model {ids} disagrees with code {code_ids}", "result": code_ids}
    return {**rec, "decided_by": "applied", "action": "code adopted the verified section ids", "result": sorted(ids),
            "confidence": "model_pointer_code_verified"}


# ---------------------------------------------------------------- 3. repair a missed definition
REPAIR_SCHEMA = {"type": "object", "additionalProperties": False,
                 "required": ["is_definition", "start_quote", "end_quote", "reason"],
                 "properties": {"is_definition": {"type": "boolean"}, "start_quote": {"type": "string"},
                                "end_quote": {"type": "string"}, "reason": {"type": "string"}}}


def repair_definition(term, blocks, k):
    ctx_blocks = blocks[k:k + 4]
    ctx = "\n".join(b["text_live"] for b in ctx_blocks)[:8000]
    user = (f"The term “{term}” appears to be defined in this passage from a credit agreement's definitions section, "
            f"but our parser did not separate it from neighbouring definitions.\n<<<\n{ctx}\n>>>\n"
            f"Is “{term}” actually defined here (not just used)? If yes, give start_quote = the first 8-15 words of its "
            "definition, starting with the opening quotation mark of the term, and end_quote = the last 8-15 words of its "
            "definition (a definition ends right before the next quoted term's definition begins). Exact copies only.")
    r = call_model(SYSTEM, user, REPAIR_SCHEMA)
    rec = {"task": "repair_definition", "term": term, "trigger": "definition pattern not captured and term used in packet",
           "model_call": r, "proposal": r.get("output")}
    if not r["ok"]:
        return {**rec, "decided_by": "needs_review", "why": "model call failed"}
    out = r["output"]
    if not out["is_definition"]:
        return {**rec, "decided_by": "no_change", "result": None, "why": f"model says not a definition: {out['reason'][:150]}"}
    # Model judges WHETHER and WHERE it starts; code measures where it ENDS (next definition or
    # heading). A model can stop early at a sub-clause and still quote real text, so its end_quote
    # is only a cross-check.
    flat = re.sub(r"\s+", " ", ctx)
    s_at = _find(flat, out["start_quote"])
    if s_at < 0:
        return {**rec, "decided_by": "needs_review", "why": "start quote not found in passage"}
    head = squash(flat[s_at:s_at + 200]).lower()
    if not (head.startswith(squash("“")) and squash(f"“{term}”").lower() in head):   # allow aliases: “A” or “B” means
        return {**rec, "decided_by": "needs_review", "why": "start does not open with the quoted term (or an alias list containing it)"}
    text, end_block = _extend_to_boundary(blocks, k, out["start_quote"], term)
    model_end = _find(re.sub(r"\s+", " ", text), out["end_quote"], 0, end=True)
    rec["end_check"] = ("model end matches code boundary" if model_end >= len(re.sub(r"\s+", " ", text)) - 2
                        else "model stopped early; code boundary used" if model_end > 0 else "model end not found; code boundary used")
    rec["checks"] = {"start found": True, "opens with quoted term": True, "end": rec["end_check"]}
    return {**rec, "decided_by": "applied", "action": "code cut the definition from the model's start to the next definition/heading",
            "result": {"term": term, "text": text, "kind": "defined", "decided_by": "model_pointer_code_cut",
                                                     "page": blocks[k]["page"], "blocks": [blocks[k]["id"], blocks[end_block]["id"]],
                                                     "start": k, "end": end_block + 1}}


def _extend_to_boundary(blocks, k, start_quote, term, max_blocks=60):
    """From the definition's start, take text until the next definition start or section heading.
    Anchor on the model's start quote if it sits in this block, else on the quoted term itself
    (a definition can open at the end of one block and continue in the next)."""
    from .structure import HEAD, _def_starts
    first = re.sub(r"\s+", " ", blocks[k]["text_live"])
    s_at = _find(first, start_quote)
    if s_at < 0:
        s_at = _find(first, f"“{term}”")
    if s_at < 0:
        s_at = 0
    tail = first[s_at:]
    nxt = [p for p in _def_starts(tail) if p > 0]
    if nxt:
        return tail[:nxt[0]].strip(), k
    parts, j = [tail], k
    for j in range(k + 1, min(len(blocks), k + max_blocks)):
        t = blocks[j]["text_live"]
        if not t:
            continue
        starts = _def_starts(t)
        if HEAD.match(t) or (starts and starts[0] == 0):
            return " ".join(parts).strip(), j - 1
        if starts:
            parts.append(t[:starts[0]].strip())
            return " ".join(parts).strip(), j
        parts.append(t)
    return " ".join(parts).strip(), j


def _fragments_in_order(quote, text):
    """A quote with '...' is several fragments; each must appear verbatim, in order."""
    pos, t = 0, squash(text)
    for frag in [f for f in re.split(r"\.\.\.|…", quote) if f.strip()]:
        j = t.find(squash(frag), pos)
        if j < 0:
            return False
        pos = j + len(squash(frag))
    return True


def _find(flat, quote, start=0, end=False):
    """Find quote in flat text (tolerant of whitespace and curly quotes) at or after `start`.
    Returns the index where it starts, or where it ends when end=True."""
    q = squash(quote)
    if not q:
        return -1
    # map squashed index back to flat index
    idx, sq = [], []
    for i, ch in enumerate(flat):
        c = squash(ch)
        if c:
            sq.append(c)
            idx.append(i)
    s = "".join(sq)
    from_sq = next((j for j, i in enumerate(idx) if i >= start), len(idx))
    j = s.find(q, from_sq)
    if j < 0:
        return -1
    return idx[j + len(q) - 1] + 1 if end else idx[j]
