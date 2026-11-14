"""Corrections a person makes in review, applied to the abstract itself (not just noted).

Each review item points at part of the abstract. `fields(ab, item)` lists the fields a reviewer may change for that
item, with their current values: a step-up / relief regime (increment, ceiling, level, duration, max uses, min gap), a
covenant's schedule rows (level, dates), or a clause of a defined term (add-back or deduction, category, cap).
`apply(...)` checks every change is one of those fields, writes it, re-validates the whole abstract against the schema
(a correction can't break the abstract), and records before -> after with who and when in the sign-off record.

Code only: no model touches a correction. The abstract's content hash changes, so a verified abstract drops to draft.
"""
import copy
import re
from datetime import date

REGIME_FIELDS = [("increment", "Step-up increment (x)", "text"), ("ceiling", "Ceiling (x)", "text"),
                 ("level", "Level during the period (x)", "text"), ("duration.count", "Duration", "text"),
                 ("duration.unit", "Duration unit", ["quarters", "months", "test_dates", "years", "none"]),
                 ("max_uses", "Max number of uses (blank = no limit)", "text"),
                 ("min_gap.count", "Min gap between uses (blank = none)", "text"),
                 ("min_gap.unit", "Min gap unit", ["quarters", "months", "test_dates", "years", "none"])]
SIGNS = ["+", "-"]


NUMERIC_CAPS = ("fixed_amount", "pct_of_ebitda", "per_transaction", "lifetime_amount")


def _number_rule(ab, path, val):
    """The same rule the pipeline holds the model to: levels, increments and amounts are numbers; counts are whole
    numbers. None if the value is fine (blank is fine where the field allows it)."""
    v = val.replace(",", "")
    name = str(path[-1])
    if name == "value" and "schedule" in path:                            # a covenant level
        return None if re.fullmatch(r"\d+(\.\d+)?", v) else "a level is a number, e.g. 3.50"
    if path[0] == "regimes" and name in ("increment", "ceiling", "level"):
        return None if not v or re.fullmatch(r"\d+(\.\d+)?", v) else "a number, e.g. 0.50, or blank"
    if name in ("count", "max_uses"):
        return None if not v or re.fullmatch(r"\d+", v) else "a whole number, or blank"
    if name == "value" and "caps" in path:
        cap = _get(ab, path[:-1])
        if cap.get("type") in NUMERIC_CAPS and not re.fullmatch(r"\d+(\.\d+)?", v):
            return "this limit is a number (dollars, or a fraction for a % of EBITDA, e.g. 0.20)"
    return None


def _get(obj, path):
    for p in path:
        obj = obj[p]
    return obj


def _set(obj, path, value):
    for p in path[:-1]:
        obj = obj[p]
    obj[path[-1]] = value


def _regime_fields(ab, gi):
    g = ab["regimes"][gi]
    out = []
    for name, label, kind in REGIME_FIELDS:
        path = ["regimes", gi] + name.split(".")
        try:
            v = _get(ab, path)
        except (KeyError, TypeError, IndexError):
            continue
        out.append({"path": path, "group": f"{g['name']}", "label": label,
                    "type": "enum" if isinstance(kind, list) else kind, "options": kind if isinstance(kind, list) else None,
                    "labels": {"test_dates": "test dates", "none": "—"} if isinstance(kind, list) else None,
                    "value": "" if v is None else str(v)})
    return out


FIRST_TEST_LABELS = {"on_or_after_closing": "first quarter end on or after closing",
                     "first_full_quarter": "end of the first full quarter after closing"}


def _covenant_fields(ab, ci):
    c = ab["covenants"][ci]
    out = []
    if any("Closing Date" in r.get("quote", "") for r in c["schedule"]) and \
            ((ab.get("loan") or {}).get("closing_date") or {}).get("value"):
        out.append({"path": ["settings", "first_test"], "group": f"{c['metric']} ({c['ref']})",
                    "label": "First test after closing", "type": "enum", "options": list(FIRST_TEST_LABELS),
                    "labels": FIRST_TEST_LABELS, "value": (ab.get("settings") or {}).get("first_test", "on_or_after_closing")})
    for k, r in enumerate(c["schedule"]):
        span = f"{r['start'].get('date') or 'start'} to {r['end'].get('date') or 'thereafter'}"
        out.append({"path": ["covenants", ci, "schedule", k, "value"], "group": f"{c['metric']} ({c['ref']})",
                    "label": f"Level, {span}", "type": "text", "value": str(r["value"])})
        for side in ("start", "end"):
            if r[side].get("date"):
                out.append({"path": ["covenants", ci, "schedule", k, side, "date"], "group": f"{c['metric']} ({c['ref']})",
                            "label": f"{'From' if side == 'start' else 'To'} date (row {k + 1})", "type": "date",
                            "value": r[side]["date"]})
    return out


def _component_fields(ab, ti, pi, categories):
    t, p = ab["terms"][ti], ab["terms"][ti]["components"][pi]
    base = ["terms", ti, "components", pi]
    grp = f"{t['name']} {p['clause']}"
    out = [{"path": base + ["sign"], "group": grp, "label": "Effect", "type": "enum",
            "options": SIGNS if p["sign"] in SIGNS else [p["sign"]] + SIGNS,
            "labels": {"+": "added", "-": "subtracted", "+/-": "added or subtracted"}, "value": p["sign"]},
           {"path": base + ["category"], "group": grp, "label": "Category", "type": "enum",
            "options": categories if p["category"] in categories else [p["category"]] + categories, "value": p["category"]}]
    for k, cap in enumerate(p.get("caps", [])):
        out.append({"path": base + ["caps", k, "value"], "group": grp, "label": f"Cap {k + 1} ({cap['type']})",
                    "type": "text", "value": cap["value"]})
    out.append({"path": base + ["caps", "+"], "group": grp, "label": "Add a cap (fixed amount, e.g. 10000000)",
                "type": "text", "value": ""})
    return out


CURE_EFFECTS = ["add_to_ebitda", "reduce_debt", "add_to_ebitda_or_reduce_debt", "other", "n/a"]


def _loan_fields(ab, which):
    """Deal facts a card is about: the equity cure, the fiscal year end."""
    ln = ab.get("loan") or {}
    if which == "equity_cure" and isinstance(ln.get("equity_cure"), dict):
        ec, base = ln["equity_cure"], ["loan", "equity_cure"]
        yn = lambda v: "yes" if v else "no"
        return [{"path": base + ["present"], "group": "Equity cure", "label": "Equity cure in the agreement", "type": "bool",
                 "options": ["yes", "no"], "value": yn(ec.get("present"))},
                {"path": base + ["section"], "group": "Equity cure", "label": "Section", "type": "text", "value": ec.get("section", "")},
                {"path": base + ["effect"], "group": "Equity cure", "label": "What the cure money does", "type": "enum",
                 "options": CURE_EFFECTS, "labels": {"add_to_ebitda": "added to EBITDA", "reduce_debt": "reduces debt",
                 "add_to_ebitda_or_reduce_debt": "added to EBITDA or reduces debt", "other": "other", "n/a": "none"},
                 "value": ec.get("effect", "n/a")},
                {"path": base + ["deadline_count"], "group": "Equity cure", "label": "Deadline (number)", "type": "text",
                 "value": ec.get("deadline_count", "")},
                {"path": base + ["deadline_unit"], "group": "Equity cure", "label": "Deadline unit", "type": "enum",
                 "options": ["business_days", "days", "n/a"], "value": ec.get("deadline_unit", "n/a")},
                {"path": base + ["prepayment_required"], "group": "Equity cure", "label": "Cure money must prepay loans",
                 "type": "bool", "options": ["yes", "no"], "value": yn(ec.get("prepayment_required"))}]
    if which in ln and isinstance(ln[which], dict) and "value" in ln[which]:
        label = {"fiscal_year_end": "Fiscal year end (MM-DD)"}.get(which, which.replace("_", " ").capitalize())
        return [{"path": ["loan", which, "value"], "group": "Deal facts", "label": label, "type": "text", "value": ln[which]["value"]}]
    return []


def _add_fields(ab, term, clause=""):
    """Record a part the abstract doesn't have (a skipped term, a clause the model didn't return). It is checked the
    same way as the model's parts: clause and quote required, category from the library, a cap is a number."""
    grp = f"Add to {term}"
    return [{"path": ["add", term, "clause"], "group": grp, "label": "Clause (e.g. (g), base)", "type": "text", "value": clause},
            {"path": ["add", term, "sign"], "group": grp, "label": "Effect", "type": "enum", "options": SIGNS,
             "labels": {"+": "added", "-": "subtracted"}, "value": "+"},
            {"path": ["add", term, "category"], "group": grp, "label": "Category", "type": "enum", "options": _categories(),
             "value": "custom"},
            {"path": ["add", term, "quote"], "group": grp, "label": "Agreement text (copy it exactly)", "type": "text", "value": ""},
            {"path": ["add", term, "cap"], "group": grp, "label": "Limit, if any (dollars)", "type": "text", "value": ""}]


def _categories():
    from ..core.decompose_term import CATEGORIES
    return list(CATEGORIES)


def _targets(ab, item):
    from .related import find_flag
    f = find_flag(ab, item) if item.get("kind") == "flag" else None
    return (f or {}).get("targets") or []


def fields(ab, item):
    """Editable fields for one review row (a dict from review_data: kind, where, term, clause, ...)."""
    out = []
    kind = item.get("kind", "")
    if kind == "review/terms":                       # a term the covenants use that was never broken down: add its parts
        return _add_fields(ab, item.get("term", ""))
    if kind == "review/loan" and "cure" in (item.get("what") or "").lower():
        return _loan_fields(ab, "equity_cure")
    if kind == "review/gpt" and (item.get("id") or "").startswith("loan:"):
        return _loan_fields(ab, item["id"].split(":", 1)[1])
    if item.get("term") and item.get("clause"):                                  # a clause of a defined term
        for ti, t in enumerate(ab.get("terms", [])):
            for pi, p in enumerate(t.get("components", [])):
                if t["name"] == item["term"] and p.get("clause") == item["clause"]:
                    out += _component_fields(ab, ti, pi, _categories())
        if not out:                                  # the item names something that isn't a stored part
            t = next(((ti, t) for ti, t in enumerate(ab.get("terms", [])) if t["name"] == item["term"]), None)
            if t and item["clause"] == "cap_on_total":                           # the definition's overall limits
                out += [f for pi, p in enumerate(t[1]["components"]) if p.get("caps")
                        for f in _component_fields(ab, t[0], pi, _categories())]
            elif item["clause"] not in ("starts_from",):                          # a clause the model didn't return
                out += _add_fields(ab, item["term"], item["clause"])
    elif kind == "flag" and _targets(ab, item):          # a structured flag says exactly what it's about
        for t in _targets(ab, item):
            if t["kind"] == "covenant":
                out += [f for ci, c in enumerate(ab["covenants"]) if c["id"] == t["id"] for f in _covenant_fields(ab, ci)]
            elif t["kind"] == "regime":
                out += [f for gi, g in enumerate(ab["regimes"]) if g["id"] == t["id"] for f in _regime_fields(ab, gi)]
            else:
                from .clause_labels import key
                for ti, term in enumerate(ab.get("terms", [])):
                    if term["name"] == t["id"]:
                        for pi, p in enumerate(term["components"]):
                            if (key(p.get("clause")) == key(t["clause"])) if t["clause"] else p.get("caps"):
                                out += _component_fields(ab, ti, pi, _categories())
        seen = set()
        out = [f for f in out if not (tuple(map(str, f["path"])) in seen or seen.add(tuple(map(str, f["path"]))))]
    elif kind == "flag" or kind == "review/gpt":         # older abstracts: resolve the flag's free-text reference
        ref = item.get("where") or ""
        cid = (item.get("id") or "")
        # A flag names what it's about in the model's words: a section ("6.13"), several ("6.12/6.13"), a covenant's
        # ratio ("Total Net Leverage Ratio") or a regime ("Covenant Holiday Period"). Resolve each to its fields.
        refs = {r.strip().lstrip("§") for r in re.split(r"/|,|&|\band\b", ref) if r.strip()}
        low = ref.lower()
        under = lambda cr: any(r == cr or r.startswith(cr + "(") or cr.startswith(r + "(") for r in refs)
        covs = [i for i, c in enumerate(ab["covenants"]) if cid.startswith(f"cov:{c['id']}") or
                (c["ref"] and (c["ref"] == ref or under(c["ref"]))) or (c.get("metric") and c["metric"].lower() == low)]
        if not covs:                                 # a sibling subsection (the relief table 6.09(a)(ii) of 6.09(a)(i)):
            refs = {re.sub(r"\([^()]*\)$", "", r) for r in refs if r.endswith(")")}     # the parent section's covenant
            covs = [i for i, c in enumerate(ab["covenants"]) if c["ref"] and refs and under(c["ref"])]
        cov_ids = {ab["covenants"][i]["id"] for i in covs}
        named = [i for i, g in enumerate(ab["regimes"]) if g.get("name") and g["name"].lower() == low]
        regs = named or [i for i, g in enumerate(ab["regimes"]) if set(g.get("applies_to", [])) & cov_ids or cid.startswith(f"reg:{g['id']}")]
        if kind == "flag":                       # a ratio's own definition: the parts that carry a limit (e.g. cash netting)
            for ti, t in enumerate(ab.get("terms", [])):
                if t["name"].lower() == low:
                    for pi, p in enumerate(t["components"]):
                        if p.get("caps"):
                            out += _component_fields(ab, ti, pi, _categories())
        for gi in regs:
            out += _regime_fields(ab, gi)
        for ci in covs:
            out += _covenant_fields(ab, ci)
        m = re.match(r"term:(.+):(\d+)$", cid)
        if m:
            for ti, t in enumerate(ab.get("terms", [])):
                if t["name"] == m.group(1) and int(m.group(2)) < len(t["components"]):
                    out += _component_fields(ab, ti, int(m.group(2)), _categories())
    return out


class CorrectionError(ValueError):
    pass


def apply(ab, item, changes, schema):
    """Return (new abstract, [{label, group, path, before, after}]). Raises CorrectionError if a change isn't one of the
    item's fields, a value is out of its options, or the result fails the abstract schema."""
    allowed = {tuple(map(str, f["path"])): f for f in fields(ab, item)}
    new = copy.deepcopy(ab)
    log = []
    adds = {}
    for ch in changes:
        path = [int(p) if isinstance(p, int) or (isinstance(p, str) and p.isdigit()) else p for p in ch.get("path", [])]
        f = allowed.get(tuple(map(str, path)))
        if not f:
            raise CorrectionError(f"{'/'.join(map(str, path))} is not a field of this item")
        val = str(ch.get("value", "")).strip()
        if path[0] == "add":                                                    # collected, applied as one part below
            adds.setdefault(path[1], {})[path[2]] = val
            continue
        if f["type"] in ("enum", "bool") and val not in f["options"]:
            raise CorrectionError(f"{f['label']}: {val!r} is not one of {f['options']}")
        if f["type"] == "date" and val and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
            raise CorrectionError(f"{f['label']}: dates are YYYY-MM-DD")
        why = _number_rule(new, path, val)
        if why:
            raise CorrectionError(f"{f['label']}: {why}")
        if path[-1] == "+":                                                      # add a fixed-amount cap
            if not val:
                continue
            if not re.fullmatch(r"\d+(\.\d+)?", val.replace(",", "")):
                raise CorrectionError("a new cap is a fixed amount in dollars, e.g. 10000000")
            caps = _get(new, path[:-1])
            parent = _get(new, path[:-2])                                # the clause the cap belongs to
            caps.append({"type": "fixed_amount", "value": val.replace(",", ""), "from": "", "to": "", "during": "",
                         "covenant": "all", "pool": "", "basis": "n/a", "quote": parent.get("quote", "")})
            log.append({"group": f["group"], "label": "Cap added", "path": path, "before": "none", "after": val})
            continue
        before = f["value"]
        if val == before:
            continue
        if path[0] == "settings":
            new.setdefault("settings", {})
        _set(new, path, (val == "yes") if f["type"] == "bool" else val)
        log.append({"group": f["group"], "label": f["label"], "path": path, "before": before or "—", "after": val or "—"})
        if path == ["settings", "first_test"]:
            log += _apply_first_test(new, val)
    for term, a in adds.items():
        log += _add_part(new, term, {**{f["path"][2]: f["value"] for f in allowed.values() if f["path"][:2] == ["add", term]}, **a})
    if not log:
        raise CorrectionError("nothing changed: change a field, or Accept if the abstract is right")
    import jsonschema
    errs = list(jsonschema.Draft202012Validator(schema).iter_errors({k: v for k, v in new.items()}))
    if errs:
        e = errs[0]
        raise CorrectionError(f"that change breaks the abstract: {'/'.join(map(str, e.path))}: {e.message[:120]}")
    return new, log


def _add_part(ab, term, a):
    clause, quote, cap = (a.get("clause") or "").strip(), re.sub(r"\s+", " ", a.get("quote") or "").strip(), (a.get("cap") or "").replace(",", "").strip()
    if not clause or len(quote) < 15:
        raise CorrectionError("to add a part, give its clause and copy its text from the agreement (at least a phrase)")
    if cap and not re.fullmatch(r"\d+(\.\d+)?", cap):
        raise CorrectionError("a limit is a dollar amount, e.g. 25000000")
    from .clause_labels import label
    t = next((t for t in ab["terms"] if t["name"] == term), None)
    if t is None:                                    # a skipped term: it becomes a term, no longer pending
        t = {"name": term, "family": "other", "starts_from": "", "components": [], "review": []}
        ab["terms"].append(t)
        ab["terms_pending"] = [x for x in ab.get("terms_pending", []) if x != term]
    t["components"].append({"clause": label(clause), "category": a.get("category") or "custom", "ref_term": "", "ref_clauses": "",
                            "sign": a.get("sign") or "+", "quote": quote, "source": "n/a", "needs_judgment": False,
                            "note": "added by the reviewer", "caps": [{"type": "fixed_amount", "value": cap, "from": "", "to": "",
                                                                       "during": "", "covenant": "all", "pool": "", "basis": "n/a",
                                                                       "quote": quote}] if cap else []})
    return [{"group": f"Add to {term}", "label": f"Part added: {label(clause)}", "path": ["terms", term], "before": "none",
             "after": f"{a.get('sign') or '+'} {quote[:60]}"}]


def _apply_first_test(ab, rule):
    """Code, not the reviewer, works out the dates: every quarterly row that runs from the Closing Date gets the first
    test date the chosen reading gives."""
    from datetime import date
    from .covenants import first_test_date
    cd = ((ab.get("loan") or {}).get("closing_date") or {}).get("value") or ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cd):
        return []
    first = first_test_date(date.fromisoformat(cd), rule).isoformat()
    log = []
    for ci, c in enumerate(ab["covenants"]):
        if c.get("frequency") != "quarterly":
            continue
        for k, r in enumerate(c["schedule"]):
            if "Closing Date" in r.get("quote", "") and r["start"].get("kind") == "date" and r["start"].get("date") != first:
                log.append({"group": f"{c['metric']} ({c['ref']})", "label": f"From date (row {k + 1})",
                            "path": ["covenants", ci, "schedule", k, "start", "date"], "before": r["start"]["date"], "after": first})
                r["start"]["date"] = first
    return log


def summary(log):
    return "; ".join(f"{x['group']}: {x['label']} {x['before']} → {x['after']}" for x in log)


def stamp(log, by):
    d = date.today().isoformat()
    return [x | {"by": by, "date": d} for x in log]
