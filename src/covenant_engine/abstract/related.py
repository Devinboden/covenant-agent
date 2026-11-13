"""Post-processing for a flag (code only): the passages its reading rests on, and whether its answer depends on a
document that isn't in this filing.

A flag used to carry one quote, so a reviewer saw a table row and not the proviso or definition the model reasoned
from. Here every flag gets (1) the passages the model cited itself ("also", from the model step) and (2) passages code
can tie to it: the definition of any regime the flag names, and how that regime can end early.

An answer that depends on something outside the document (the agreement "as in effect immediately prior to the Third
Amendment", an annex that wasn't filed, text the model says it wasn't given) can't be confirmed from these pages.
Such a flag is always High priority, and says which document is needed.
"""
import csv
import re
from pathlib import Path

PRIOR = re.compile(r"as in effect (?:immediately )?prior to (?:the )?(\w+) Amendment", re.I)
NOT_GIVEN = re.compile(r"not (?:available|provided|included|present) in the (?:text|packet|document|filing)"
                       r"|not in the (?:text|packet|document) (?:provided|given)", re.I)
ORDINAL = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8,
           "ninth": 9, "tenth": 10, "eleventh": 11, "twelfth": 12}
_NTH = re.compile(r"(\d+)(?:st|nd|rd|th)\b", re.I)


def find_flag(ab, row):
    """The stored flag a review row is about (rows carry the flag's ref and note)."""
    return next((f for f in ab.get("flags", []) if f.get("ref") == row.get("where") and f.get("note") == row.get("what")),
                None)


def _named_regimes(ab, text):
    return [g for g in ab.get("regimes", []) if g.get("name") and g["name"].lower() in (text or "").lower()]


def passages(ab, flag):
    """[(label, quote)] beyond the flag's own quote: what the model cited, then the regimes the flag names."""
    out = [("Also relied on", q) for q in flag.get("also", []) if q]
    text = " ".join([flag.get("note", ""), flag.get("quote", "")] + flag.get("also", []))
    for g in _named_regimes(ab, text):
        out.append((f"Definition of {g['name']}", g.get("quote", "")))
        ex = g.get("early_exit") or {}
        if ex.get("allowed") and ex.get("quote"):
            out.append((f"How the {g['name']} can end early", ex["quote"]))
    return [(lab, q) for lab, q in out if q]


def _manifest(borrower):
    from ..config import ROOT
    p = Path(ROOT) / "data" / "manifest.csv"
    if not p.exists() or not borrower:
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if r.get("ticker") == borrower]


def _ord(label):
    m = _NTH.search(label or "")
    if m:
        return int(m.group(1))
    return next((n for w, n in ORDINAL.items() if re.search(rf"\b{w}\b", label or "", re.I)), None)


def outside(ab, flag, borrower=None):
    """None, or a sentence naming what the flag's answer needs that this filing doesn't contain."""
    def prior(texts):
        for t in texts:
            m = PRIOR.search(t or "")
            if m:
                n = ORDINAL.get(m.group(1).lower())
                earlier = [r["label"] for r in _manifest(borrower) if r.get("doc_type") in ("credit_agreement", "amendment")
                           and (n is None or (_ord(r["label"]) or 0) < n)]
                where = f" (earlier filings for this deal: {', '.join(earlier)})" if earlier else ""
                return f"the agreement as it stood before the {m.group(1).title()} Amendment{where}"
        return None
    # 1. the passages the flag itself rests on point to an earlier version
    own = prior([flag.get("quote", "")] + flag.get("also", []))
    if own:
        return own
    # 2. the model says it wasn't given the text: name what's missing if a regime the flag names points to it
    if NOT_GIVEN.search(flag.get("note", "")):
        named = _named_regimes(ab, flag.get("note", "") + " " + flag.get("quote", ""))
        return prior([x for g in named for x in (g.get("quote", ""), (g.get("early_exit") or {}).get("quote", ""))]) \
            or "text the agreement refers to that isn't in this filing"
    return None


def numbers(text):
    """Decimal figures (levels, ratios) a flag talks about: 3.50, 5.25. Dates and section numbers are left out."""
    return {float(x) for x in re.findall(r"(?<![\d.§])(\d{1,2}\.\d{2})(?![\d.])", text or "")}
