"""Validate a v1 answer key for terms (data/keys_v1/<B>.terms.json) with the SAME code checks the pipeline applies to
model output, plus schema shape. Used by whoever builds a key (person or agent) until it comes back clean.

Key file shape: {"borrower": "WEST", "terms": [{"name", "family", "starts_from", "components": [COMPONENT...], "notes"}]}
COMPONENT = covenant_engine.abstract.decompose.COMPONENT (category must come from the family's library, or term_reference /
calculation_rule / custom).

Usage: python -m covenant_engine.abstract.validate_key WEST
"""
import json
import sys

import jsonschema

from ..ingest.run import ROOT
from .clauses import coverage, enumerate_clauses
from .decompose import COMMON, COMPONENT, FAMILIES, Bundle, check

KEY_DIR = ROOT / "data" / "keys_v1"


def validate(borrower):
    key = json.loads((KEY_DIR / f"{borrower}.terms.json").read_text(encoding="utf-8"))
    b = Bundle(borrower)
    out = []
    for t in key["terms"]:
        name, fam = t["name"], t.get("family")
        if name not in b.defs["defined"]:
            out.append(f"{name}: not a defined term in the agreement (use the exact name)")
            continue
        if fam not in FAMILIES:
            out.append(f"{name}: family {fam!r} must be one of {sorted(FAMILIES)}")
            continue
        allowed = set(FAMILIES[fam]) | set(COMMON)
        for c in t["components"]:
            for e in jsonschema.Draft202012Validator(COMPONENT).iter_errors(c):
                out.append(f"{name} {c.get('clause')}: schema: {e.message[:140]}")
            if c.get("category") not in allowed:
                out.append(f"{name} {c.get('clause')}: category {c.get('category')!r} not in the {fam} library")
        definition = b.defs["defined"][name]["text"]
        leaves = enumerate_clauses(definition)
        try:
            probs, missing, unknown, review = check(t["components"], t.get("starts_from", ""), leaves, b, fam, definition, name)
        except (KeyError, TypeError) as e:
            out.append(f"{name}: could not run checks ({e!r}); fix the schema problems first")
            continue
        for k, v in probs.items():
            out += [f"{name} {k}: {x}" for x in v]
        out += [f"{name} {k}: clause found by code but not in the key" for k in missing]
    return out


if __name__ == "__main__":
    probs = validate(sys.argv[1])
    print("\n".join(probs) if probs else "CLEAN")
