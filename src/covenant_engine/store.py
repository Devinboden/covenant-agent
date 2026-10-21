"""Where abstracts, sign-off records and review files live. One interface; LocalStore writes JSON files under
data/abstract/v1 today. A cloud store (S3 / DynamoDB) implements the same methods at deployment.
"""
import json
from pathlib import Path

from . import config


class LocalStore:
    def __init__(self, abs_dir=config.ABSTRACT_DIR):
        self.dir = config.ROOT / abs_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _p(self, deal, kind):
        return self.dir / f"{deal}.{kind}.json"

    def exists(self, deal, kind):
        return self._p(deal, kind).exists()

    def load(self, deal, kind, default=None):
        p = self._p(deal, kind)
        if not p.exists():
            if default is not None:
                return default
            raise FileNotFoundError(f"no {kind} for {deal}")
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, deal, kind, obj):
        p = self._p(deal, kind)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)                      # atomic: a reader never sees half a file

    def deals(self):
        return sorted(p.name.split(".")[0] for p in self.dir.glob("*.abstract.json"))


def store(abs_dir=config.ABSTRACT_DIR):
    return LocalStore(abs_dir)
