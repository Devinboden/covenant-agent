"""Score the abstract's NON-EBITDA terms against the v1 keys (data/keys_v1/<B>.terms.json).

Matching: same term name; within a term, a key component matches the model component with the same clause label, else
the one whose quote sits at the same place in the agreement (any occurrence within 60 chars: sibling clauses share
openings). Fields scored on matched parts: category, sign, ref_term (when the key has one), cap (type + values, when
the key has one). Every disagreement is listed for triage: key error / model error / acceptable alternative.

Usage: python -m covenant_engine.abstract.score_keys WEST TRS MEC
"""
import json
import re
import sys

from ..ingest.exceptions import squash
from ..ingest.run import ROOT
from .clauses import norm
from .decompose import Bundle, cap_sig


def occurrences(hay, needle):
    out, i = [], hay.find(needle)
    while i >= 0 and needle:
        out.append(i)
        i = hay.find(needle, i + 1)
    return out


STOP = {"the", "of", "and", "or", "a", "an", "such", "thereby", "to", "in", "by", "for", "any", "on"}


def _vals(v):
    """Split a cap value into numbers (normalised) and prose (content words)."""
    nums, words = set(), set()
    for x in str(v).replace(",", "").replace("$", "").split("|"):
        x = x.strip()
        try:
            nums.add(round(float(x), 6))
        except ValueError:
            words |= {w for w in re.findall(r"[a-z]+", x.lower().replace("_", " ")) if w not in STOP}
    return nums, words


def cap_match(kc, mc):
    """Same SET of caps: each cap compared on type, limbs (numbers normalised, prose by content words), date range,
    'during' period and covenant section. Pool names are free text: only whether a cap is pooled is compared."""
    def sig(c):
        t, limbs, f, to, during, cov = cap_sig(c)
        nums = tuple(sorted(x for x in limbs if re.fullmatch(r"(excess_over:)?-?[\d.]+", x)))
        words = frozenset(w for x in limbs if not re.fullmatch(r"(excess_over:)?-?[\d.]+", x)
                          for w in re.findall(r"[a-z]+", x) if w not in STOP)
        return (t, nums, f, to, during, cov, bool(c["pool"])), words
    ks, ms = [sig(c) for c in kc["caps"]], [sig(c) for c in mc["caps"]]
    if len(ks) != len(ms):
        return False
    left = list(ms)
    for k, kw in ks:
        hit = next((i for i, (m, mw) in enumerate(left) if m == k and (not kw or len(kw & mw) / len(kw | mw) >= 0.4)), None)
        if hit is None:
            return False
        left.pop(hit)
    return True


def score(b, abs_dir="data/abstract/v1"):
    key = json.loads((ROOT / "data" / "keys_v1" / f"{b}.terms.json").read_text(encoding="utf-8"))
    ab = json.loads((ROOT / abs_dir / f"{b}.abstract.json").read_text(encoding="utf-8"))
    live = Bundle(b).live_sq
    model_terms = {t["name"]: t for t in ab["terms"]}
    tot = {"parts": 0, "found": 0, "category": 0, "sign": 0, "ref": 0, "ref_n": 0, "cap": 0, "cap_n": 0, "tcap": 0, "tcap_n": 0}
    diffs, lines = [], []
    missing_terms = [t["name"] for t in key["terms"] if t["name"] not in model_terms]
    for kt in key["terms"]:
        mt = model_terms.get(kt["name"])
        if not mt:
            continue
        mcomps = mt["components"]
        mpos = [occurrences(live, squash(c["quote"])[:80]) for c in mcomps]
        used, t = set(), {"parts": 0, "found": 0}
        for kc in kt["components"]:
            tot["parts"] += 1
            t["parts"] += 1
            cand = [i for i, c in enumerate(mcomps) if i not in used and norm(c["clause"]) == norm(kc["clause"])
                    and (c["category"] == kc["category"] or c["ref_term"] == kc["ref_term"])]
            if not cand:
                kp = occurrences(live, squash(kc["quote"])[:80])
                best = None
                for i, ps in enumerate(mpos):
                    if i in used:
                        continue
                    dist = min((abs(p - q) for p in ps for q in kp), default=None)
                    if dist is not None and dist <= 60 and (best is None or dist < best[0]):
                        best = (dist, i)
                cand = [best[1]] if best else [i for i, c in enumerate(mcomps) if i not in used and norm(c["clause"]) == norm(kc["clause"])]
            if not cand:
                diffs.append(f"{kt['name']} {kc['clause']} [{kc['category']} {kc['sign']}]: NOT FOUND in model")
                continue
            i = cand[0]
            used.add(i)
            mc = mcomps[i]
            tot["found"] += 1
            t["found"] += 1
            for f in ("category", "sign"):
                if mc[f] == kc[f]:
                    tot[f] += 1
                else:
                    diffs.append(f"{kt['name']} {kc['clause']}: {f} key {kc[f]!r} model {mc[f]!r}")
            if kc["ref_term"]:
                tot["ref_n"] += 1
                ok = mc["ref_term"] == kc["ref_term"] and (not kc["ref_clauses"] or
                                                           set(re.findall(r"\([^)]+\)", mc["ref_clauses"])) == set(re.findall(r"\([^)]+\)", kc["ref_clauses"])))
                tot["ref"] += ok
                if not ok:
                    diffs.append(f"{kt['name']} {kc['clause']}: ref key {kc['ref_term']}{kc['ref_clauses']} model {mc['ref_term']}{mc['ref_clauses']}")
            if kc["caps"]:
                tot["cap_n"] += 1
                ok = cap_match(kc, mc)
                tot["cap"] += ok
                if not ok:
                    fmt = lambda cs: "; ".join(f"{c['type']} {c['value']}{' ' + c['from'] + '..' + c['to'] if c['from'] or c['to'] else ''}"
                                               f"{' during ' + c['during'] if c['during'] else ''}{' @' + c['covenant'] if c['covenant'] not in ('all', '') else ''}"
                                               f"{' pool' if c['pool'] else ''}" for c in cs) or "none"
                    diffs.append(f"{kt['name']} {kc['clause']}: cap key [{fmt(kc['caps'])}] model [{fmt(mc['caps'])}]")
        # caps at TERM level: is every key cap somewhere in the model's term (limbs may sit on their own clause parts)
        ksig = {cap_sig(c) for p in kt["components"] for c in p["caps"]}
        msig = {cap_sig(c) for p in mcomps for c in p["caps"]}
        tot["tcap_n"] += len(ksig)
        tot["tcap"] += len(ksig & msig)
        diffs += [f"{kt['name']} TERM CAP missing: {k[0]} {'|'.join(k[1])} {k[2]}..{k[3]} {k[4]} @{','.join(k[5])}" for k in ksig - msig]
        extra = [f"{mcomps[i]['clause']}:{mcomps[i]['category']}" for i in range(len(mcomps)) if i not in used]
        lines.append(f"  {kt['name'][:44]:44} found {t['found']}/{t['parts']}  extra in model {len(extra)} {extra[:4]}")
    head = (f"{b}: terms {len(key['terms']) - len(missing_terms)}/{len(key['terms'])} | parts found {tot['found']}/{tot['parts']} | "
            f"category {tot['category']}/{tot['found']} | sign {tot['sign']}/{tot['found']} | ref {tot['ref']}/{tot['ref_n']} | "
            f"cap {tot['cap']}/{tot['cap_n']} (term level {tot['tcap']}/{tot['tcap_n']}) | missing terms {missing_terms}")
    return head, lines, diffs, tot


if __name__ == "__main__":
    allt = {}
    for b in sys.argv[1:]:
        head, lines, diffs, tot = score(b)
        print(head)
        print("\n".join(lines))
        print("  DISAGREEMENTS:")
        print("\n".join("   - " + d for d in diffs))
        for k, v in tot.items():
            allt[k] = allt.get(k, 0) + v
    print(f"\nALL: parts found {allt['found']}/{allt['parts']} | category {allt['category']}/{allt['found']} | sign {allt['sign']}/{allt['found']} | "
          f"ref {allt['ref']}/{allt['ref_n']} | cap {allt['cap']}/{allt['cap_n']}")
