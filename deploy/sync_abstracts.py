"""Bring second-model (GPT) results from the image's abstracts into the volume's copies on boot.

The volume keeps reviewers' work (decisions live in <B>.signoff.json; corrections change the abstract), so abstracts
are never overwritten wholesale. Only the GPT check's output is carried over: `second_model_review` and the review items
it produced (step "gpt"). Deals that exist only in the image are copied whole.
  python deploy/sync_abstracts.py /app/data/abstract/v1 /data/home/data/abstract/v1
"""
import json
import shutil
import sys
from pathlib import Path


def main(src, dst):
    src, dst = Path(src), Path(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for f in sorted(src.glob("*.incurrence.json")):          # derived from the agreement by code: always the image's
        shutil.copy2(f, dst / f.name)
    for f in sorted(src.glob("*.abstract.json")):
        d = dst / f.name
        if not d.exists():
            shutil.copy2(f, d)
            print(f"sync: {f.name} new, copied")
            continue
        img, vol = json.loads(f.read_text(encoding="utf-8")), json.loads(d.read_text(encoding="utf-8"))
        smr = img.get("second_model_review")
        if not smr or vol.get("second_model_review") == smr:
            continue
        vol["second_model_review"] = smr
        vol["review"] = [r for r in vol.get("review", []) if r.get("step") != "gpt"] + \
                        [r for r in img.get("review", []) if r.get("step") == "gpt"]
        d.write_text(json.dumps(vol, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"sync: {f.name} GPT results updated ({smr.get('tally', {}).get('agree')}/{smr.get('tally', {}).get('claims')} agree)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
