"""Reviewer-facing wording. The checks write their findings for the pipeline ("no library part fits", "set ref_term");
the screen shows what a credit person needs instead: what is uncertain about the covenant and what to look at.
Code only. The pipeline's own wording stays in the abstract and in the model's package as `check_detail`."""
import re

from .brief import cat_label, money

SIGN = {"+": "added", "-": "subtracted"}


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return str(v)
    return money(x) if x >= 1000 else f"{x:g}"


def _list(s):
    s = s.strip()
    if s in ("none", "—", ""):
        return "none"
    vals = re.findall(r"[\d.]+", s)
    return " / ".join(_num(v) for v in vals) if vals else s


def gpt_diff(text):
    """'caps none vs [125000000.0]' -> 'Limit: the abstract has none; GPT reads $125.0M.'"""
    t = (text or "").strip()
    if not t or "could not find" in t:
        return "GPT couldn't find this clause in the agreement."
    parts = []
    for d in t.split("; "):
        m = re.match(r"(caps|refers to|sign|level|direction|frequency|duration|fiscal year end|days|equity cure|margins|"
                     r"increment|ceiling|max_uses|[a-z_]+) (.+?) vs (.+)$", d)
        if not m:
            parts.append(d)
            continue
        k, mine, his = m.groups()
        label = {"caps": "Limit", "refers to": "Points to the defined term", "sign": "Added or subtracted",
                 "max_uses": "Max uses"}.get(k, k.replace("_", " ").capitalize())
        if k == "caps":
            mine, his = _list(mine), _list(his)
        if k == "sign":
            mine, his = SIGN.get(mine, mine), SIGN.get(his, his)
        his = "nothing" if his.strip() in ("—", "") else his.replace("_", " ")
        mine = mine.replace("_", " ")
        parts.append(f"{label}: the abstract has {mine}; GPT reads {his}.")
    return " ".join(parts)


def clause_label(c):
    """Pipeline clause labels -> how a reader would refer to that part of the definition."""
    c = (c or "").strip()
    fixed = {"base": "(main text)", "lead-in": "(opening words)", "minus lead-in": "(opening words of the deductions)",
             "cap_on_total": "(overall limit)", "starts_from": "(starting amount)",
             "finalsentence": "(last sentence)", "variant": "(covenant-specific version)", "proviso": "(proviso)"}
    if c in fixed:
        return fixed[c]
    m = re.match(r"segment (\d+)$", c)
    if m:
        return f"(part {m.group(1)})"
    m = re.match(r"excl(\(.+\))$", c)
    if m:
        return f"deduction {m.group(1)}"
    return c


EARNINGS = re.compile(r"EBITDA|EBIT\b|Net Income|Earnings|Cash Flow", re.I)


def _sign(ab, term, clause):
    return next((c.get("sign") for t in (ab or {}).get("terms", []) if t["name"] == term
                 for c in t["components"] if c.get("clause") == clause), None)


def _where(term, clause, ab=None, sign=None):
    """'Consolidated EBITDA add-back (j)' / 'deduction (iv)' for an earnings measure, the way a credit analyst says it;
    'Indebtedness clause (j)' elsewhere (an item added to debt isn't an add-back)."""
    if not term:
        return ""
    lab = clause_label(clause)
    if lab.startswith("deduction"):
        return f"{term} {lab}"
    if lab.startswith("(") and lab[1:4] in ("mai", "ope", "las", "cov", "pro", "par", "ove", "sta"):
        return f"{term} {lab}"
    if not lab:
        return term
    sign = sign or _sign(ab, term, clause)
    if EARNINGS.search(term) and sign in ("+", "-"):
        return f"{term} {'add-back' if sign == '+' else 'deduction'} {lab}"
    return f"{term} clause {lab}"


def clean(text):
    """Drop sentences written for the pipeline (field names like quarters_after, 'Correct: split ...' instructions)."""
    text = re.sub(r",?\s*(?:so|and) (?:it|this|they) (?:is|was|are|were) (?:modell?ed|recorded|stored|captured) (?:with|as) [^.;]*", "", text or "")
    keep = [x for x in re.split(r"(?<=[.;])\s+(?=[A-Z])", text)
            if not re.search(r"\b[a-z]+_[a-z_]+\b|^(Correct|Set|Give|Copy|Label|Extract):?\s|\bmodel\b|\bGPT check\b", x)]
    return " ".join(keep).rstrip(";") or (text or "")


def item_text(row, ab):
    """What the reviewer is being asked about, in plain words."""
    if row.get("kind") == "flag":
        return clean(row.get("what"))
    w = (row.get("what") or "").split(". Model's reading")[0]
    term, clause = row.get("term") or "", row.get("clause") or ""
    kind = row.get("kind", "")
    where = _where(term, clause, ab)
    if kind == "review/3":
        comp = next((c for t in ab.get("terms", []) if t["name"] == term for c in t["components"] if c.get("clause") == clause), {})
        how = SIGN.get(comp.get("sign"), "counted")
        if w.startswith("no library part fits"):
            return f"{where} isn't one of the usual adjustments. Check that it's {how} and whether it has a limit."
        if w.startswith("quote is not from the text of clause"):
            return f"The text recorded for {where} may come from a neighbouring clause. Check the highlighted text is this clause."
        if w.startswith("cap_on_total parts state different caps"):
            return f"Parts of {term} state different limits. Check which limit applies to which part."
        if w.startswith("clause not returned"):
            return f"{where} wasn't captured. Check whether it changes the amount."
        if w.startswith("base amount"):
            return f"It isn't clear what {term} starts from. Check the opening words of the definition."
        if w.startswith("this clause is the defined term"):
            m = re.search(r"'([^']+)'", w)
            return f"{where} points to another definition{f' ({m.group(1)})' if m else ''}. Check it should be read through that definition."
        if w.startswith("covenant-specific wording"):
            m = re.search(r"Section ([\w.()]+)", w)
            return f"{where} has wording that applies only to {('Section ' + m.group(1)) if m else 'one covenant'}. Check whether it changes that covenant."
        return clean(re.split(r";\s*(?:give|copy|set|confirm|extract|label)\b", w)[0].rstrip(".") + ".")
    if kind == "review/gpt":
        cid = row.get("id") or ""
        m = re.match(r"term:(.+):\d+$", cid)
        claim = ((ab.get("second_model_review") or {}).get("claims") or {}).get(cid, {})
        if m:
            comps = next((t["components"] for t in ab.get("terms", []) if t["name"] == m.group(1)), [])
            k = int(cid.rsplit(":", 1)[1])
            w2 = _where(m.group(1), comps[k].get("clause") if k < len(comps) else "", ab)
            if claim.get("verdict") == "not_found":
                return f"Confirm how {w2} is recorded against the evidence."
            return f"Check how {w2} is recorded against the evidence."
        if cid.startswith("loan:fiscal_year_end"):
            fy = ((ab.get("loan") or {}).get("fiscal_year_end") or {}).get("value", "")
            return f"The abstract has the fiscal year ending {fy}. Check the fiscal year end."
        if cid.startswith("reg:"):
            g = next((g for g in ab.get("regimes", []) if g["id"] == cid.split(":")[1]), None)
            return f"Check the terms of the {g['name'] if g else 'step-up'} against the evidence."
        return "Check this against the evidence."
    if kind == "review/loan" and "equity cure" in w:
        return "The agreement seems to have equity cure wording, but the abstract records no cure. Check whether the borrower can cure a breach with new equity."
    if kind in ("review/calc", "review/pricing"):
        m = re.search(r"Schedule ([\w.()]+)", w)
        sch = f"Schedule {m.group(1)}" if m else "a schedule"
        if kind == "review/pricing":
            return f"The pricing grid is on {sch}, which isn't in the public filings. Get it from the deal team or accept the abstract without it."
        term = re.search(r"values for (.+?) are on", w)
        return (f"{term.group(1) if term else 'Some amounts'} is set to fixed figures for certain periods on {sch}, which isn't in the "
                f"public filings. It only matters if a test period still uses those figures.")
    return w


def reading(note):
    """Claude's own note on a clause, without pipeline vocabulary ('no library category fits')."""
    t = re.sub(r"[;,.]?\s*no (?:library|standard) (?:part|category) fits\.?", ".", note or "", flags=re.I)
    t = re.sub(r"\s*\([^)]*\b(?:enum|category|library)\b[^)]*\)", "", t, flags=re.I)
    t = re.sub(r"\s*\.\s*\.", ".", t).strip(" ;,")
    return t if t not in (".", "") else ""


def where_label(row, ab=None):
    """Card title: what the item is about, in a reader's terms (a covenant by name, a definition and its clause,
    or the deal term concerned)."""
    kind, w0 = row.get("kind", ""), (row.get("what") or "")
    if kind == "review/calc":
        m = re.search(r"values for (.+?) are on (Schedule [\w.()]+)", w0)
        if m:
            return f"Deemed {m.group(1)} ({m.group(2)})"
    if kind == "review/pricing":
        return "Pricing grid"
    if kind == "review/terms":
        return f"{row.get('term')} (definition not broken down)"
    if kind == "review/loan" and "equity cure" in w0:
        return "Equity cure"
    if kind == "flag" and ab:
        ref = row.get("where") or ""
        c = next((c for c in ab.get("covenants", []) if c.get("ref") and (c["ref"] == ref or ref.startswith(c["ref"]))), None)
        if c:
            return f"{c['metric']}, §{c['ref']}"
    m = re.match(r"term:(.+):(\d+)$", row.get("id") or "")
    if row.get("term"):
        return _where(row["term"], row.get("clause"), ab)
    if m:
        t = next((t for t in (ab or {}).get("terms", []) if t["name"] == m.group(1)), None)
        k = int(m.group(2))
        return _where(m.group(1), t["components"][k].get("clause"), ab) if t and k < len(t["components"]) else m.group(1)
    w = row.get("where") or ""
    if w.startswith(("reg:", "cov:")):
        return w.split(":")[1].replace("_", " ")
    if w.startswith("loan:"):
        return w.split(":")[1].replace("_", " ").capitalize()
    return f"§{w}" if re.match(r"\d", w) else w or "Deal terms"


UNSURE = re.compile(r"\b(unclear|not stated|no (?:cap|limit|minimum|stated|start|end)|sets no|does not|doesn't|isn't|is not|"
                    r"ambiguous|may |might|assumed|cannot|can't|not found|missing|differs?|typo)\b", re.I)


def _sentences(t):
    return [x.strip() for x in re.split(r"(?<=[.;])\s+(?=[A-Z])", t or "") if x.strip()]


def _first(t, n=180):
    s = (_sentences(t) or [""])[0].rstrip(";")
    s = s if len(s) <= n else s[:n].rsplit(" ", 1)[0] + "…"
    return s if s.endswith((".", "…")) else s + "."


def _lc(s):
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def _d(iso):
    from datetime import date
    try:
        return date.fromisoformat(iso).strftime("%B %-d, %Y")
    except ValueError:
        return iso


def explain(row, ab, gpt=None):
    """One sentence for the reviewer: what this item is and why it was flagged. Plain words, no mention of which model
    or check raised it. Built by code from the item and the abstract."""
    kind, term, clause = row.get("kind", ""), row.get("term") or "", row.get("clause") or ""
    w = (row.get("what") or "").split(". Model's reading")[0]
    note = row.get("note") or ""
    if kind == "flag" and row.get("type") == "ambiguous_date" and ab:
        # A period that starts at the Closing Date: say what the abstract stores, not the model's guess in its note
        # (code sets the first test to the first quarter end on or after the Closing Date).
        q = re.sub(r"\s+", " ", row.get("evidence") or "")
        hit = next(((c, r) for c in ab.get("covenants", []) for r in c.get("schedule", [])
                    if q and re.sub(r"\s+", " ", r.get("quote", "")) == q and "Closing Date" in q), None)
        cd = ((ab.get("loan") or {}).get("closing_date") or {}).get("value")
        if hit and hit[1]["start"].get("date") and cd:
            from .abstract.covenants import FIRST_TEST
            rule = (ab.get("settings") or {}).get("first_test", "on_or_after_closing")
            return (f"This period starts at the Closing Date ({_d(cd)}) and the agreement doesn't name the first test; "
                    f"recorded as {_d(hit[1]['start']['date'])}, {FIRST_TEST[rule]}.")
    if kind == "flag":
        why = next((x for x in _sentences(clean(w)) if UNSURE.search(x) and not re.match(r"nothing\b", x, re.I)), None) \
            or _first(clean(w))
        lead = {"conflict": "Two parts of the agreement don't line up here", "ambiguous_date": "The dates here are ambiguous",
                "dangling_reference": "This refers to something that isn't in the agreement",
                "missing_schedule": "A schedule this relies on is missing"}.get(row.get("type"))
        return (f"{lead}: {_lc(why)}" if lead else f"Flagged because {_lc(why)}").rstrip(".") + "."
    if kind == "review/3":
        t, c = next(((t, c) for t in ab.get("terms", []) if t["name"] == term for c in t["components"] if c.get("clause") == clause), (None, {}))
        what = f"{_where(term, clause, ab)} is " + {"+": "added", "-": "subtracted"}.get(c.get("sign"), "part of the calculation")
        r = reading(note)
        if r:                                       # the card title already names the clause
            f1 = _first(r).rstrip(".")
            what = f1[:1].upper() + f1[1:]
        if w.startswith("no library part fits"):
            why = "it isn't one of the standard adjustments"
        elif w.startswith("quote is not from the text"):
            why = "the recorded text may belong to a neighbouring clause"
        elif w.startswith("cap_on_total"):
            why = "parts of the definition state different limits"
        elif w.startswith("clause not returned"):
            why = "this clause wasn't captured"
        elif w.startswith("this clause is the defined term"):
            why = "it points to another definition"
        elif w.startswith("covenant-specific wording"):
            why = "its wording applies to only one covenant"
        elif w.startswith("base amount"):
            why = "it isn't clear what the definition starts from"
        else:
            why = "its treatment in the definition is uncertain"
        return f"{what.rstrip('.')}; flagged because {why}."
    if kind == "review/gpt":
        cid = row.get("id") or ""
        m = re.match(r"term:(.+):(\d+)$", cid)
        diff = (row.get("gpt_reads") or "")
        if m:
            t = next((t for t in ab.get("terms", []) if t["name"] == m.group(1)), None)
            c = t["components"][int(m.group(2))] if t and int(m.group(2)) < len(t["components"]) else {}
            what = f"{_where(m.group(1), c.get('clause'), ab)} is {SIGN.get(c.get('sign'), 'counted')} in the calculation"
        else:
            what = "This term"
        if "could not find" in diff or not diff:
            why = "its wording in the agreement is uncertain"
        elif diff.startswith("caps none vs"):
            why = "the agreement's wording appears to set a limit the abstract doesn't record"
        elif diff.startswith("caps") and diff.endswith("vs none"):
            why = "the agreement's wording may not support the recorded limit"
        elif diff.startswith("caps"):
            why = "the limit amount may be different"
        elif diff.startswith("refers to"):
            why = "it may point to a different defined term"
        elif diff.startswith("sign"):
            why = "it may count the other way (added vs subtracted)"
        elif diff.startswith("duration"):
            why = "how long it lasts may be different"
        elif diff.startswith(("level", "increment", "ceiling")):
            why = "the level may be different"
        elif diff.startswith("fiscal year end"):
            why = "the fiscal year end couldn't be confirmed"
        else:
            why = "its reading of the agreement is uncertain"
        if cid.startswith("reg:"):
            g = next((g for g in ab.get("regimes", []) if g["id"] == cid.split(":")[1]), {})
            what = f"The {g.get('name', 'step-up')} changes the covenant level"
        if cid.startswith("loan:fiscal_year_end"):
            what = "The fiscal year end sets the test dates"
        return f"{what}; flagged because {why}."
    if kind == "review/value":
        what = w.split(":")[0]
        return f"{what} is recorded, but that number isn't in the quoted agreement text; flagged so the figure is checked against the agreement."
    if kind == "review/terms":
        return (f"{term} feeds the covenant calculation, but its parts weren't recorded in the abstract; flagged so its "
                f"definition is checked by hand.")
    if kind == "review/calc":
        m = re.search(r"values for (.+?) are on (Schedule [\w.()]+)", w)
        if m:
            return (f"{m.group(1)} is replaced by fixed figures from {m.group(2)} for some periods; flagged because "
                    f"that schedule isn't in the public filing.")
    if kind == "review/pricing":
        m = re.search(r"(Schedule [\w.()]+)", w)
        return (f"The pricing grid sets the loan margin by leverage level; flagged because it's on "
                f"{m.group(1) if m else 'a schedule'} that isn't in the public filing.")
    if kind == "review/loan" and "equity cure" in w:
        return ("The agreement may let the borrower fix a covenant breach with new equity; flagged because that wording "
                "was found but the abstract records no cure.")
    return f"{_first(item_text(row, ab)).rstrip('.')}."
