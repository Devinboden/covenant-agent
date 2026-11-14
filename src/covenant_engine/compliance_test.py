"""What the covenant test sheet calculates, from the abstract's definitions (code only; no model call).

The test sheet is where an analyst enters the borrower's financial statement numbers and the covenants are calculated
the way this agreement defines them. Nothing here is specific to a deal: the lines come from the abstract.

- A block per defined term the covenants use (Consolidated EBITDA, Consolidated Interest Expense, Indebtedness, ...).
  Flow terms (EBITDA, interest, capex) take four quarters and sum them; balance terms (debt, cash) take one figure at
  the test date.
- Each add-back / deduction is a line. A line that names another defined term the sheet already builds links to that
  block's total instead of asking for the number twice (EBITDA's interest add-back = the Interest Expense block).
- Caps code can apply exactly become formulas: a fixed limit, a limit that changes during a relief period, "only the
  amount above $X", and a shared cap as a % of EBITDA over several add-backs (a pool). Caps that need facts the sheet
  doesn't have (per acquisition, lifetime use, lesser of two values) are shown as checks, never guessed.
- A ratio covenant is its definition's numerator over its denominator; a level covenant (e.g. a capex limit) is its
  term's total.

plan(ab) -> {"blocks": [block, ...] in dependency order, "tests": [test, ...]}
"""
import re

NOTE = {"measurement_period", "scope_entities", "calculation_rule", "location_or_account_requirement"}
POINT = {"debt", "cash"}                    # balance-sheet terms: one figure at the test date
PCT_OF = re.compile(r"(\d+(?:\.\d+)?)\s*%\s+of\b", re.I)
LEAD = re.compile(r"^\s*(?:plus/minus|plus|minus|less|and|or)?\s*(?:\((?:[ivxlc]+|[a-z]{1,2}|\d+)\)\s*)+", re.I)


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


NEXT = re.compile(r"[,;]?\s+(?:and\s+|or\s+|plus\s+|minus\s+)?\((?:[ivx]+|[a-z]{1,2})\)\s")


def _snip(q, n=78):
    q = LEAD.sub("", " ".join((q or "").split()))
    m = NEXT.search(q, 8)                                     # one clause per line: stop where the next one starts
    q = q[:m.start()] if m else q
    q = q[:1].upper() + q[1:]
    if len(q) <= n:
        return q.rstrip(" ,;:")
    cut = q[:n].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:") + "…"


def _terms(ab):
    return {t["name"].strip().lower(): t for t in ab.get("terms", [])}


def _ref(T, p, self_name=None):
    t = T.get((p.get("ref_term") or "").strip().lower())
    return t if t and t["name"] != self_name and t.get("family") != "ratio" else None


def _structural(p):
    c = p["category"]
    return c == "term_reference" or c.startswith(("numerator", "denominator"))


def ratio_term(ab, c):
    T = _terms(ab)
    t = T.get((c.get("metric") or "").lower()) or T.get((c.get("name") or "").lower())
    if t and t.get("family") == "ratio":
        return t
    return next((t for t in ab.get("terms", []) if t.get("family") == "ratio" and c["id"] in (t.get("used_by") or [])), None)


def metric_term(ab, c):
    T = _terms(ab)
    return T.get((c.get("metric") or "").lower()) or T.get((c.get("name") or "").lower())


def _section(c):
    return re.match(r"[\d.]+", c.get("ref") or "")


def _covenant_specific(cap, c):
    """None for a cap that applies to every covenant; True / False for one that names a section."""
    cov = (cap.get("covenant") or "all").strip().lower()
    if cov in ("all", "", "n/a"):
        return None
    m = _section(c) if c else None
    return bool(m and m.group(0).rstrip(".") in cov)


def caps_for(p, c=None, regimes=None, pool_ok=False):
    """Split a component's caps into (formula caps, pool, checks).
    formula caps: [("excess", X)] and [("min", X, regime_id or None)]; pool: (name, pct, basis, quote) or None;
    checks: text an analyst must confirm by hand."""
    regimes = regimes or {}
    seen, form, checks, pool = set(), [], [], None
    caps = p.get("caps", [])
    specific = [k for k in caps if _covenant_specific(k, c) is True]
    for k in caps:
        key = (k.get("type"), str(k.get("value")), k.get("during", ""), k.get("covenant", ""))
        if key in seen:
            continue
        seen.add(key)
        t, v = k.get("type", ""), str(k.get("value", ""))
        sp = _covenant_specific(k, c)
        if sp is False:
            continue                                          # names another covenant's section
        if t == "pct_of_ebitda" and pool_ok and _num(v) is not None:
            pool = (k.get("pool") or p.get("clause", ""), _num(v), k.get("basis") or "not_stated", k.get("quote", ""))
            continue
        if v.startswith("excess_over:") and _num(v.split(":", 1)[1]) is not None:
            form.append(("excess", _num(v.split(":", 1)[1])))
            continue
        if t in ("fixed_amount", "lifetime_amount", "other") and _num(v) is not None:
            if sp is None and specific and t == "other":
                continue                                      # a section-specific limit replaces the general one
            during = k.get("during", "")
            if during:
                rid = regimes.get(during.strip().lower())
                if not rid:
                    checks.append(f"During {during}: limit {v} (check by hand)")
                    continue
                form.append(("min", _num(v), rid))
            else:
                form.append(("min", _num(v), None))
            if t == "lifetime_amount":
                checks.append("Lifetime limit: count only what is left after earlier periods")
            continue
        checks.append(_cap_check(k))
    return form, pool, checks


def _cap_check(k):
    from .brief import cap_text
    t = k.get("type", "")
    if t == "per_transaction":
        return cap_text(k) + ": check each transaction"
    if t == "lesser_of":
        return "Lesser of " + " and ".join(str(k.get("value", "")).split("|"))
    return cap_text(k)


def _line(T, p, block, c=None, regimes=None, pool_ok=False):
    ref = _ref(T, p, block["name"] if block else None)
    form, pool, checks = caps_for(p, c, regimes, pool_ok)
    mult = None
    if p["category"].endswith("_amount"):
        m = PCT_OF.search(p.get("quote", ""))
        mult = float(m.group(1)) / 100 if m else None
    return {"clause": p.get("clause", ""), "sign": p.get("sign", "+"), "category": p["category"],
            "label": _snip(p.get("quote")) if p.get("quote") else p["category"].replace("_", " ").capitalize(),
            "kind": "input", "ref": ref["name"] if ref else None, "mult": mult, "point": None, "caps": form, "pool": pool,
            "checks": checks, "source": p.get("source", ""), "quote": p.get("quote", ""), "note": p.get("note", "")}


def plan(ab):
    T = _terms(ab)
    regimes = {g["name"].strip().lower(): g["id"] for g in ab.get("regimes", []) if g.get("type") == "relief_period"}
    order, busy = [], set()

    def visit(t):                                            # dependency order: a term after the terms it links to
        if t["name"] in order or t["name"] in busy:
            return
        busy.add(t["name"])
        for p in t["components"]:
            if p["category"] not in NOTE and _structural(p):
                u = _ref(T, p, t["name"])
                if u:
                    visit(u)
        busy.discard(t["name"])
        order.append(t["name"])

    covs = []
    for c in ab.get("covenants", []):
        if c.get("kind") == "ratio":
            rt = ratio_term(ab, c)
            if not rt:
                continue
            for p in rt["components"]:
                u = _ref(T, p)
                if u and p["category"] not in NOTE:
                    visit(u)
            covs.append((c, rt))
        else:
            mt = metric_term(ab, c)
            if mt:
                visit(mt)
                covs.append((c, mt))
    built = {n.lower() for n in order}

    blocks = []
    for name in order:
        t = T[name.lower()]
        b = {"name": t["name"], "family": t.get("family", ""), "point": t.get("family") in POINT, "lines": [],
             "pools": {}, "notes": [], "start": None}
        amount = [p for p in t["components"] if p["category"] not in NOTE]
        sf = (t.get("starts_from") or "").strip()
        if sf and len(sf) <= 60:
            b["start"] = sf
            b["lines"].append({"clause": "", "sign": "+", "category": "start", "label": sf, "kind": "input", "ref": None,
                               "mult": None, "point": b["point"], "caps": [], "pool": None, "checks": [], "source": "",
                               "quote": f"{t['name']} starts from {sf}.", "note": ""})
        cats = {p["category"] for p in amount}
        if len(amount) > 1 and len(cats) == 1 and not any(_structural(p) for p in amount):
            # every clause is one kind of item (a list of eligible investments): one figure, the clauses are the test
            clauses = [p.get("clause", "") for p in amount]
            checks = sorted({f"{p.get('clause', '')}: {_cap_check(k)}" for p in amount for k in p.get("caps", [])})
            b["lines"].append({"clause": f"{clauses[0]}–{clauses[-1]}", "sign": amount[0].get("sign", "+"),
                               "category": amount[0]["category"], "label": t["name"], "kind": "input", "ref": None,
                               "mult": None, "point": b["point"], "caps": [], "pool": None, "checks": checks,
                               "source": amount[0].get("source", ""), "quote": "; ".join(p.get("quote", "") for p in amount),
                               "note": "Only items that meet the listed clauses count"})
            amount = []
        if not t["components"] and not b["start"]:
            b["lines"].append({"clause": "", "sign": "+", "category": "total", "label": t["name"], "kind": "input",
                               "ref": None, "mult": None, "point": b["point"], "caps": [], "pool": None, "checks": [],
                               "source": "", "quote": "", "note": "Not broken down in the abstract: enter the total"})
        for p in amount:
            ln = _line(T, p, b, regimes=regimes, pool_ok=b["family"] == "ebitda")
            ln["point"] = b["point"]
            if ln["ref"] and ln["ref"].lower() in built and (_structural(p) or T[ln["ref"].lower()].get("family") not in POINT
                                                              and not b["point"]):
                ln["kind"] = "link"
                ln["label"] = ln["ref"] + (f" × {ln['mult']:.0%}" if ln["mult"] else "")
            if ln["pool"]:
                name, pct, basis, quote = ln["pool"]
                b["pools"].setdefault(name, {"pct": pct, "basis": basis, "quote": quote, "clauses": []})["clauses"].append(ln["clause"])
            b["lines"].append(ln)
        b["notes"] = [f"{p.get('clause', '')}: {_snip(p.get('quote'), 220)}" for p in t["components"]
                      if p["category"] == "calculation_rule" and p.get("quote")]
        blocks.append(b)

    tests = []
    for c, t in covs:
        if c.get("kind") != "ratio":
            tests.append({"covenant": c, "kind": "level", "term": t["name"], "num": [], "den": [], "ebitda_side": None,
                          "groups": []})
            continue
        sides = {"num": [], "den": []}
        for p in t["components"]:
            if p["category"] in NOTE or not p["category"].startswith(("numerator", "denominator")):
                continue
            ln = _line(T, p, None, c=c, regimes=regimes)
            if ln["ref"] and ln["ref"].lower() in built:
                ln["kind"] = "link"
                ln["label"] = ln["ref"] + (f" × {ln['mult']:.0%}" if ln["mult"] else "")
                ln["point"] = T[ln["ref"].lower()].get("family") in POINT
            elif (p.get("ref_term") or "").strip():           # a defined term the abstract doesn't break down: by name
                ln["label"] = p["ref_term"].strip() + (f" × {ln['mult']:.0%}" if ln["mult"] else "")
            sides["num" if p["category"].startswith("numerator") else "den"].append(ln)
        for side in sides.values():                           # inputs follow the side: balance figures or four quarters
            pt = any(x["kind"] == "link" and x["point"] for x in side)
            for x in side:
                if x["point"] is None:
                    x["point"] = x["source"] == "balance_sheet" or pt
        # lines that carry the same limits share them: the limit applies to their combined amount ("cash and permitted
        # investments in excess of $10M, not to exceed $240M"), not to each line
        groups = []
        for side, lines in sides.items():
            by = {}
            for i, x in enumerate(lines):
                if x["caps"]:
                    by.setdefault((x["sign"], tuple(x["caps"])), []).append(i)
            for (sign, caps), idx in by.items():
                if len(idx) > 1:
                    groups.append({"side": side, "lines": idx, "caps": list(caps), "sign": sign})
                    for i in idx:
                        lines[i]["caps"] = []
                        lines[i]["grouped"] = True
        fams = {T[x["ref"].lower()].get("family") for x in sides["num"] if x["kind"] == "link"}, \
               {T[x["ref"].lower()].get("family") for x in sides["den"] if x["kind"] == "link"}
        eb = "num" if "ebitda" in fams[0] else "den" if "ebitda" in fams[1] else None
        tests.append({"covenant": c, "kind": "ratio", "term": t["name"], "num": sides["num"], "den": sides["den"],
                      "ebitda_side": eb, "groups": groups})
    return {"blocks": blocks, "tests": tests}
