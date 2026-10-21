"""The one gate every model call goes through. Swapping providers (e.g. Bedrock at deployment) means adding a backend
here; nothing else changes.

A backend takes (system, user, json_schema, model, timeout) and returns
  {"ok", "output" (the structured object), "model", "models_used", "cost_usd", "seconds", "error"}.

Backends: ClaudeCLI (local `claude -p`, structured output via --json-schema) and Replay (recorded responses, keyed by a
hash of the request, so tests and the agent loop can run without model calls). Set COVENANT_RECORD to record.
"""
import hashlib
import json
import subprocess
import tempfile
import time
from pathlib import Path

from . import config

DEFAULT_MODEL = "claude-haiku-5-5"


def request_key(system, user, schema, model):
    return hashlib.sha256(json.dumps([system, user, schema, model], sort_keys=True).encode()).hexdigest()[:24]


class ClaudeCLI:
    name = "claude-cli"

    def complete_json(self, system, user, schema, model=DEFAULT_MODEL, timeout=180):
        t0 = time.time()
        cmd = ["claude", "-p", "--model", model, "--tools", "", "--no-session-persistence",
               "--system-prompt", system, "--output-format", "json", "--json-schema", json.dumps(schema)]
        try:
            p = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=timeout, cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"timed out after {timeout}s", "seconds": round(time.time() - t0, 1)}
        try:
            d = json.loads(p.stdout)
        except json.JSONDecodeError:
            return {"ok": False, "error": (p.stderr or p.stdout)[:400], "seconds": round(time.time() - t0, 1)}
        usage = d.get("modelUsage", {}) or {}
        # the CLI can use a small helper model too; report the model that did the work (largest spend) plus the breakdown
        model_name = max(usage, key=lambda m: usage[m].get("costUSD", 0)) if usage else "?"
        return {"ok": not d.get("is_error") and d.get("structured_output") is not None,
                "output": d.get("structured_output"), "model": model_name,
                "models_used": {m: round(u.get("costUSD", 0), 5) for m, u in usage.items()},
                "cost_usd": round(d.get("total_cost_usd", 0), 5), "seconds": round(time.time() - t0, 1),
                "error": None if not d.get("is_error") else str(d.get("result"))[:400]}


class Replay:
    """Serves recorded responses; a request that was never recorded fails loudly (never silently empty)."""
    name = "replay"

    def __init__(self, directory):
        self.dir = Path(directory)

    def complete_json(self, system, user, schema, model=DEFAULT_MODEL, timeout=180):
        f = self.dir / f"{request_key(system, user, schema, model)}.json"
        if not f.exists():
            return {"ok": False, "error": f"no recorded response for this request ({f.name})", "seconds": 0}
        return json.loads(f.read_text(encoding="utf-8"))


class Recording:
    def __init__(self, inner, directory):
        self.inner, self.dir, self.name = inner, Path(directory), inner.name

    def complete_json(self, system, user, schema, model=DEFAULT_MODEL, timeout=180):
        r = self.inner.complete_json(system, user, schema, model=model, timeout=timeout)
        if r.get("ok"):
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{request_key(system, user, schema, model)}.json").write_text(json.dumps(r), encoding="utf-8")
        return r


_backend = None


def backend():
    global _backend
    if _backend is None:
        b = Replay(config.REPLAY_DIR) if config.MODEL_BACKEND == "replay" else ClaudeCLI()
        _backend = Recording(b, config.RECORD_DIR) if config.RECORD_DIR else b
    return _backend


def set_backend(b):
    """Tests and the agent can inject a backend."""
    global _backend
    _backend = b


def call_model(system, user, schema, model=DEFAULT_MODEL, timeout=180):
    return backend().complete_json(system, user, schema, model=model, timeout=timeout)
