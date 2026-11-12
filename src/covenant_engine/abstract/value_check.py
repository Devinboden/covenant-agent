"""Value-in-quote check (code only): every covenant level and every numeric cap must appear in its own quote.

The quote check proves the quote is in the agreement; this proves the stored number is in the quote. Without it, a
misread level ("4.50" stored as "4.25") with a correct quote passes every code check. A failure becomes a review item.
"""
import re

MIN_QUOTE = 20                      # shorter quotes prove nothing about where a number came from

_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(%|percent|per cent|million|billion|thousand)?", re.I)
_SCALE = {"million": 1e6, "billion": 1e9, "thousand": 1e3}


def numbers_in(text):
    """Every number the text states, with its natural readings: 20% -> {20, 0.20}; $2.5 million -> {2.5, 2500000}."""
    out = set()
    for m in _NUM.finditer(text or ""):
        try:
            x = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        out.add(x)
        unit = (m.group(2) or "").lower()
        if unit in ("%", "percent", "per cent"):
            out.add(x / 100)
        elif unit in _SCALE:
            out.add(x * _SCALE[unit])
    return out


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def value_in_quote(value, quote):
    """None if fine, else why not. Non-numeric values (a formula, 'lesser of ...') are not this check's business."""
    x = _num(value)
    if x is None:
        return None
    q = re.sub(r"\s+", " ", quote or "").strip()
    table = "|" in q                     # a table row ('date | amount'): short by nature, and the column header
    if len(q) < (10 if table else MIN_QUOTE):    # often says the amounts are in thousands or millions
        return f"the quote is too short to show where {value} comes from"
    found = numbers_in(q)
    if table:
        found |= {y * f for y in found for f in (1e3, 1e6)}
    if not any(abs(x - y) <= 1e-9 * max(1.0, abs(y)) for y in found):
        return f"{value} doesn't appear in the quoted text"
    return None


def shown(cap):
    """How a reader writes the limit: 20%, $2,000,000."""
    x = _num(cap.get("value"))
    if x is None:
        return str(cap.get("value"))
    if cap.get("type", "").startswith("pct") or x < 1:
        return f"{x * 100:g}%"
    return f"${x:,.0f}"


def checks(ab):
    """Review entries for levels and caps whose value isn't in their own quote."""
    out = []
    for c in ab.get("covenants", []):
        for k, r in enumerate(c.get("schedule", [])):
            why = value_in_quote(r.get("value"), r.get("quote"))
            if why:
                out.append({"step": "value", "id": f"row:{c['id']}:{k}", "term": c.get("metric", "").split(":")[0],
                            "item": f"Covenant level {r.get('value')}: {why}", "evidence": r.get("quote", "")})
    for g in ab.get("regimes", []):
        for s in g.get("schedules", []):
            for k, r in enumerate(s.get("rows", [])):
                why = value_in_quote(r.get("value"), r.get("quote"))
                if why:
                    out.append({"step": "value", "id": f"rrow:{g['id']}:{s.get('covenant_id')}:{k}", "term": g.get("name", ""),
                                "item": f"{g.get('name', 'Step-up')} level {r.get('value')}: {why}", "evidence": r.get("quote", "")})
    for t in ab.get("terms", []):
        for comp in t.get("components", []):
            for cap in comp.get("caps", []):
                why = value_in_quote(cap.get("value"), cap.get("quote"))
                if why:
                    out.append({"step": "value", "term": t["name"], "clause": comp.get("clause", ""),
                                "item": f"Limit {shown(cap)}: {why.replace(str(cap.get('value')), shown(cap))}", "evidence": cap.get("quote", "")})
    return out
