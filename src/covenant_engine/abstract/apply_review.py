"""Apply decisions made on the Covenant Abstract Review page to the sign-off record (signoff.py).

The page writes decisions/<B>__<key> {decision, note, correction, by, at} and actions/<B> {sign_requested_by}. Claude exports them (ArtifactData list with out_dir) and runs:

  python -m covenant_engine.abstract.apply_review <export dir>     (the dir holding decisions/ and actions/)

Every decision goes through signoff.resolve; sign-off goes through signoff.sign, so the same rules hold
(all items resolved, none deferred, no schema errors). Then the review files are refreshed.
"""
import json
import sys
from pathlib import Path

from . import signoff as S
from .signoff import SignoffError
from .new_deal import review_file


def main(export):
    ex = Path(export)
    done, skipped = [], []
    for f in sorted((ex / "decisions").glob("*.json")):
        d = json.loads(f.read_text())
        d = d.get("data", d)
        try:
            S.resolve(d["deal"], d["key"], d["decision"], d.get("by") or "reviewer", d.get("note", ""), d.get("correction", ""))
            done.append(f"{d['deal']} {d['key']} {d['decision']}")
        except SignoffError as e:
            skipped.append(f"{d.get('deal')} {d.get('key')}: {e}")
    for f in sorted((ex / "actions").glob("*.json")) if (ex / "actions").exists() else []:
        a = json.loads(f.read_text())
        a = a.get("data", a)
        b = a["deal"]
        for kind, fn in (("sign", S.sign),):
            who = a.get(f"{kind}_requested_by")
            if not who:
                continue
            try:
                fn(b, who)
                done.append(f"{b} {kind} by {who}")
            except SignoffError as e:
                skipped.append(f"{b} {kind}: {e}")
    deals = {l.split()[0] for l in done} | {l.split()[0] for l in skipped}
    for b in sorted(deals):
        review_file(b)
    print("applied:", *done, sep="\n  ")
    print("not applied:", *skipped, sep="\n  ")


if __name__ == "__main__":
    main(sys.argv[1])
