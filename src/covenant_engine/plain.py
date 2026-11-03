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
             "finalsentence": "(last sentence)", "variant": "(covenant-specific version)", "proviso": "(proviso)"}
    if c in fixed:
        return fixed[c]
    m = re.match(r"segment (\d+)$", c)
    if m:
        return f"(part {m.group(1)})"
    m = re.match(r"excl(\(.+\))$", c)
    if m:
        return f"exclusion {m.group(1)}"
    return c


def _where(term, clause):
    if not term:
        return ""
    lab = clause_label(clause)
    if lab.startswith("exclusion"):
        return f"{term} {lab}"
    return f"{term} {lab}" if lab.startswith("(") and lab[1:4] in ("mai", "ope", "las", "cov", "pro", "par") else f"{term} clause {lab}" if lab else term


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
    where = _where(term, clause)
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
            w2 = _where(m.group(1), comps[k].get("clause") if k < len(comps) else "")
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


def where_label(row):
    """Card header: a section number, or the definition and clause in reader's terms."""
    m = re.match(r"term:(.+):\d+$", row.get("id") or "")
    if row.get("term"):
        return _where(row["term"], row.get("clause"))
    if m:
        return m.group(1)
    w = row.get("where") or ""
    if w.startswith(("reg:", "cov:")):
        return w.split(":")[1].replace("_", " ")
    if w.startswith("loan:"):
        return w.split(":")[1].replace("_", " ").capitalize()
    return f"§{w}" if re.match(r"\d", w) else w or "Deal terms"
