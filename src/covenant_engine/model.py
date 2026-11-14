"""The one gate every model call goes through. Swapping providers (e.g. Bedrock at deployment) means adding a backend
here; nothing else changes.

A backend takes (system, user, json_schema, model, timeout) and returns
  {"ok", "output" (the structured object), "model", "models_used", "cost_usd", "seconds", "error"}.

Backends: ClaudeCLI (local `claude -p`, structured output via --json-schema), CodexCLI (the GPT second check:
`codex exec --output-schema`, model "codex"), Live (routes by model name to one of those two) and Replay (recorded responses, keyed by a
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


class CodexCLI:
    """The blind second check on GPT. Codex runs as an agent in an empty scratch folder with the prompt on stdin and a
    strict output schema; only its final structured answer comes back. Needs the Codex CLI signed in (CODEX_HOME)."""
    name = "codex-cli"

    def complete_json(self, system, user, schema, model="codex", timeout=2400):
        import os
        t0 = time.time()
        if not config.CODEX_BIN or not os.environ.get("CODEX_HOME"):
            return {"ok": False, "error": "Codex not signed in (set CODEX_HOME)", "seconds": 0}
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "schema.json").write_text(json.dumps(schema), encoding="utf-8")
            prompt = (system + "\n\n" if system else "") + user
            try:
                p = subprocess.run([config.CODEX_BIN, "exec", "--skip-git-repo-check", "--ephemeral", "--output-schema",
                                    "schema.json", "-o", "answers.json", "-"], cwd=d, input=prompt, capture_output=True,
                                   text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                return {"ok": False, "error": f"timed out after {timeout}s", "seconds": round(time.time() - t0, 1)}
            out = Path(d) / "answers.json"
            if p.returncode or not out.exists():
                return {"ok": False, "error": f"codex exit {p.returncode}: {(p.stderr or p.stdout)[-400:]}",
                        "seconds": round(time.time() - t0, 1)}
            try:
                output = json.loads(out.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                return {"ok": False, "error": f"codex answer is not JSON: {e}", "seconds": round(time.time() - t0, 1)}
        return {"ok": True, "output": output, "model": "codex", "models_used": {"codex": 0}, "cost_usd": 0,
                "seconds": round(time.time() - t0, 1), "error": None}


class Live:
    """Routes by model name: 'codex' / 'gpt-*' to the Codex CLI, everything else to the Claude CLI."""
    name = "live"

    def __init__(self):
        self.claude, self.codex = ClaudeCLI(), CodexCLI()

    def complete_json(self, system, user, schema, model=DEFAULT_MODEL, timeout=180):
        b = self.codex if model == "codex" or model.startswith("gpt") else self.claude
        return b.complete_json(system, user, schema, model=model, timeout=timeout)


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
        b = Replay(config.REPLAY_DIR) if config.MODEL_BACKEND == "replay" else Live()
        _backend = Recording(b, config.RECORD_DIR) if config.RECORD_DIR else b
    return _backend


def set_backend(b):
    """Tests and the agent can inject a backend."""
    global _backend
    _backend = b


def call_model(system, user, schema, model=DEFAULT_MODEL, timeout=180):
    if config.MODEL_CALLS == "off":            # the one gate every model call passes: off = no call, no cost
        return {"ok": False, "error": "model calls are off on this server (view-only demo)", "seconds": 0, "cost_usd": 0}
    return backend().complete_json(system, user, schema, model=model, timeout=timeout)
