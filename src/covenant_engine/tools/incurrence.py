"""Other ratio tests in the agreement (incurrence / permission tests), found by a code scan of the agreement text.

Maintenance covenants are tested every quarter; these are tested only when the borrower does something (takes on debt,
pays a dividend, designates an unrestricted subsidiary). They aren't in the covenant schedule, but an analyst wants to
see them next to it. Code only: a regex over the agreement text, so each row carries its sentence and section.
"""
import re

RATIO = r"((?:Consolidated |Total |Senior Secured |First Lien |Secured )*(?:Net )?(?:Leverage|Interest Coverage|Fixed Charge Coverage)(?: Ratio)?)"
CMP = (r"(is less than or equal to|is equal to or less than|is no greater than|does not exceed|would not exceed|shall not exceed|"
       r"not (?:to )?exceed|less than or equal to|is greater than or equal to|is equal to or greater than|is at least|"
       r"greater than or equal to|is less than|is greater than)")
PAT = re.compile(RATIO + r"[^.;]{0,80}?" + CMP + r"\s+(\d+\.\d{1,2})\s*(?:to|:)\s*1(?:\.00)?", re.I)
SEC = re.compile(r"^SECTION\s+(\d{1,2}\.\d{2})\.?\s+([^\n]{0,80})", re.M)
MAX = re.compile(r"less|not exceed|no greater", re.I)


PURPOSE = [("Restricted Payment", "Paying dividends / restricted payments"), ("Unrestricted Subsidiar", "Designating an unrestricted subsidiary"),
           ("Incremental", "Adding incremental debt"), ("Acquisition", "Acquisitions / investments"), ("Investment", "Acquisitions / investments"),
           ("Indebtedness", "Taking on more debt"), ("Early Termination", "Ending a relief period early"), ("Covenant Relief", "Ending a relief period early")]


def purpose(heading, sentence):
    for where in (heading, sentence):                      # the section heading says it best; else nearby wording
        for k, v in PURPOSE:
            if k.lower() in (where or "").lower():
                return v
    return "Other permission test"


def scan(text, maintenance_quotes=(), maintenance_sections=()):
    """[{ratio, test, level, section, heading, gates, context}] for ratio tests outside the maintenance covenants."""
    secs = [(m.start(), m.group(1), m.group(2).split(".")[0].strip()) for m in SEC.finditer(text)
            if not re.search(r"[A-Za-z.]\d{1,3}\s+(SECTION|ARTICLE)", text[m.start():text.find("\n", m.start())])]   # not a TOC line
    maint = {re.sub(r"\s+", " ", q)[:60] for q in maintenance_quotes if q}
    out, seen = [], set()
    for m in PAT.finditer(text):
        at = m.start()
        sec = next(((n, h) for s, n, h in reversed(secs) if s <= at), ("", ""))
        start = max(text.rfind("\n", 0, at), text.rfind(". ", 0, at)) + 1
        nl = text.find("\n", m.end())
        sentence = re.sub(r"\s+", " ", text[start:min(m.end() + 40, nl if nl > 0 else len(text))]).strip()
        if "|" in sentence or sec[0] in maintenance_sections:
            continue                                                       # a pricing-grid row, or the covenant section
        if any(q in re.sub(r"\s+", " ", text[at - 200:m.end() + 50]) for q in maint):
            continue                                                       # the maintenance covenant itself
        if float(m.group(3)) < 0.5:
            continue                                                       # a step (e.g. 0.25x), not a test level
        ratio = re.sub(r"\s+", " ", m.group(1)).strip()
        if "Ratio" not in ratio:
            ratio += " Ratio"
        key = (ratio.lower(), m.group(3), sec[0])
        if key in seen:
            continue
        seen.add(key)
        out.append({"ratio": ratio, "test": "max" if MAX.search(m.group(2)) else "min", "level": m.group(3),
                    "section": sec[0], "heading": sec[1], "gates": purpose(sec[1], text[max(0, at - 600):m.end()]), "context": sentence[:260], "found_by": "code scan"})
    return out
