"""Generated workbooks, kept so a link can hand them to the person (code only).

make(b) builds the covenant testing workbook, stores it under <COVENANT_HOME>/exports/<token>/<file name> and returns
the bytes and, when COVENANT_PUBLIC_URL is set, a download link (/files/<token>/<file name> on the hosted server).
The token is a keyed hash of what the workbook is made from: the same workbook gets the same link (so repeated clicks on
the public demo don't fill the disk) and a link can't be guessed from the deal name.
"""
import hashlib
import io
import os
import re
import secrets
from pathlib import Path

from . import config

MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TOKEN = re.compile(r"^[a-f0-9]{32}$")
NAME = re.compile(r"^[A-Za-z0-9_.-]{1,80}\.xlsx$")


def _dir():
    d = Path(config.ROOT) / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _salt():
    s = os.environ.get("COVENANT_FILE_SALT")
    if s:
        return s.encode()
    f = _dir() / ".salt"
    if not f.exists():
        f.write_text(secrets.token_hex(32), encoding="utf-8")
        os.chmod(f, 0o600)
    return f.read_text(encoding="utf-8").strip().encode()


def public_url(token, name):
    base = os.environ.get("COVENANT_PUBLIC_URL", "").rstrip("/")
    return f"{base}/files/{token}/{name}" if base else None


def make(b, abs_dir=config.ABSTRACT_DIR, today=None):
    """Build (or reuse) the workbook. The link is keyed on what the workbook is made from: the abstract's content, the
    sign-off record, the date and the generator's source; the same inputs give the same file and link."""
    import json
    from datetime import date
    from . import export_xlsx, compliance_test, xlsx_layout
    from .abstract import signoff as SO
    ab, rec, h = SO.load(b, abs_dir)
    today = today or date.today()
    code = b"".join(Path(m.__file__).read_bytes() for m in (export_xlsx, compliance_test, xlsx_layout))
    key = b"|".join([_salt(), b.encode(), str(h).encode(), json.dumps(rec, sort_keys=True, default=str).encode(),
                     today.isoformat().encode(), hashlib.sha256(code).digest()])
    token = hashlib.sha256(key).hexdigest()[:32]
    name = f"{b}_covenant_testing.xlsx"
    p = _dir() / token / name
    if p.exists():
        data = p.read_bytes()
    else:
        buf = io.BytesIO()
        export_xlsx.build(b, abs_dir, today).save(buf)
        data = buf.getvalue()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return {"borrower": b, "file_name": name, "token": token, "bytes": data, "mime": MIME, "url": public_url(token, name)}


def find(token, name):
    """The stored file for a link, or None (bad token / name, or not made here)."""
    if not TOKEN.match(token or "") or not NAME.match(name or ""):
        return None
    p = _dir() / token / name
    return p if p.is_file() else None
