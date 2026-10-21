"""Steps 4-5: find the financial covenant section(s) and classify the document.

Code first. Each function returns a decision plus `confidence` and `evidence`. When code
can't decide, confidence is "undecided" and the pipeline calls `model_fallback` (a small
model such as Haiku), which is off by default so local runs stay deterministic.
"""
import re

COV_TITLE = re.compile(r"financial\s+(covenant|condition\s+covenant|maintenance)", re.I)
RATIO_TITLE = re.compile(r"(leverage|coverage|fixed\s+charge|interest)\s+(expense\s+)?(coverage\s+)?ratio|minimum\s+(liquidity|ebitda|net\s+worth)|capital\s+expenditures", re.I)
TEST_LANG = re.compile(r"(permit|will not permit|shall not permit)[^.]{0,450}?(to exceed|to be (greater|less) than|greater than|less than)"
                      r"|(maintain|have)[^.]{0,200}?(not (less|greater) than|at least|of at least|no (less|more) than)", re.I)


def find_covenant_sections(sections, blocks):
    """Return (section ids, confidence, evidence)."""
    titled = [s for s in sections if COV_TITLE.search(s["title"] or "") and TEST_LANG.search(_text(s, blocks))]
    if titled:
        # Neighbours in the same article titled after a maintenance test (e.g. MEC 9.14 Capital Expenditures)
        arts = {s["article"] for s in titled}
        extra = [s for s in sections if s["article"] in arts and s not in titled
                 and re.search(r"^(Capital Expenditures|Minimum (Liquidity|EBITDA|Net Worth)|Maximum .*Ratio|.*Coverage Ratio|.*Leverage Ratio)$", (s["title"] or "").strip(), re.I)]
        ids = [s["id"] for s in sorted(titled + extra, key=lambda s: s["start"])]
        return ids, "high", f"title match: {[s['title'] for s in titled]}" + (f"; neighbouring tests: {[s['title'] for s in extra]}" if extra else "")
    # Fallback: sections titled after a ratio AND containing maintenance-test language, in negative covenants
    ratio = [s for s in sections if RATIO_TITLE.search(s["title"] or "") and TEST_LANG.search(_text(s, blocks))]
    if ratio:
        arts = {}
        for s in ratio:
            arts.setdefault(s["article"], []).append(s)
        best = max(arts.values(), key=len)                 # covenants cluster in one article
        return [s["id"] for s in best], "medium", f"ratio-titled sections with test language: {[(s['id'], s['title']) for s in best]}"
    return [], "undecided", "no titled or ratio-titled section with maintenance-test language"


def _text(sec, blocks):
    return " ".join(blocks[k]["text_live"] for k in range(sec["start"], sec["end"]))


DOC_PATTERNS = [
    ("10-Q", re.compile(r"FORM\s+10-Q|QUARTERLY REPORT PURSUANT", re.I)),
    ("10-K", re.compile(r"FORM\s+10-K|ANNUAL REPORT PURSUANT", re.I)),
    ("8-K", re.compile(r"FORM\s+8-K|CURRENT REPORT PURSUANT", re.I)),
    ("compliance_certificate", re.compile(r"COMPLIANCE CERTIFICATE", re.I)),
    ("amendment", re.compile(r"\bAMENDMENT\s+NO\.?\s*\d+|\b(FIRST|SECOND|THIRD|FOURTH|FIFTH|SIXTH|SEVENTH|EIGHTH|NINTH|TENTH)\s+AMENDMENT\b|INCREMENTAL ASSUMPTION AGREEMENT", re.I)),
    ("consent_or_waiver", re.compile(r"^\W*(CONSENT|WAIVER)\b|\bLIMITED WAIVER\b", re.I | re.M)),
    ("credit_agreement", re.compile(r"\b(CREDIT|LOAN|FINANCING)\s+AGREEMENT\b", re.I)),
]


def classify_document(blocks, n_defs, struck_chars):
    """Look at the first ~40 blocks (cover page) and the document's overall shape."""
    head = " ".join(b["text_raw"] for b in blocks[:40])
    found = sorted((m.start(), name) for name, rx in DOC_PATTERNS for m in [rx.search(head)] if m)
    hits = [name for _, name in found]                         # in order of first appearance on the cover
    evidence = {"cover_matches": hits, "definitions": n_defs, "struck_chars": struck_chars}
    if not hits:
        return {"doc_type": None, "confidence": "undecided", "evidence": evidence}
    # Whatever the document calls itself first wins: a 10-K cover mentions 10-Q, an amendment filed
    # as an exhibit mentions the 8-K it was attached to.
    if hits[0] in ("10-Q", "10-K", "8-K"):
        return {"doc_type": hits[0], "confidence": "high", "evidence": evidence}
    # A compliance certificate is short and titled as one; agreements merely mention it
    title = " ".join(b["text_raw"] for b in blocks[:6])
    if "compliance_certificate" in hits and n_defs < 20 and re.search(r"COMPLIANCE CERTIFICATE", title, re.I):
        return {"doc_type": "compliance_certificate", "confidence": "high", "evidence": evidence}
    if "amendment" in hits:
        has_full = n_defs >= 100                               # an attached conformed agreement carries the full definitions
        return {"doc_type": "amendment", "contains_full_agreement": has_full,
                "is_blackline": struck_chars > 50, "confidence": "high", "evidence": evidence}
    if "consent_or_waiver" in hits and n_defs < 100:
        return {"doc_type": "consent_or_waiver", "confidence": "medium", "evidence": evidence}
    if "credit_agreement" in hits:
        return {"doc_type": "credit_agreement", "contains_full_agreement": n_defs >= 100,
                "is_blackline": struck_chars > 50, "confidence": "high" if n_defs >= 100 else "medium", "evidence": evidence}
    return {"doc_type": hits[0], "confidence": "low", "evidence": evidence}


def model_fallback(task, context, enabled=False):
    """Hook for a small model (e.g. Haiku) when code is undecided. Disabled by default; when
    enabled it must return the same shape as the code path plus `decided_by: model`."""
    if not enabled:
        return {"decided_by": "none", "note": f"model fallback disabled; {task} left undecided for review"}
    raise NotImplementedError("wire to Bedrock / Claude API with a strict JSON schema")
