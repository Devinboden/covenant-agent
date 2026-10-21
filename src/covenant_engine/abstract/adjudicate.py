"""Referee: a third, stronger model (Fable) rules on each key-vs-model disagreement from score_keys, seeing only the
definition text and the two readings (labelled A/B in random order, so it doesn't know which is the key).

Usage: python -m covenant_engine.abstract.adjudicate WEST TRS MEC [--model claude-fable-5-1]
"""
import argparse
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor

from ..ingest.exceptions import call_model
from ..ingest.run import ROOT
from .decompose import FAMILIES, Bundle
from .score_keys import score

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rulings"],
          "properties": {"rulings": {"type": "array", "items": {
              "type": "object", "additionalProperties": False,
              "required": ["id", "verdict", "material", "reason"],
              "properties": {"id": {"type": "string"},
                             "verdict": {"type": "string", "enum": ["A_correct", "B_correct", "both_acceptable", "both_wrong"]},
                             "material": {"type": "boolean"},
                             "reason": {"type": "string"}}}}}}
SYSTEM = ("You are a senior leveraged-finance lawyer reviewing how a credit agreement definition was decomposed into a "
          "fixed parts library for a covenant compliance engine. Judge only from the definition text given. Be decisive.")
PROMPT = """Definition of “{term}” (family: {family}; library categories: {cats}):
<<<
{definition}
>>>

Two reviewers decomposed this definition. For each disagreement below, rule: A_correct, B_correct, both_acceptable
(both readings would let a compliance engine compute the same correct result, or the text genuinely supports both),
or both_wrong. material = would the wrong reading change the computed covenant ratio or the amount counted?

{items}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("deals", nargs="+")
    ap.add_argument("--model", default="claude-fable-5-1")
    a = ap.parse_args()
    jobs = []
    for b in a.deals:
        key = {t["name"]: t for t in json.loads((ROOT / "data" / "keys_v1" / f"{b}.terms.json").read_text(encoding="utf-8"))["terms"]}
        ab = {t["name"]: t for t in json.loads((ROOT / "data" / "abstract" / "v1" / f"{b}.abstract.json").read_text(encoding="utf-8"))["terms"]}
        _, _, diffs, _ = score(b)
        defs = Bundle(b).defs["defined"]
        by_term = {}
        for d in diffs:
            name = next((n for n in sorted(key, key=len, reverse=True) if d.startswith(n + " ")), None)
            if name:
                by_term.setdefault(name, []).append(d)
        for name, ds in by_term.items():
            items, legend = [], {}
            for i, d in enumerate(ds):
                m = re.match(r".*?: (category|sign|ref|cap) key (.*) model (.*)$", d)
                clause = d[len(name) + 1:].split(":")[0].split(" [")[0]
                if m:
                    kv, mv = m.group(2), m.group(3)
                    what = m.group(1)
                else:                                   # NOT FOUND in model
                    what, kv, mv = "component present?", d.split("[")[1].split("]")[0] + " included as a part", "no such part"
                ka = random.random() < 0.5
                A, B = (kv, mv) if ka else (mv, kv)
                legend[f"d{i}"] = {"diff": d, "A_is": "key" if ka else "model"}
                items.append(f"- id d{i}: clause {clause}, {what}: A = {A} | B = {B}")
            jobs.append((b, name, legend, PROMPT.format(term=name, family=key[name]["family"],
                                                        cats=", ".join(FAMILIES[key[name]["family"]]) + ", term_reference, calculation_rule, custom",
                                                        definition=defs[name]["text"][:20000], items="\n".join(items))))

    def run(job):
        b, name, legend, prompt = job
        r = call_model(SYSTEM, prompt, SCHEMA, model=a.model, timeout=900)
        out = []
        for x in (r.get("output") or {}).get("rulings", []):
            lg = legend.get(x["id"])
            if not lg:
                continue
            v = x["verdict"]
            if v in ("A_correct", "B_correct"):
                v = ("key" if (v == "A_correct") == (lg["A_is"] == "key") else "model") + "_correct"
            out.append({"deal": b, "term": name, "diff": lg["diff"], "verdict": v, "material": x["material"], "reason": x["reason"]})
        return out, r.get("cost_usd") or 0

    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(run, jobs))
    rulings = [x for o, _ in res for x in o]
    cost = sum(c for _, c in res)
    (ROOT / "data" / "keys_v1" / "adjudication.json").write_text(json.dumps(rulings, indent=1), encoding="utf-8")
    from collections import Counter
    print(f"{len(rulings)} rulings, ${cost:.2f}: {dict(Counter(r['verdict'] for r in rulings))}")
    for r in rulings:
        print(f"[{r['verdict']:15}{' MATERIAL' if r['material'] else '         '}] {r['deal']} {r['diff'][:110]}\n      {r['reason'][:240]}")


if __name__ == "__main__":
    main()
