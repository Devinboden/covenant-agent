"""Score a v1 abstract against the hand/agent-built answer keys (5 deals). Same method as the step-2 spike: evaluate
schedules at every quarter-end 2025-2028 (standard from the covenant, relief from the relief regime's schedule), and
regimes by type + parameters. Terms: any abstract term whose name matches an answer-key term with quoted components
is scored by quote position (step-3 method). Known answer-key artifacts are excluded and listed in KEY_ARTIFACTS."""
import yaml

from ..ingest.run import ROOT
from ..core.run_round import nearest, positions
from datetime import date as _date

from ..core.step2 import EXPECTED, at, d, key_maintenance, key_rows, nref, num

# every quarter end the deals can be tested on (was 2025-2028: missed the MEC first-row start, 2026-10-13)
QTRS = [_date(y, m, dd) for y in range(2022, 2031) for m, dd in ((3, 31), (6, 30), (9, 30), (12, 31))]
from .decompose import Bundle

KEY_ARTIFACTS = {("CARS", "7.12"): "sale-leaseback cap is tested per transaction, not a maintenance covenant",
                 ("MEC", "9.14"): "pre-2026 $50M capex limit is history no longer in the live text"}
UNIT_Q = {"quarters": 1, "test_dates": 1, "months": 1 / 3, "years": 4}


def _rows(rs):
    return [(d(r["start"]["date"]) if r["start"]["date"] else None,          # code-resolved relative dates count
             d(r["end"]["date"]) if r["end"]["date"] else None, num(r["value"])) for r in rs]


def _q(dur):
    c = num(dur["count"])
    return None if c is None or dur["unit"] not in UNIT_Q else c * UNIT_Q[dur["unit"]]


def score_terms(b, ab):
    kt = {t["name"]: t for t in yaml.safe_load((ROOT / "data" / "abstracts" / f"{b}.yaml").read_text(encoding="utf-8"))["terms"]}
    live_sq = Bundle(b).live_sq
    out = {}
    for t in ab["terms"]:
        k = kt.get(t["name"])
        kq = [c.get("quote", "") for c in ((k or {}).get("components") or [])
              if c.get("quote") and not str(c.get("id", "")).endswith("equity_cure")]
        if not kq:
            continue
        kpos, mpos = positions(kq, live_sq), positions([c["quote"] for c in t["components"]] +      # cap quotes count (multi-cap)
                                                   [k["quote"] for c in t["components"] for k in c["caps"]], live_sq)
        out[t["name"]] = f"{sum(1 for p in kpos if nearest(p, mpos) is not None)}/{len(kq)}"
    return out


def score_abstract(b, ab, terms=True):
    """terms=False skips the term-part scoring, which needs the deal's intake bundle (fetched data)."""
    misses = []
    keys = key_maintenance(b)
    if keys is None:
        return {"summary": "no answer key", "misses": [], "terms": {}}
    found = pts = ok = n_keys = 0
    for kc in keys:
        base = nref(kc["ref"])
        if (b, base) in KEY_ARTIFACTS:
            continue
        n_keys += 1
        ms = [c for c in ab["covenants"] if nref(c["ref"]) == base or nref(c["ref"]).startswith(base + "(")]
        if not ms:
            misses.append(f"{kc['ref']} not found")
            continue
        found += 1
        ids = {c["id"] for c in ms}
        std = [(f, t, v, "standard") for c in ms for f, t, v in _rows(c["schedule"])]
        rel = [(f, t, v, "relief") for g in ab["regimes"] if g["type"] == "relief_period"
               for s in g["schedules"] if s["covenant_id"] in ids for f, t, v in _rows(s["rows"])]
        lives_in_regime = any(c["applies_during"] for c in ms)
        kr = key_rows(kc)
        key_has_relief = any(reg == "relief" for *_, reg in kr)
        if not rel and key_has_relief:
            # valid alternative: the dated table (incl. relief quarters) kept as the standard schedule, relief modelled
            # by its early-exit levels (MEC). Same engine answer; compare relief values against the table, and say so.
            rel = [(f, t, v, "relief") for f, t, v, _ in std]
            misses.append(f"{kc['ref']}: relief values carried in the standard table (accepted representation)")
        for regime in ("standard", "relief"):
            k_from = ((kc.get("applies") or {}).get("from"))
            for q in QTRS:
                if k_from and hasattr(k_from, "year") and q < k_from:
                    continue                          # the key's covenant doesn't exist yet (WEST liquidity from Feb 2024)
                kv = at(kr, q, regime)
                if kv is None:
                    continue
                pts += 1
                mv = at(std + rel, q, regime)
                if mv is None and (lives_in_regime or not key_has_relief):
                    mv = at(std + rel, q, "relief" if regime == "standard" else "standard")
                if mv is not None and abs(mv - kv) < 1e-6:
                    ok += 1
                elif len(misses) < 12:
                    misses.append(f"{kc['ref']} {regime} {q}: key {kv} model {mv}")
    exp_ok = exp_n = 0
    for e in EXPECTED.get(b, []):
        t = "step_up" if e["type"] == "step_up_election" else e["type"]
        cands = [g for g in ab["regimes"] if g["type"] == t]
        exp_n += len(e)
        if not cands:
            misses.append(f"regime {t} missing")
            continue
        best, bad = -1, []
        for g in cands:
            got = {"level": num(g["level"]), "increment": num(g["increment"]), "ceiling": num(g["ceiling"]),
                   "duration_quarters": _q(g["duration"]), "max_uses": num(g["max_uses"]), "min_gap_quarters": _q(g["min_gap"]),
                   "early_termination": g["early_exit"]["allowed"],
                   "after_early_termination": {num(x["value"]) for x in g["early_exit"]["after_levels"]}}
            s, gb = 1, []
            for k, v in e.items():
                if k == "type":
                    continue
                okk = got[k] == v if isinstance(v, (bool, set)) else (got[k] is not None and abs(got[k] - v) < 1e-6)
                s += okk
                if not okk:
                    gb.append(f"regime {t} {k}: key {v} model {got[k]}")
            if s > best:
                best, bad = s, gb
        exp_ok += best
        misses += bad
    ts = score_terms(b, ab) if terms else {}
    ebitda = "".join(f" | {n} {v}" for n, v in ts.items() if "EBITDA" in n)
    return {"summary": f"covenants {found}/{n_keys} schedule {ok}/{pts} regime fields {exp_ok}/{exp_n}{ebitda}",
            "misses": misses, "terms": ts}
