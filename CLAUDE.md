# Covenant Agent

Production-grade covenant abstraction agent: credit agreement PDF in, checked covenant abstract out, human sign-off.
Built as an AWS AgentCore agent with an MCP App interface; the engine in `src/covenant_engine` is its tool library.

## Rules
- Code decides anything deterministic and checks everything; the model only fills fixed structures. Never let the model do arithmetic.
- Nothing is marked verified or signed off except by a person through the sign-off record (four eyes).
- Every model call goes through `covenant_engine.model.call_model`. Bedrock is the last step: build and test locally on `claude -p` first.
- Public data only (SEC EDGAR). No client data, work-org metadata or work code. Never commit credentials or large data (see .gitignore).
- Check current vendor docs before relying on a platform API (AgentCore, MCP Apps, Entra); a doc citation informs, a probe decides.
- Run `pytest -q` before every commit. A change that lowers a score in tests/baseline.json is a regression; raise the baseline only when a fix improves it.
- Building or changing the MCP server or its widget: follow docs/mcp-apps-practices.md (content-hashed widget URI, app-only decision tools, display modes, empty states).
