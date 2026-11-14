"""Settings. Everything that differs between a laptop and AgentCore lives here, read from the environment.

COVENANT_HOME   project root holding data/ (default: the repository root)
COVENANT_MODEL  model backend: "claude-cli" (default, local `claude -p`) | "replay" (recorded responses, for tests)
COVENANT_RECORD directory to record every model response to (for replay tests); off by default
COVENANT_REPLAY directory of recorded responses used by the "replay" backend
CODEX_BIN       Codex CLI for the optional GPT second check; CODEX_HOME holds its sign-in
"""
import os
import shutil
from pathlib import Path

ROOT = Path(os.environ.get("COVENANT_HOME") or Path(__file__).resolve().parents[2])
DATA = ROOT / "data"
ABSTRACT_DIR = "data/abstract/v1"            # relative to ROOT (kept relative: CLIs take it as an argument)

MODEL_BACKEND = os.environ.get("COVENANT_MODEL", "claude-cli")
# "off" blocks every model call (the public view-only demo): call_model refuses before any backend runs.
MODEL_CALLS = os.environ.get("COVENANT_MODEL_CALLS", "on").lower()
RECORD_DIR = os.environ.get("COVENANT_RECORD") or None
REPLAY_DIR = os.environ.get("COVENANT_REPLAY") or None

CODEX_BIN = os.environ.get("CODEX_BIN") or shutil.which("codex")
