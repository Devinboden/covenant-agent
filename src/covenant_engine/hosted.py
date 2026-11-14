"""Hosted demo server (Fly): one MCP endpoint per reviewer, so claude.ai / ChatGPT custom connectors can use it.

  COVENANT_HOME=/data/home  uvicorn covenant_engine.hosted:app --host 0.0.0.0 --port 8080
  (reviewer secrets: COVENANT_REVIEWERS='{"<secret>": "Reviewer 1", ...}' or <COVENANT_HOME>/reviewers.json, made on first boot)

Each reviewer connects to https://<host>/r/<secret>/mcp. Hosts' custom connectors can't send a fixed API key, so the
secret in the URL is the access control, and which URL you connected with is who you are (the sign-off
record names that reviewer). Real sign-in (Entra / Cognito) replaces this at the AgentCore step.
Unknown paths get 404 and secrets never appear in logs (the access log prints /r/<redacted>/...).
"""
import contextlib
import json
import logging
import os
import re

from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from mcp.server.transport_security import TransportSecuritySettings

from .mcp_server import create_server

SECRET = re.compile(r"^[A-Za-z0-9_-]{32,}$")


def reviewers():
    """Reviewer URLs: COVENANT_REVIEWERS if set, else <COVENANT_HOME>/reviewers.json, created on first boot with two
    random secrets (read them with: fly ssh console -C "cat /data/home/reviewers.json"). Never logged."""
    raw = os.environ.get("COVENANT_REVIEWERS", "")
    if not raw:
        from . import config
        f = config.ROOT / "reviewers.json"
        if not f.exists():
            import secrets
            f.write_text(json.dumps({secrets.token_urlsafe(32): "Reviewer 1", secrets.token_urlsafe(32): "Reviewer 2"},
                                    indent=1), encoding="utf-8")
            os.chmod(f, 0o600)
        raw = f.read_text(encoding="utf-8")
    try:
        m = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise SystemExit("COVENANT_REVIEWERS must be JSON: {\"<secret>\": \"Reviewer name\", ...}")
    bad = [k for k in m if not SECRET.match(k)]
    if bad or not m:
        raise SystemExit("COVENANT_REVIEWERS needs at least one entry, each secret 32+ chars of [A-Za-z0-9_-]")
    if len(set(m.values())) != len(m):
        raise SystemExit("each reviewer secret must map to a different name (the sign-off record names who acted)")
    return m


class _Redact(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(re.sub(r"/r/[^/ ]+", "/r/<redacted>", a) if isinstance(a, str) else a for a in record.args)
        return True


def build(reviewer_map=None, allowed_hosts=None):
    reviewer_map = reviewer_map or reviewers()
    hosts = allowed_hosts or [h.strip() for h in os.environ.get("COVENANT_ALLOWED_HOSTS", "").split(",") if h.strip()]
    # A browser Origin other than these is refused; server-side connectors (claude.ai, ChatGPT) usually send none.
    extra = os.environ.get("COVENANT_ALLOWED_ORIGINS", "https://claude.ai,https://chatgpt.com")
    origins = [f"https://{h}" for h in hosts] + [o.strip() for o in extra.split(",") if o.strip()]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=bool(hosts), allowed_hosts=hosts,
                                         allowed_origins=origins)
    subs = []
    if os.environ.get("COVENANT_PUBLIC_VIEW") == "1":       # the public demo: no secret, view-only tools
        subs.append(("__view__", create_server(user="Visitor", view_only=True).streamable_http_app(
            streamable_http_path="/mcp", transport_security=security, host="0.0.0.0")))
    for secret, name in reviewer_map.items():
        srv = create_server(user=name)
        subs.append((secret, srv.streamable_http_app(streamable_http_path="/mcp", transport_security=security,
                                                     host="0.0.0.0")))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for _, sub in subs:                       # each MCP sub-app needs its session manager running
                await stack.enter_async_context(sub.router.lifespan_context(sub))
            yield

    async def health(request):
        return JSONResponse({"ok": True, "reviewers": len(subs)})

    async def root(request):
        view = os.environ.get("COVENANT_PUBLIC_VIEW") == "1"
        return PlainTextResponse("covenant-agent demo. " + (f"View-only MCP app: add {request.base_url}view/mcp as a "
                                 "custom connector in ChatGPT or Claude. " if view else "")
                                 + "Reviewers connect to their own URL.")

    logging.getLogger("uvicorn.access").addFilter(_Redact())
    return Starlette(routes=[Route("/health", health), Route("/", root)] +
                     [Mount("/view" if secret == "__view__" else f"/r/{secret}", app=sub) for secret, sub in subs],
                     lifespan=lifespan)


def __getattr__(name):                                # `uvicorn covenant_engine.hosted:app` builds on import
    if name == "app":
        return build()
    raise AttributeError(name)
