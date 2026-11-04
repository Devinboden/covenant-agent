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


def _covenant_fields(ab, ci):
    c = ab["covenants"][ci]
    out = []
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


def _categories():
    from ..core.decompose_term import CATEGORIES
    return list(CATEGORIES)


def fields(ab, item):
    """Editable fields for one review row (a dict from review_data: kind, where, term, clause, ...)."""
    out = []
    kind = item.get("kind", "")
    if item.get("term") and item.get("clause"):                                  # a clause of a defined term
        for ti, t in enumerate(ab.get("terms", [])):
            for pi, p in enumerate(t.get("components", [])):
                if t["name"] == item["term"] and p.get("clause") == item["clause"]:
                    out += _component_fields(ab, ti, pi, _categories())
    elif kind == "flag" or kind == "review/gpt":
        ref = item.get("where") or ""
        cid = (item.get("id") or "")
        covs = [i for i, c in enumerate(ab["covenants"]) if c["ref"] and (c["ref"] == ref or cid.startswith(f"cov:{c['id']}"))]
        cov_ids = {ab["covenants"][i]["id"] for i in covs}
        regs = [i for i, g in enumerate(ab["regimes"]) if set(g.get("applies_to", [])) & cov_ids or cid.startswith(f"reg:{g['id']}")]
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
    for ch in changes:
        path = [int(p) if isinstance(p, int) or (isinstance(p, str) and p.isdigit()) else p for p in ch.get("path", [])]
        f = allowed.get(tuple(map(str, path)))
        if not f:
            raise CorrectionError(f"{'/'.join(map(str, path))} is not a field of this item")
        val = str(ch.get("value", "")).strip()
        if f["type"] == "enum" and val not in f["options"]:
            raise CorrectionError(f"{f['label']}: {val!r} is not one of {f['options']}")
        if f["type"] == "date" and val and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", val):
            raise CorrectionError(f"{f['label']}: dates are YYYY-MM-DD")
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
        _set(new, path, val)
        log.append({"group": f["group"], "label": f["label"], "path": path, "before": before or "—", "after": val or "—"})
    if not log:
        raise CorrectionError("nothing changed: change a field, or Accept if the abstract is right")
    import jsonschema
    errs = list(jsonschema.Draft202012Validator(schema).iter_errors({k: v for k, v in new.items()}))
    if errs:
        e = errs[0]
        raise CorrectionError(f"that change breaks the abstract: {'/'.join(map(str, e.path))}: {e.message[:120]}")
    return new, log


def summary(log):
    return "; ".join(f"{x['group']}: {x['label']} {x['before']} → {x['after']}" for x in log)


def stamp(log, by):
    d = date.today().isoformat()
    return [x | {"by": by, "date": d} for x in log]
