"""Sign-off record (#31). An abstract moves draft -> verified -> signed_off; the record lives next to it
(<B>.signoff.json) so rebuilding the abstract never overwrites a person's decisions.

- Every flag and review item gets a stable key (hash of what it is about, not its position), so a rebuild maps old
  resolutions onto the same items; new items arrive unresolved.
- resolve: decision accept | correct | reject | defer, by, date, note (and the corrected value for 'correct').
- verify: allowed only when every item is resolved and none is deferred. Records the abstract's content hash.
- sign: allowed only for a verified abstract whose hash is unchanged, by a different person than the verifier.
- Any change to the abstract's content after verify / sign drops the status back to draft (transition logged).

  python -m covenant_engine.abstract.signoff status WEST
  python -m covenant_engine.abstract.signoff resolve WEST <key> accept --by "Reviewer 1" --note "..."
  python -m covenant_engine.abstract.signoff verify WEST --by "Reviewer 1"
  python -m covenant_engine.abstract.signoff sign WEST --by "Reviewer 2"
"""
import argparse
import hashlib
import json
from datetime import date

from ..config import ABSTRACT_DIR
from ..store import store


class SignoffError(ValueError):
    """A sign-off rule refused the action (message says why)."""


DECISIONS = ["accept", "correct", "reject", "defer"]
VOLATILE = ("built", "status", "models")       # header fields that change on every rebuild without changing content


def content_hash(ab):
    """Hash of what the abstract says (header build metadata and the sign-off status excluded)."""
    x = {k: v for k, v in ab.items() if k not in ("header",)}
    x["header"] = {k: v for k, v in ab["header"].items() if k not in VOLATILE}
    return hashlib.sha256(json.dumps(x, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def item_key(kind, x):
    if kind == "flag":
        basis = ["flag", x.get("type"), x.get("ref"), (x.get("quote") or "")[:120]]
    else:
        basis = ["review", str(x.get("step")), x.get("term") or "", x.get("key_clause") or x.get("clause") or x.get("id") or "",
                 (x.get("why") or x.get("item") or x.get("claim") or "")[:160]]
    return hashlib.sha1("|".join(map(str, basis)).encode()).hexdigest()[:10]


def review_list(ab):
    """The abstract's review entries, plus code checks run on the stored abstract: each level or cap whose value isn't
    in its own quote, and one per defined term the covenants rely on that wasn't broken into its parts
    (no category for it, or the depth / term limit cut it off). Skipped terms are never silent."""
    from .value_check import checks
    from .clause_labels import match
    # An entry's clause written another way than the stored part's ("(g)(exclusion)" vs "(g) (exclusion)") is matched
    # to it, so every lookup downstream finds the part; the original label keeps the item's key (and any decision on it).
    rows = [r if not (r.get("term") and r.get("clause")) or match(ab, r["term"], r["clause"]) == r["clause"]
            else {**r, "clause": match(ab, r["term"], r["clause"]), "key_clause": r["clause"]} for r in ab.get("review", [])]
    return rows + checks(ab) + [{"step": "terms", "term": t, "item": f"{t} is used in the covenant calculation but "
                                    f"its parts weren't recorded"} for t in ab.get("terms_pending", [])]


def items(ab):
    out = {}
    for f in ab.get("flags", []):
        out[item_key("flag", f)] = {"kind": "flag", "text": f"{f['type']} {f['ref']}: {f['note'][:200]}"}
    for r in review_list(ab):
        what = r.get("why") or r.get("item") or r.get("claim") or ""
        out[item_key("review", r)] = {"kind": f"review/{r.get('step')}",
                                      "text": f"{r.get('term', '')} {r.get('clause', '') or r.get('id', '')}: {what[:200]}".strip()}
    return out


def load(b, abs_dir=ABSTRACT_DIR):
    st = store(abs_dir)
    ab = st.load(b, "abstract")
    rec = st.load(b, "signoff", default={
        "borrower": b, "status": "draft", "hash": None, "verified_by": None, "signed_by": None,
        "resolutions": {}, "transitions": []})
    h = content_hash(ab)
    if rec["status"] != "draft" and rec["hash"] != h:          # content changed since verify/sign
        rec["transitions"].append({"from": rec["status"], "to": "draft", "by": "system", "date": date.today().isoformat(),
                                   "why": "abstract content changed after verification"})
        rec.update(status="draft", verified_by=None, signed_by=None)
    return ab, rec, h


def summary(ab, rec):
    its = items(ab)
    res = rec["resolutions"]
    open_ = [k for k in its if k not in res]
    deferred = [k for k in its if res.get(k, {}).get("decision") == "defer"]
    stale = [k for k in res if k not in its]                     # resolutions for items that no longer exist
    return its, open_, deferred, stale


def save(b, ab, rec, abs_dir=ABSTRACT_DIR):
    st = store(abs_dir)
    ab["header"]["status"] = rec["status"]
    st.save(b, "signoff", rec)
    st.save(b, "abstract", ab)


def resolve(b, key, decision, by, note="", correction="", abs_dir=ABSTRACT_DIR, changes=None):
    """Record a decision. A 'correct' with `changes` ([{path, value}], from corrections.fields) is applied to the
    abstract itself (re-validated, logged before -> after); a 'correct' without changes is only noted."""
    ab, rec, h = load(b, abs_dir)
    its = items(ab)
    if key not in its:
        raise SignoffError(f"no open item {key}")
    if decision not in DECISIONS:
        raise SignoffError(f"decision must be one of {DECISIONS}")
    log = []
    if decision == "correct" and changes:
        from . import corrections as C
        from .build import ABSTRACT_SCHEMA
        from .new_deal import review_rows
        row = next((r for r in review_rows(ab, rec) if r["key"] == key), None)
        try:
            ab, log = C.apply(ab, row or {}, changes, ABSTRACT_SCHEMA)
        except C.CorrectionError as e:
            raise SignoffError(str(e))
        log = C.stamp(log, by)
        correction = C.summary(log)
        rec.setdefault("corrections", []).extend(x | {"key": key} for x in log)
    if decision == "correct" and not correction:
        raise SignoffError("a 'correct' decision needs the corrected value (--correction)")
    rec["resolutions"][key] = {"item": its[key]["text"], "kind": its[key]["kind"], "decision": decision, "by": by,
                               "date": date.today().isoformat(), "note": note, "correction": correction,
                               "applied": bool(log), "changes": [{k: x[k] for k in ("group", "label", "before", "after")} for x in log]}
    save(b, ab, rec, abs_dir)


def verify(b, by, abs_dir=ABSTRACT_DIR):
    ab, rec, h = load(b, abs_dir)
    its, open_, deferred, _ = summary(ab, rec)
    if open_ or deferred:
        raise SignoffError(f"cannot verify: {len(open_)} unresolved, {len(deferred)} deferred")
    if any(r.get("step") == "schema" for r in ab["review"]):
        raise SignoffError("cannot verify: schema errors")
    rec["transitions"].append({"from": rec["status"], "to": "verified", "by": by, "date": date.today().isoformat()})
    rec.update(status="verified", hash=h, verified_by=by)
    save(b, ab, rec, abs_dir)


def sign(b, by, abs_dir=ABSTRACT_DIR):
    ab, rec, h = load(b, abs_dir)
    if rec["status"] != "verified":
        raise SignoffError(f"cannot sign: status is {rec['status']}")
    if by == rec["verified_by"]:
        raise SignoffError("cannot sign: sign-off must be by a different person than the verifier")
    rec["transitions"].append({"from": "verified", "to": "signed_off", "by": by, "date": date.today().isoformat()})
    rec.update(status="signed_off", signed_by=by)
    save(b, ab, rec, abs_dir)


def _do(a):
    if a.cmd == "resolve":
        resolve(a.borrower, a.key, a.decision, a.by, a.note, a.correction, a.abs_dir)
    elif a.cmd == "verify":
        verify(a.borrower, a.by, a.abs_dir)
    elif a.cmd == "sign":
        sign(a.borrower, a.by, a.abs_dir)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["status", "resolve", "verify", "sign"])
    ap.add_argument("borrower")
    ap.add_argument("key", nargs="?")
    ap.add_argument("decision", nargs="?")
    ap.add_argument("--by")
    ap.add_argument("--note", default="")
    ap.add_argument("--correction", default="")
    ap.add_argument("--abs-dir", default=ABSTRACT_DIR)
    a = ap.parse_args()
    try:
        _do(a)
    except SignoffError as e:
        raise SystemExit(str(e))
    ab, rec, h = load(a.borrower, a.abs_dir)
    if store(a.abs_dir).exists(a.borrower, "signoff"):
        save(a.borrower, ab, rec, a.abs_dir)              # persist a drop to draft found on load
    its, open_, deferred, stale = summary(ab, rec)
    print(f"{a.borrower}: {rec['status']} | items {len(its)} | resolved {len(its) - len(open_)} | open {len(open_)} | "
          f"deferred {len(deferred)} | resolutions for items no longer present {len(stale)}")
    if a.cmd == "status":
        for k in open_:
            print(f"   {k} [{its[k]['kind']}] {its[k]['text'][:140]}")


def sync(b, abs_dir=ABSTRACT_DIR):
    """Called after a build: set header.status from the record (dropping to draft if content changed)."""
    if not store(abs_dir).exists(b, "signoff"):
        return "draft"
    ab, rec, h = load(b, abs_dir)
    save(b, ab, rec, abs_dir)
    return rec["status"]


if __name__ == "__main__":
    main()
