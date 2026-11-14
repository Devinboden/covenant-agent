"""One way to write a clause label. The model writes "(g) (exclusion)", "(g)(exclusion)" or "( g )" for the same part;
everything that compares labels (fields to edit, evidence, GPT claims, priority) needs them to agree. Labels are made
canonical at build (label) and matched by key (key) wherever a review entry meets the stored components."""
import re


def label(s):
    """Canonical display form: single spaces, none inside or between brackets: '(g)(exclusion)', 'proviso (ii)'."""
    s = re.sub(r"\s+", " ", (s or "").strip())
    s = re.sub(r"\(\s*", "(", s)
    s = re.sub(r"\s*\)", ")", s)
    s = re.sub(r"\)\s+\(", ")(", s)
    return s


def key(s):
    """Comparison key: case and spacing ignored."""
    return re.sub(r"\s+", "", (s or "")).lower()


def canonical(ab):
    """Rewrite every component's clause, and every review entry's, in the canonical form (in place)."""
    for t in ab.get("terms", []):
        for c in t.get("components", []):
            if c.get("clause") is not None:
                c["clause"] = label(c["clause"])
    for r in ab.get("review", []):
        if r.get("clause"):
            r["clause"] = label(r["clause"])
    return ab


def match(ab, term, clause):
    """The stored component's exact clause for a (term, clause) written any way; the input if none matches."""
    k = key(clause)
    for t in ab.get("terms", []):
        if t["name"] == term:
            for c in t.get("components", []):
                if key(c.get("clause")) == k:
                    return c["clause"]
    return clause
