"""Score the loan, events, calculation-rule, deemed-value and pricing sections of finished abstracts against the
answer keys (the same keys as score_loan / score_events / score_calc / score_pricing, which score raw run folders).
Each function takes a loader `ab(deal) -> abstract` and returns (ok, total, misses).

  python -m covenant_engine.abstract.score_sections            (stored abstracts in data/abstract/v1)
"""
from ..store import store
from .score_calc import DEEMED, RULES
from .score_events import REGIME_STATE, REPORTED
from .score_loan import EXPECTED as LOAN_EXPECTED
from .score_loan import score as score_loan_one
from .score_pricing import KEY as PRICING_KEY
from .score_pricing import f


def loan(ab):
    ok = n = 0
    misses = []
    for b in LOAN_EXPECTED:
        for field, good, got, want in score_loan_one(b, ab(b)["loan"]):
            n += 1
            ok += good
            if not good:
                misses.append(f"{b} {field}: got {got} want {want}")
    return ok, n, misses


def events(ab):
    ok = n = 0
    misses = []
    for b, ref, val, th in REPORTED:
        a = ab(b)
        ids = {c["id"] for c in a["covenants"] if c["ref"].replace(" ", "").startswith(ref)}
        got = [x for x in a.get("reported", []) if x["covenant_id"] in ids]
        n += 2
        v_ok = any(abs(float(x["value"]) - val) < 1e-6 for x in got)
        t_ok = any(x["threshold"] and abs(float(x["threshold"]) - th) < 1e-6 for x in got)
        ok += v_ok + t_ok
        if not (v_ok and t_ok):
            misses.append(f"{b} {ref}: got {[(x['value'], x['threshold']) for x in got]} want {val} / {th}")
    for b, word, (state, d) in REGIME_STATE:
        a = ab(b)
        rids = {g["id"] for g in a["regimes"] if word in g["name"].lower()}
        evs = [e for e in a.get("events", []) if e["regime_id"] in rids]
        n += 1
        good = any(e["type"] == "regime_ended" and e["date"] == d for e in evs) if state == "ended" else \
            not any(e["type"] in ("regime_ended", "regime_started") for e in evs)
        ok += good
        if not good:
            misses.append(f"{b} {word}: expected {state} {d or ''}")
    return ok, n, misses


def calc(ab):
    ok = n = 0
    misses = []
    for b, want in RULES.items():
        rules = ab(b).get("calculation_rules", [])
        for t, th in want:
            n += 1
            hits = [x for x in rules if x["type"] == t]
            good = bool(hits) and (th is None or any(x["threshold"].replace(",", "") == th for x in hits))
            ok += good
            if not good:
                misses.append(f"{b} rule {t}")
    return ok, n, misses


def deemed(ab):
    ok = n = 0
    misses = []
    for b, want in DEEMED.items():
        got = {(x["entity"], x["period_end"], x["value"]) if x["source"] == "definition_text"
               else ("schedule", x["schedule"], x["schedule_provided"]) for x in ab(b).get("deemed_values", [])}
        n += 1
        ok += got == want
        if got != want:
            misses.append(f"{b} deemed: got {sorted(got)} want {sorted(want)}")
    return ok, n, misses


def pricing(ab):
    ok = n = 0
    miss = []
    for b, k in PRICING_KEY.items():
        o = ab(b).get("pricing_grid") or {"metric": None, "levels": [], "default_if_late": ""}
        n += 1
        ok += o["metric"] == k["metric"]
        got = {}
        for L in o["levels"]:
            got[(f(L["lower"]) if L["lower"] else None, f(L["upper"]) if L["upper"] else None)] = \
                {m["type"]: m.get("fraction") for m in L["margins"]}
        for i, (lo, hi, sofr, fee) in enumerate(k["levels"]):
            g = got.get((lo, hi))
            n += 3
            ok += g is not None
            ok += bool(g) and abs((g.get("term_sofr_spread") or -1) - sofr) < 1e-9
            ok += bool(g) and abs((g.get("commitment_fee") or -1) - fee) < 1e-9
            if not g:
                miss.append(f"{b} level {lo}-{hi} missing")
            if "abr" in k and g:
                n += 1
                ok += abs((g.get("base_rate_spread") or -1) - k["abr"][i]) < 1e-9
        if k.get("late_default"):
            n += 1
            ok += bool(o["default_if_late"])
        n += 1
        ok += len(o["levels"]) == len(k["levels"])
    return ok, n, miss


SECTIONS = {"loan": loan, "events": events, "calculation_rules": calc, "deemed_values": deemed, "pricing": pricing}


def score_all(abs_dir=None):
    st = store(abs_dir) if abs_dir else store()
    cache = {}

    def ab(b):
        if b not in cache:
            cache[b] = st.load(b, "abstract")
        return cache[b]
    return {name: fn(ab) for name, fn in SECTIONS.items()}


if __name__ == "__main__":
    for name, (ok, n, misses) in score_all().items():
        print(f"{name}: {ok}/{n}")
        for m in misses:
            print("   -", m)
