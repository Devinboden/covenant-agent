"""Check an intake bundle against a hand-built answer key: are the key's quotes from the
current agreement present in (a) the full extracted text and (b) the reading packet?
Usage: python -m covenant_engine.ingest.coverage
"""
import json
import re
from pathlib import Path

import yaml

from ..verify_abstract import RAW_KEYS, normalize, walk_quotes
from .run import CURRENT, ROOT


def squash(s):
    return re.sub(r"[\s|​]+", "", normalize(s))


def main():
    for b, pdf in CURRENT.items():
        stem = Path(pdf).stem
        bundle = ROOT / "data" / "intake" / b / stem
        full = squash((bundle / "text_live.txt").read_text(encoding="utf-8"))
        packet = squash(" ".join(c["text"] for c in json.loads((bundle / "packet.json").read_text(encoding="utf-8"))))
        ab = yaml.safe_load((ROOT / "data" / "abstracts" / f"{b}.yaml").read_text(encoding="utf-8"))
        src_ids = {k for k, v in ab["sources"].items() if Path(v["file"]).stem == stem}
        n = in_full = in_packet = 0
        outside = []
        by_part = {}
        for trail, src, q in walk_quotes(ab, "ca", []):
            if src not in src_ids or any(k in trail[-1] for k in RAW_KEYS):
                continue
            n += 1
            part = trail[0]
            by_part.setdefault(part, [0, 0])
            by_part[part][0] += 1
            qs = squash(q)
            by_part[part][1] += qs in packet
            in_full += qs in full
            if qs in packet:
                in_packet += 1
            elif qs in full:
                outside.append(".".join(trail)[:70])
        print(f"{b:5} answer-key quotes from current agreement: {n} | in extracted text {in_full} | in packet {in_packet} ({in_packet / max(n, 1):.0%})")
        print("      by section: " + "  ".join(f"{k} {v[1]}/{v[0]}" for k, v in by_part.items()))
        for o in outside[:int(__import__("sys").argv[1]) if len(__import__("sys").argv) > 1 else 6]:
            print("      not in packet:", o)


if __name__ == "__main__":
    main()
