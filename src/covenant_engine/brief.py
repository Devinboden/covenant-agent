"""Plain-English covenant brief and review levels, from the abstract (code only; no model writes these sentences).

- covenant_sheet(ab): each financial covenant as a sentence a credit analyst would write, how it is calculated (what is
  in the numerator / denominator, add-backs and their caps), when it is tested, step-ups / relief, cure, reporting,
  pricing grid, the last reported value with cushion (code arithmetic), and other ratio tests found in the agreement.
- statement(ab, item, fields): what the abstract says for one review item, as text segments with the editable numbers
  embedded (segment {"f": i} = item field i), so the analyst can fix a number in the sentence itself.
- level(item, gpt): which review level an item gets (decide / confirm / auto) from criticality x model accuracy x GPT.
"""
import re

DIR = {"max": "must not exceed", "min": "must not fall below"}
FREQ = {"quarterly": "at the end of each fiscal quarter", "monthly": "at the end of each month", "annually": "at each fiscal year end"}
UNIT_WORD = {"months": "months", "quarters": "fiscal quarters", "test_dates": "test dates", "years": "years"}
CAT = {"taxes": "income taxes", "interest": "interest expense", "depreciation_amortization": "depreciation and amortization",
       "other_non_cash_charges": "other non-cash charges", "restructuring_business_optimization": "restructuring costs",
       "transaction_costs": "transaction costs", "extraordinary_unusual_items": "extraordinary / unusual items",
       "litigation_regulatory": "litigation costs", "impairment_writedowns": "impairments and write-downs",
       "hedging_mark_to_market": "mark-to-market hedging losses", "discontinued_operations": "discontinued operations",
       "debt_extinguishment": "debt extinguishment costs", "non_cash_compensation": "non-cash stock compensation",
       "projected_savings": "projected cost savings and synergies", "insurance_recoveries": "insurance proceeds",
       "third_party_reimbursed_expenses": "expenses reimbursed by third parties", "non_cash_gains": "non-cash gains",
       "acquired_ebitda": "EBITDA of acquired businesses", "disposed_ebitda": "EBITDA of sold businesses",
       "cost_savings_implementation": "cost-saving implementation costs", "equity_plan_costs": "equity plan costs",
       "fx_losses": "FX losses", "fx_gains": "FX gains", "transition_closure_costs": "closure costs",
       "public_company_costs": "public company costs", "sponsor_management_fees": "sponsor fees", "custom": "a custom item"}


def cat_label(c):
    return CAT.get(c) or c.replace("_", " ")


def money(v):
    try:
        x = float(str(v).replace(",", ""))
    except ValueError:
        return str(v)
    return f"${x / 1e6:,.1f}M" if x >= 1e6 else f"${x:,.0f}"


def level_text(c, v):
    if v in (None, ""):
        return "—"
    return money(v) if c.get("unit") == "usd" else f"{float(v):.2f}x" if re.fullmatch(r"[\d.]+", str(v)) else str(v)


def _date(d):
    return d.get("date") if isinstance(d, dict) else d


def cap_text(cap):
    v, t = cap.get("value", ""), cap.get("type", "")
    if t == "pct_of_ebitda":
        try:
            return f"capped at {float(v) * 100:g}% of EBITDA"
        except ValueError:
            return f"capped at {v} of EBITDA"
    if t in ("fixed_amount", "per_transaction", "lifetime_amount") or (t == "other" and re.fullmatch(r"[\d.,]+", str(v))):
        return f"capped at {money(v)}" + (" per transaction" if t == "per_transaction" else "")
    if str(v).startswith("excess_over:"):
        return f"only the amount above {money(v.split(':', 1)[1])}"
    return f"limited: {cap.get('quote') or v}"[:140]


def _ratio_parts(ab, metric):
    t = next((x for x in ab.get("terms", []) if x["name"] == metric and x["family"] == "ratio"), None)
    if not t:
        return None
    num = [c for c in t["components"] if c["category"] in ("numerator_term", "numerator_amount") and c.get("ref_term")]
    less = [c for c in t["components"] if c["category"] in ("numerator_less_term", "numerator_less_amount")]
    den = [c for c in t["components"] if c["category"] == "denominator_term"]
    name = lambda c: c.get("ref_term") or (c.get("quote") or "")[:60]
    out = {"numerator": sorted({name(c) for c in num}), "less": [], "denominator": sorted({name(c) for c in den})}
    for c in less:
        caps = "; ".join(cap_text(k) for k in c.get("caps", []))
        out["less"].append(name(c) + (f" ({caps})" if caps else ""))
    out["less"] = sorted(set(out["less"]))
    return out


def term_summary(ab, name):
    """A defined term in a line plus its capped items, e.g. EBITDA: 33 adjustments, 4 capped."""
    t = next((x for x in ab.get("terms", []) if x["name"] == name), None)
    if not t:
        return None
    amount = [c for c in t["components"] if c["category"] not in ("calculation_rule", "measurement_period", "scope_entities")]
    adds = [c for c in amount if c["sign"] == "+"]
    subs = [c for c in amount if c["sign"] == "-"]
    capped = [f"{c['clause']} {CAT.get(c['category'], c['category'].replace('_', ' '))}: {'; '.join(cap_text(k) for k in c['caps'])}"
              for c in amount if c.get("caps")]
    custom = [c["clause"] for c in amount if c["category"] == "custom"]
    return {"name": name, "starts_from": t.get("starts_from", ""), "adds": len(adds), "subtracts": len(subs),
            "capped": capped, "custom": custom}


def regime_sentence(ab, g):
    covs = {c["id"]: c for c in ab["covenants"]}
    names = ", ".join(covs[x]["metric"] for x in g.get("applies_to", []) if x in covs) or "the covenant"
    dur = g.get("duration") or {}
    d = f" for {dur['count']} {UNIT_WORD.get(dur.get('unit'), dur.get('unit', ''))}" if dur.get("count") else ""
    trig = {"borrower_election": "if the borrower elects", "amendment": "under an amendment"}.get(g.get("trigger"), g.get("trigger") or "")
    if g["effect"] == "add_increment":
        s = f"{g['name']}: {trig}, the {names} level can rise by {g.get('increment') or '—'}x" + \
            (f" (to at most {g['ceiling']}x)" if g.get("ceiling") else "") + d + "."
    elif g["effect"] == "set_level":
        s = f"{g['name']}: {trig}, the {names} level becomes {g.get('level') or '—'}x" + d + "."
    elif g["effect"] == "replace_schedule":
        s = f"{g['name']}: {trig}, a different schedule of levels applies to {names}" + d + "."
    elif g["effect"] == "suspend_test":
        s = f"{g['name']}: {trig}, the {names} test is suspended" + d + "."
    else:
        s = f"{g['name']} ({g['type'].replace('_', ' ')}) applies to {names}" + d + "."
    mu = g.get("max_uses")
    s += f" Can be used at most {mu} time(s)." if mu else " No limit on the number of uses is stated."
    gap = g.get("min_gap") or {}
    if gap.get("count"):
        s += f" At least {gap['count']} {UNIT_WORD.get(gap.get('unit'), gap.get('unit', ''))} between uses."
    ee = g.get("early_exit") or {}
    if ee.get("after") == "revert_to_standard":
        s += " Afterwards it reverts to the standard level."
    return s


def covenant_sentence(ab, c):
    who = (ab.get("loan") or {}).get("borrower") or "The borrower"
    rows = c["schedule"]
    when = FREQ.get(c["frequency"], c["frequency"])
    if len(rows) == 1:
        lv = level_text(c, rows[0]["value"])
        s = f"{who}'s {c['metric']} {DIR.get(c['direction'], c['direction'])} {lv}, tested {when}."
    else:
        steps = []
        for r in rows:
            a, b = _date(r["start"]), _date(r["end"])
            span = (f"through {b}" if not a and b else f"from {a}" + (f" through {b}" if b else " onward") if a else "")
            steps.append(f"{level_text(c, r['value'])} {span}".strip())
        s = f"{who}'s {c['metric']} {DIR.get(c['direction'], c['direction'])}: " + "; ".join(steps) + f" (tested {when})."
    if c.get("applies_during"):
        g = next((g for g in ab["regimes"] if g["id"] == c["applies_during"]), None)
        s += f" Applies only during the {g['name'] if g else c['applies_during']}."
    return s


def reported(ab, cid, c):
    rows = sorted((r for r in ab.get("reported", []) if r.get("covenant_id") == cid), key=lambda r: r.get("period_end", ""))
    if not rows:
        return None
    r = rows[-1]
    try:
        v, t = float(r["value"]), float(r["threshold"])
    except (TypeError, ValueError):
        return {"period": r.get("period_end"), "value": r.get("value"), "doc": r.get("doc")}
    cushion = (t - v) if c["direction"] == "max" else (v - t)
    return {"period": r.get("period_end"), "value": level_text(c, r["value"]), "threshold": level_text(c, r["threshold"]),
            "cushion": level_text(c, f"{cushion:.4f}".rstrip("0").rstrip(".")), "passes": cushion >= 0, "doc": r.get("doc")}


def other_tests(b, abs_dir):
    import json
    from pathlib import Path
    from . import config
    f = Path(config.ROOT) / abs_dir / f"{b}.incurrence.json"
    return json.loads(f.read_text(encoding="utf-8"))["tests"] if f.exists() else []


def covenant_sheet(ab, others=()):
    loan = ab.get("loan") or {}
    covs = []
    for c in ab["covenants"]:
        parts = _ratio_parts(ab, c["metric"])
        defs = []
        if parts:
            for n in parts["numerator"] + [x.split(" (")[0] for x in parts["less"]] + parts["denominator"]:
                s = term_summary(ab, n)
                if s and s not in defs:
                    defs.append(s)
        else:
            s = term_summary(ab, c["metric"])
            if s:
                defs.append(s)
        covs.append({"id": c["id"], "ref": c["ref"], "metric": c["metric"], "sentence": covenant_sentence(ab, c),
                     "direction": c["direction"], "frequency": c["frequency"],
                     "levels": [{"from": _date(r["start"]) or "start", "to": _date(r["end"]) or "thereafter",
                                 "value": level_text(c, r["value"])} for r in c["schedule"]],
                     "formula": parts, "definitions": defs,
                     "regimes": [regime_sentence(ab, g) for g in ab["regimes"] if c["id"] in g.get("applies_to", [])],
                     "reported": reported(ab, c["id"], c), "quote": c.get("quote", "")})
    rp = loan.get("reporting") or {}
    cure = loan.get("equity_cure") or {}
    deal = {"reporting": (f"Annual financials within {rp.get('annual_days')} days of year end; quarterly within "
                          f"{rp.get('quarterly_days')} days of quarter end"
                          + ("; compliance certificate delivered with them." if rp.get("compliance_certificate") == "with_financials" else ".")
                          ) if rp.get("annual_days") else "",
            "fiscal_year_end": (loan.get("fiscal_year_end") or {}).get("value", ""),
            "cure": (f"Equity cure (Section {cure.get('section')}): equity contributed within {cure.get('deadline_count')} "
                     f"{str(cure.get('deadline_unit', '')).replace('_', ' ')} counts toward EBITDA"
                     + ("; " + "; ".join(f"{l['type'].replace('_', ' ')} {l['value']}" for l in cure.get("limits", [])) if cure.get("limits") else "") + "."
                     ) if cure.get("present") else "No equity cure right.",
            "calc_rules": [r.get("summary", "") for r in ab.get("calculation_rules", [])][:8],
            "deemed": [f"{d['term']} is deemed for some periods by {d.get('schedule') or 'a schedule'}"
                       + ("" if d.get("schedule_provided") else " (schedule not in the filed documents)") for d in ab.get("deemed_values", [])],
            "pricing": _pricing(ab), "other_tests": list(others)}
    return {"covenants": covs, "deal": deal}


def _pricing(ab):
    pg = ab.get("pricing_grid") or {}
    if not pg.get("present"):
        return None
    rows = []
    for L in pg["levels"]:
        lo = f"{'>' if not L['lower_inclusive'] else '≥'} {L['lower']}x" if L.get("lower") else ""
        hi = f"{'<' if not L['upper_inclusive'] else '≤'} {L['upper']}x" if L.get("upper") else ""
        m = {x["type"]: x["value"] for x in L["margins"]}
        rows.append({"level": L["level"], "range": " and ".join(x for x in (lo, hi) if x) or "all",
                     "sofr": m.get("term_sofr_spread", ""), "base": m.get("base_rate_spread", ""), "fee": m.get("commitment_fee", "")})
    return {"metric": pg.get("metric", ""), "rows": rows}


# ---------------- review items: what the abstract says, with editable numbers in the sentence ----------------

def statement(ab, item, fields):
    """Segments: {"t": text} or {"f": index into fields}. Built from the same fields a correction can change."""
    idx = {f["label"]: i for i, f in enumerate(fields)}
    seg = []
    T = lambda s: seg.append({"t": s}) if s else None
    F = lambda label: seg.append({"f": idx[label]}) if label in idx else None
    F_ = _seg_field(seg)
    if "Effect" in idx:                                     # a clause of a defined term (term check or GPT item)
        g = fields[idx["Effect"]]["group"]
        m = re.match(r"term:(.+):\d+$", item.get("id") or "")
        name = item.get("term") or (m.group(1) if m else g)
        clause = item.get("clause") or g[len(name):].strip()
        T(f"In {name}, clause {clause} is ")
        F("Effect")
        T(f" ({'added to' if (fields[idx['Effect']]['value'] if 'Effect' in idx else '+') == '+' else 'subtracted from'} the amount). Category: ")
        F("Category")
        caps = [l for l in idx if l.startswith("Cap ")]
        if caps:
            T(". Limits: ")
            for k, l in enumerate(caps):
                T(", " if k else "")
                F(l)
        else:
            T(". No cap stored; add one if the clause has a limit: ")
            F("Add a cap (fixed amount, e.g. 10000000)")
        T(".")
        return seg
    covs = {}
    for k, f in enumerate(fields):
        if f["path"][0] == "covenants" and f["path"][-1] == "value":
            covs.setdefault(f["path"][1], []).append(k)
    for n, (ci, ks) in enumerate(covs.items()):
        c = ab["covenants"][ci]
        usd = c.get("unit") == "usd"
        T(("" if not n else " ") + f"{c['metric']} ({c['ref']}) {DIR.get(c['direction'], 'is')} ")
        for j, k in enumerate(ks):
            r = c["schedule"][fields[k]["path"][3]]
            a_, b_ = _date(r["start"]), _date(r["end"])
            T("; " if j else "")
            T("$" if usd else "")
            seg.append({"f": k})
            T(("" if usd else "x") + (f" from {a_}" if a_ else "") + (f" through {b_}" if b_ else (" onward" if a_ else "")))
        T(f", tested {FREQ.get(c['frequency'], c['frequency'])}.")
    regs = {}
    for k, f in enumerate(fields):
        if f["path"][0] == "regimes":
            regs.setdefault(f["path"][1], {})[".".join(map(str, f["path"][2:]))] = k
    for gi, ks in regs.items():
        g = ab["regimes"][gi]
        trig = {"borrower_election": "if the borrower elects"}.get(g.get("trigger"), "when it applies")
        T(f" {g['name']}: {trig}, ")
        if g["effect"] == "add_increment" or g.get("increment"):
            T("the level rises by "); F_(ks, "increment"); T("x, to at most "); F_(ks, "ceiling"); T("x")
        elif g["effect"] == "set_level" or g.get("level"):
            T("the level becomes "); F_(ks, "level"); T("x")
        elif g["effect"] == "replace_schedule":
            T("its own schedule of levels replaces the standard one (rows on the Covenants tab)")
        else:
            T(f"{g['type'].replace('_', ' ')} applies ({g['effect'].replace('_', ' ')})")
        T(" for "); F_(ks, "duration.count"); T(" "); F_(ks, "duration.unit")
        T(". Max uses: "); F_(ks, "max_uses"); T(" (blank = no limit). Gap between uses: "); F_(ks, "min_gap.count"); T(" "); F_(ks, "min_gap.unit"); T(".")
    return seg


def _seg_field(seg):
    return lambda ks, name: seg.append({"f": ks[name]}) if name in ks else None


# ---------------- review levels ----------------
# Accuracy by kind of check. ILLUSTRATIVE where marked: placeholders until a person grades a sample; the level rules
# read these numbers, so replacing them with graded ones changes what is asked, with no code change.
ACCURACY = {
    "covenant_level": {"rate": 0.997, "label": "Covenant levels and test dates", "basis": "measured: 350/351 rows, 5 deals with answer keys"},
    "regime_terms": {"rate": 1.0, "label": "Step-up / relief terms", "basis": "measured: 27/27 fields, 5 deals"},
    "flag_note": {"rate": 0.95, "label": "Model notes where GPT reads the same values", "basis": "illustrative, to be graded"},
    "clause_uncapped": {"rate": 0.95, "label": "Uncapped clauses (add-back vs deduction)", "basis": "illustrative, to be graded"},
    "clause_capped": {"rate": 0.85, "label": "Clauses with a cap", "basis": "illustrative, to be graded"},
    "gpt_not_found": {"rate": 0.97, "label": "Clauses GPT couldn't locate (Claude's quote is verbatim)", "basis": "illustrative, to be graded"},
    "dates": {"rate": 0.80, "label": "Ambiguous dates, conflicts, cross-references", "basis": "illustrative, to be graded"},
}
CONFIRM_AT = 0.90       # a check at least this accurate, with GPT reading the same, is confirmed in a batch
HARD_FLAGS = ("conflict", "ambiguous_date", "dangling_reference", "missing_schedule")


def _lv(level, tier, why, acc=None):
    out = {"level": level, "tier": tier, "level_why": why}
    if acc:
        out["accuracy"] = {"key": acc, **ACCURACY[acc]}
    return out


def level(item, gpt, stored=None):
    """decide = full card, a person judges it; confirm = 'both models agree' batch, a person still clicks to accept.
    Tier 1 = changes pass/fail directly; Tier 2 = changes an input to the ratio. Nothing is ever auto-resolved."""
    kind, t = item.get("kind", ""), item.get("type")
    agrees = bool(gpt and gpt.get("total") and gpt["agree"] == gpt["total"])
    if kind == "review/gpt":
        claims = (gpt or {}).get("claims") or []
        if claims and all(c["verdict"] == "not_found" for c in claims) and ACCURACY["gpt_not_found"]["rate"] >= CONFIRM_AT:
            return _lv("confirm", 2, "GPT couldn't find this clause; Claude's quote is in the agreement word for word.", "gpt_not_found")
        return _lv("decide", 1, "The two models read the agreement differently.")
    if kind == "flag":
        if t in HARD_FLAGS:
            return _lv("decide", 1, f"{t.replace('_', ' ').capitalize()}: which dates and levels apply decides pass/fail.", "dates")
        if agrees and ACCURACY["flag_note"]["rate"] >= CONFIRM_AT:
            return _lv("confirm", 1, f"A note from the extraction model; GPT, answering on its own, read the same "
                                     f"{gpt['total']} value(s) for this covenant.", "flag_note")
        return _lv("decide", 1, "The extraction model raised a question that no second check has settled.")
    if kind == "review/3" and stored:
        if stored.get("caps"):
            return _lv("decide", 1, "A clause with a cap: the cap changes the covenant number.", "clause_capped")
        if agrees and ACCURACY["clause_uncapped"]["rate"] >= CONFIRM_AT:
            return _lv("confirm", 2, "Uncapped clause; GPT, answering on its own, reads the same effect.", "clause_uncapped")
        return _lv("decide", 2, "Code couldn't confirm this clause" + (" and GPT didn't either." if gpt else "."))
    return _lv("decide", 1, "Needs a document or judgment code can't supply.")
