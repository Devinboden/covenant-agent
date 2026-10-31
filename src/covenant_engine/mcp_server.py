"""Covenant Agent MCP server (step 2): the engine's tools over MCP, local first.

  COVENANT_USER="Reviewer 1" python -m covenant_engine.mcp_server            (stdio)
  COVENANT_USER="Reviewer 1" python -m covenant_engine.mcp_server --http     (streamable HTTP on :8000/mcp)

Rules carried from the engine:
- The model never decides anything a person must decide. resolve_item, verify and sign_off ask the PERSON through an
  MCP elicitation form (the answer goes from the user to the server, not through the model); the model can only
  propose. Declined or cancelled forms change nothing.
- Who is acting comes from the connection, never from a tool argument: the authenticated principal when the
  transport has auth (AgentCore Identity / OAuth at deployment), else the server's configured user (COVENANT_USER).
  Four eyes (verifier != signer) is enforced by signoff.py on that identity.
- Sign-off rules stay in signoff.py; this file only routes.
"""
import argparse
import json
import os
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, create_model

from mcp.server.mcpserver import (AcceptedElicitation, Context, Elicit, ElicitationResult, MCPServer, Resolve,
                                  authenticated_principal)
from mcp.server.apps import Apps
from mcp.server.mcpserver.resources import TextResource
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import config
from .abstract import signoff as SO
from .abstract.new_deal import review_data
from .store import store
from . import review_view as RV

UI_URI = "ui://covenant-agent/review.html"
UI_DIR = Path(__file__).parent / "ui"
UI_DISPLAY_MODES_OPENAI = ["fullscreen"]   # ChatGPT is the primary surface: open the review screen full screen

TICKER = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
SECTIONS = ["summary", "covenants", "regimes", "flags", "loan", "terms", "pricing_grid", "calculation_rules",
            "deemed_values", "events", "reported"]
READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


def _ticker(b):
    b = (b or "").strip().upper()
    if not TICKER.match(b):
        raise ToolError(f"{b!r} is not a borrower ticker")
    return b


def _row(r):
    s, e = r["start"], r["end"]
    frm = s.get("date") or (f"{s.get('count')} quarters after {s.get('anchor')}" if s["kind"] == "quarters_after" else s["kind"])
    to = e.get("date") or (f"{e.get('count')} quarters after {e.get('anchor')}" if e["kind"] == "quarters_after" else e["kind"])
    return {"from": frm, "to": to, "value": r["value"]}


def summarize(ab, rec, open_n):
    """The abstract's headline: what a reviewer or the agent needs first. Code-built; no model."""
    loan = ab.get("loan") or {}
    return {
        "borrower": ab["header"].get("borrower"), "status": rec["status"], "spec_version": ab.get("spec_version"),
        "source_file": ab["header"].get("source_file"), "built": ab["header"].get("built"),
        "verified_by": rec.get("verified_by"), "signed_by": rec.get("signed_by"), "open_items": open_n,
        "loan": {k: loan.get(k) for k in ("closing_date", "maturity_date", "facilities", "fiscal_year_end") if k in loan},
        "covenants": [{"id": c["id"], "ref": c["ref"], "metric": c["metric"], "direction": c["direction"],
                       "frequency": c["frequency"], "applies_during": c.get("applies_during", ""),
                       "schedule": [_row(r) for r in c["schedule"]]} for c in ab["covenants"]],
        "regimes": [{"id": g["id"], "type": g["type"], "name": g["name"], "effect": g["effect"],
                     "applies_to": g["applies_to"]} for g in ab["regimes"]],
        "counts": {"terms": len(ab.get("terms", [])), "flags": len(ab.get("flags", [])),
                   "review_items": len(ab.get("review", []))},
    }


class Jobs:
    """Background runs of the front door (abstract_deal). One running job per borrower. State is kept as JSON under
    data/jobs so run_status survives the client reconnecting; a job found 'running' after a server restart is
    reported as interrupted."""

    def __init__(self, runner, job_dir):
        self.runner, self.dir = runner, Path(job_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.live = set()

    def _p(self, jid):
        if not re.fullmatch(r"[0-9a-f]{12}", jid or ""):
            raise ToolError(f"unknown job {jid!r}")
        return self.dir / f"{jid}.json"

    def _save(self, j):
        p = self._p(j["job_id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(j, indent=1), encoding="utf-8")
        tmp.replace(p)

    def get(self, jid):
        p = self._p(jid)
        if not p.exists():
            raise ToolError(f"unknown job {jid!r}")
        j = json.loads(p.read_text(encoding="utf-8"))
        if j["state"] == "running" and jid not in self.live:
            j["state"] = "interrupted"
        return j

    def all(self):
        return [self.get(p.stem) for p in sorted(self.dir.glob("*.json"))]

    def start(self, borrower, pdf, **kw):
        with self.lock:
            busy = [j for j in self.all() if j["borrower"] == borrower and j["state"] == "running"]
            if busy:
                raise ToolError(f"{borrower} already has a running job: {busy[0]['job_id']}")
            j = {"job_id": uuid.uuid4().hex[:12], "borrower": borrower, "pdf": pdf, "options": kw, "state": "running",
                 "started": time.strftime("%Y-%m-%dT%H:%M:%S"), "progress": [], "result": None}
            self.live.add(j["job_id"])
            self._save(j)
        threading.Thread(target=self._run, args=(j,), daemon=True).start()
        return j

    def _run(self, j):
        def progress(line):
            j["progress"].append(str(line)[:500])
            self._save(j)
        try:
            r = self.runner(j["borrower"], j["pdf"], progress=progress, **j["options"])
            j.update(state=r.get("status", "done"), result=r)
        except Exception as e:                       # the run's own failure, recorded for the person
            j.update(state="failed", result={"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-2000:]})
        j["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._save(j)
        self.live.discard(j["job_id"])


# ---- forms the person answers (MCP elicitation). Flat, primitive fields only (spec requirement). ----

class Confirm(BaseModel):
    confirm: bool = Field(description="Tick to confirm")


def create_server(abs_dir=config.ABSTRACT_DIR, user=None, runner=None, job_dir=None, edgar=None):
    """Build the server. user = the configured person behind an unauthenticated (local) connection.
    edgar = the EDGAR intake module (tests pass a stand-in; default covenant_engine.tools.edgar)."""
    if runner is None:
        from .abstract.new_deal import abstract_deal as runner
    if edgar is None:
        from .tools import edgar
    abstract_runner = runner

    def runner(b, pdf, progress, fetch_url=None, **kw):
        # An EDGAR intake job fetches and renders the exhibit first, inside the background job, then abstracts it.
        if fetch_url:
            edgar.fetch_pdf(b, fetch_url, progress=progress)
        return abstract_runner(b, pdf, progress=progress, **kw)

    st = store(abs_dir)
    jobs = Jobs(runner, job_dir or config.ROOT / "data" / "jobs")
    user = user if user is not None else os.environ.get("COVENANT_USER")
    apps = Apps()

    # ---------------- the review screen (MCP App view) ----------------
    # open_review is for the model and the view. The rest are app-only (_meta.ui.visibility = ["app"]): the model never
    # sees them, so a decision recorded through them is a person's click in the view. Identity still comes from the
    # connection, never from an argument.

    def _view(ctx, b):
        return RV.payload(b, who_or_none(ctx), abs_dir)

    @apps.tool(resource_uri=UI_URI, annotations=READ)
    def open_review(borrower: str, ctx: Context) -> dict[str, Any]:
        """Open the review screen for a deal: open items with their evidence, the covenant schedules, and the agreement
        page with each quote highlighted. The person resolves items, verifies and signs off in the screen."""
        b = need(borrower)
        p = _view(ctx, b)
        return {k: p[k] for k in ("borrower", "name", "status", "open", "total", "verified_by", "signed_by")} | \
            {"screen": p, "note": "The review screen shows this deal; decisions are made by the person in the screen."}

    @apps.tool(resource_uri=UI_URI, visibility=["app"], annotations=READ)
    def review_page(borrower: str, page: int) -> dict[str, Any]:
        """(screen only) One agreement page as an image."""
        b = need(borrower)
        try:
            return {"page": page, "image": RV.page_image(b, page, abs_dir)}
        except (FileNotFoundError, IndexError, ValueError) as e:
            raise ToolError(str(e))

    @apps.tool(resource_uri=UI_URI, visibility=["app"], annotations=WRITE)
    def review_decide(borrower: str, key: str, decision: Literal["accept", "correct", "reject", "defer"], ctx: Context,
                      note: str = "", correction: str = "") -> dict[str, Any]:
        """(screen only) Record the person's decision on one item."""
        b = need(borrower)
        try:
            SO.resolve(b, key, decision, who(ctx), note, correction, abs_dir)
        except SO.SignoffError as e:
            raise ToolError(str(e))
        return _view(ctx, b)

    @apps.tool(resource_uri=UI_URI, visibility=["app"], annotations=WRITE)
    def review_verify(borrower: str, ctx: Context) -> dict[str, Any]:
        """(screen only) Verify the abstract (first pair of eyes)."""
        b = need(borrower)
        try:
            SO.verify(b, who(ctx), abs_dir)
        except SO.SignoffError as e:
            raise ToolError(str(e))
        return _view(ctx, b)

    @apps.tool(resource_uri=UI_URI, visibility=["app"], annotations=WRITE)
    def review_sign(borrower: str, ctx: Context) -> dict[str, Any]:
        """(screen only) Sign off a verified abstract (second pair of eyes)."""
        b = need(borrower)
        try:
            SO.sign(b, who(ctx), abs_dir)
        except SO.SignoffError as e:
            raise ToolError(str(e))
        return _view(ctx, b)

    html = (UI_DIR / "review.html").read_text(encoding="utf-8") if (UI_DIR / "review.html").exists() else \
        "<p>Review screen not built: run npm run build:ui</p>"
    # Built by hand (add_html_resource can't carry extra _meta). "openai/ui".availableDisplayModes = ["fullscreen"]
    # tells ChatGPT to open the screen full screen straight away, with no inline loading state. Hosts that ignore the
    # openai/ keys use the standard bridge; the view declares inline+fullscreen there and asks for fullscreen on load.
    apps.add_resource(TextResource(
        uri=UI_URI, name="Covenant review", title="Covenant review", text=html,
        description="Checker screen: open items, covenant schedules, highlighted source pages",
        meta={"openai/ui": {"availableDisplayModes": UI_DISPLAY_MODES_OPENAI},
              "openai/widgetDescription": "Covenant review screen: open items with evidence, covenant schedules and "
                                          "the agreement page. The person decides, verifies and signs off here."}))

    server = MCPServer(
        "covenant-agent", version="0.1.0",
        instructions=("Covenant abstraction: credit agreement PDF in, checked covenant abstract out, human sign-off. "
                      "Code builds and checks the abstract; you explain it and route the person to open items. "
                      "Never do covenant arithmetic yourself: every value and date comes from get_abstract. "
                      "resolve_item, verify and sign_off ask the person directly with a form; you can propose a "
                      "decision but only their answer counts. open_review shows the review screen. "
                      "For a borrower not yet abstracted, find_credit_agreements looks it up on SEC EDGAR and "
                      "start_from_edgar starts the abstraction once the person picks the agreement."),
        extensions=[apps])

    def who_or_none(ctx):
        return authenticated_principal(ctx.request_context) or user or None

    def who(ctx):
        p = authenticated_principal(ctx.request_context)
        if p:
            return p
        if user:
            return user
        raise ToolError("no reviewer identity on this connection: set COVENANT_USER for local use, or connect with auth")

    def need(b):
        b = _ticker(b)
        if not st.exists(b, "abstract"):
            raise ToolError(f"no abstract for {b}; known deals: {', '.join(st.deals())}")
        return b

    # ---------------- read ----------------

    @server.tool(annotations=READ)
    def list_deals() -> dict[str, Any]:
        """Every deal with an abstract: sign-off status, open review items, and any background job."""
        out = []
        running = {j["borrower"]: j["job_id"] for j in jobs.all() if j["state"] == "running"}
        for b in st.deals():
            ab, rec, h = SO.load(b, abs_dir)
            its, open_, deferred, stale = SO.summary(ab, rec)
            out.append({"borrower": b, "status": rec["status"], "items": len(its), "open": len(open_),
                        "deferred": len(deferred), "covenants": [c["metric"] for c in ab["covenants"]],
                        "running_job": running.get(b)})
        return {"deals": out}

    @server.tool(annotations=READ)
    def get_abstract(borrower: str, section: Literal[tuple(SECTIONS)] = "summary") -> dict[str, Any]:
        """One section of a deal's abstract. 'summary' = status, covenants with their schedules, regimes, counts.
        Other sections return that part of the abstract as stored (quotes included)."""
        b = need(borrower)
        ab, rec, h = SO.load(b, abs_dir)
        if section == "summary":
            its, open_, _, _ = SO.summary(ab, rec)
            return summarize(ab, rec, len(open_))
        if section == "flags":
            return {"flags": ab.get("flags", [])}
        return {section: ab.get(section)}

    @server.tool(annotations=READ)
    def list_review_items(borrower: str, open_only: bool = True) -> dict[str, Any]:
        """The flags and review items a person must resolve before the abstract can be verified, with the evidence
        quote for each and any resolution already recorded. Use the 'key' with resolve_item."""
        b = need(borrower)
        d = review_data(b, abs_dir)
        rows = [r for r in d["items"] if not (open_only and r["resolution"])]
        return {"borrower": b, "status": d["status"], "open": d["open"], "total": len(d["items"]), "items": rows}

    @server.tool(annotations=READ)
    def signoff_status(borrower: str) -> dict[str, Any]:
        """Sign-off state: draft / verified / signed_off, who did what, and what still blocks verification."""
        b = need(borrower)
        ab, rec, h = SO.load(b, abs_dir)
        its, open_, deferred, stale = SO.summary(ab, rec)
        blocks = []
        if open_:
            blocks.append(f"{len(open_)} unresolved items")
        if deferred:
            blocks.append(f"{len(deferred)} deferred items")
        if any(r.get("step") == "schema" for r in ab.get("review", [])):
            blocks.append("schema errors")
        return {"borrower": b, "status": rec["status"], "verified_by": rec.get("verified_by"),
                "signed_by": rec.get("signed_by"), "items": len(its), "open": len(open_), "deferred": len(deferred),
                "blocks_verify": blocks, "transitions": rec.get("transitions", [])[-10:]}

    @server.tool(annotations=READ)
    def run_status(job_id: str) -> dict[str, Any]:
        """Progress of a start_deal job: state (running / ready_for_review / stopped / failed / interrupted),
        the steps so far, and the result when finished."""
        j = jobs.get(job_id)
        return {k: j.get(k) for k in ("job_id", "borrower", "state", "started", "finished", "progress", "result")}

    # ---------------- run ----------------

    @server.tool(annotations=WRITE)
    def start_deal(borrower: str, pdf: str, run_ingest: bool = True, gpt_check: bool = False) -> dict[str, Any]:
        """Start abstracting a credit agreement in the background (ingest -> build -> checks -> review list). Takes
        minutes; poll run_status with the returned job_id. pdf is a path under data/pdf/<BORROWER>/. Nothing here
        marks the abstract verified."""
        b = _ticker(borrower)
        base = (config.ROOT / "data" / "pdf" / b).resolve()
        p = (config.ROOT / pdf).resolve()
        if base not in p.parents or p.suffix.lower() != ".pdf":
            raise ToolError(f"pdf must be a .pdf under data/pdf/{b}/")
        if not p.exists():
            have = sorted(x.name for x in base.glob("*.pdf")) if base.exists() else []
            raise ToolError(f"{pdf} not found; PDFs for {b}: {have or 'none (run scripts/fetch_filings.py)'}")
        j = jobs.start(b, str(p.relative_to(config.ROOT)), run_ingest=run_ingest, gpt=gpt_check)
        return {"job_id": j["job_id"], "borrower": b, "state": j["state"],
                "next": "poll run_status; when ready_for_review, call list_review_items"}

    # ---------------- EDGAR intake: find a borrower's agreement and start it, when the person asks ----------------
    WEB_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
    WEB_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)

    @server.tool(annotations=WEB_READ)
    def find_credit_agreements(borrower: str, years: int = 5) -> dict[str, Any]:
        """Look a public borrower up on SEC EDGAR: credit agreements and amendments it filed as exhibits (EX-10) in the
        last `years`, newest first. Code types each one from its cover (credit_agreement / amendment / consent);
        full_text_likely = the cover says it restates or attaches the whole agreement (a conformed copy), which is
        what abstraction needs. Also says whether this borrower is already abstracted. Show the person the list."""
        b = _ticker(borrower)
        try:
            r = edgar.find_agreements(b, years=max(1, min(int(years), 15)))
        except edgar.EdgarError as e:
            raise ToolError(str(e))
        if st.exists(b, "abstract"):
            rec = SO.load(b, abs_dir)[1]
            r["already_abstracted"] = {"status": rec["status"], "source_file": st.load(b, "abstract")["header"]["source_file"],
                                       "next": "open_review: this deal is already done"}
        else:
            r["already_abstracted"] = None
            r["next"] = ("ask the person which agreement to use (prefer the newest full-text conformed copy), then "
                         "call start_from_edgar with its url")
        return r

    @server.tool(annotations=WEB_WRITE)
    def start_from_edgar(borrower: str, url: str, gpt_check: bool = False) -> dict[str, Any]:
        """Start abstracting a borrower's credit agreement straight from SEC EDGAR: downloads the exhibit, renders it
        to PDF and runs the same background job as start_deal (minutes; poll run_status). Only when the person has
        asked for it and chosen the agreement (url from find_credit_agreements). Refuses a borrower that is already
        abstracted. Nothing here marks the abstract verified."""
        b = _ticker(borrower)
        if st.exists(b, "abstract"):
            rec = SO.load(b, abs_dir)[1]
            raise ToolError(f"{b} is already abstracted (status {rec['status']}); use open_review. "
                            "To abstract a newer agreement, a person runs start_deal on its PDF.")
        try:
            edgar.check_url(b, url)
        except edgar.EdgarError as e:
            raise ToolError(str(e))
        pdf = edgar.pdf_path(b, url)
        j = jobs.start(b, pdf, run_ingest=True, gpt=gpt_check, fetch_url=url)
        return {"job_id": j["job_id"], "borrower": b, "state": j["state"], "pdf": pdf, "source": url,
                "next": "poll run_status; when ready_for_review, call open_review"}

    # ---------------- decisions: the person answers, not the model ----------------

    async def ask_resolution(borrower: str, key: str, proposed_decision: str = "accept", note: str = "",
                             correction: str = ""):
        b = need(borrower)
        its = SO.items(st.load(b, "abstract"))
        if key not in its:
            raise ToolError(f"no item {key!r} on {b}; see list_review_items")
        Form = create_model(
            "Resolution",
            decision=(Literal["accept", "correct", "reject", "defer"],
                      Field(default=proposed_decision if proposed_decision in SO.DECISIONS else "accept",
                            description="accept = abstract is right; correct = give the right value; "
                                        "reject = item is wrong; defer = decide later (blocks verify)")),
            note=(str, Field(default=note, description="Why (kept in the sign-off record)")),
            correction=(str, Field(default=correction, description="Corrected value (only for 'correct')")))
        return Elicit(f"{b} [{its[key]['kind']}] {its[key]['text']}\n\nYour decision:", Form)

    @server.tool(annotations=WRITE)
    async def resolve_item(borrower: str, key: str, ctx: Context,
                           answer: Annotated[ElicitationResult[Any], Resolve(ask_resolution)],
                           proposed_decision: str = "accept", note: str = "", correction: str = "") -> dict[str, Any]:
        """Record a person's decision on one review item. The person sees the item and chooses accept / correct /
        reject / defer in a form; proposed_decision, note and correction only pre-fill it."""
        b = need(borrower)
        by = who(ctx)
        if not isinstance(answer, AcceptedElicitation):
            return {"borrower": b, "key": key, "recorded": False, "why": {"decline": "person declined the form", "cancel": "form was cancelled (no answer from a person)"}[answer.action]}
        a = answer.data
        try:
            SO.resolve(b, key, a.decision, by, a.note, a.correction, abs_dir)
        except SO.SignoffError as e:
            raise ToolError(str(e))
        return {"borrower": b, "key": key, "recorded": True, "decision": a.decision, "by": by}

    async def ask_verify(borrower: str):
        b = need(borrower)
        ab, rec, h = SO.load(b, abs_dir)
        its, open_, deferred, _ = SO.summary(ab, rec)
        return Elicit(f"Verify the {b} abstract? {len(its)} items, {len(open_)} open, {len(deferred)} deferred. "
                      f"Verifying records that you checked every item against the agreement.", Confirm)

    @server.tool(annotations=WRITE)
    async def verify(borrower: str, ctx: Context,
                     answer: Annotated[ElicitationResult[Confirm], Resolve(ask_verify)]) -> dict[str, Any]:
        """Mark an abstract verified (first pair of eyes). The person confirms in a form. Refused while any item is
        open or deferred."""
        b = need(borrower)
        by = who(ctx)
        if not (isinstance(answer, AcceptedElicitation) and answer.data.confirm):
            return {"borrower": b, "verified": False, "why": "person did not confirm"}
        try:
            SO.verify(b, by, abs_dir)
        except SO.SignoffError as e:
            raise ToolError(str(e))
        return {"borrower": b, "verified": True, "by": by, "status": "verified"}

    async def ask_sign(borrower: str):
        b = need(borrower)
        ab, rec, h = SO.load(b, abs_dir)
        return Elicit(f"Sign off the {b} abstract (verified by {rec.get('verified_by')})? Sign-off must be by a "
                      f"different person than the verifier.", Confirm)

    @server.tool(annotations=WRITE)
    async def sign_off(borrower: str, ctx: Context,
                       answer: Annotated[ElicitationResult[Confirm], Resolve(ask_sign)]) -> dict[str, Any]:
        """Sign off a verified abstract (second pair of eyes; must be a different person than the verifier). The
        person confirms in a form."""
        b = need(borrower)
        by = who(ctx)
        if not (isinstance(answer, AcceptedElicitation) and answer.data.confirm):
            return {"borrower": b, "signed_off": False, "why": "person did not confirm"}
        try:
            SO.sign(b, by, abs_dir)
        except SO.SignoffError as e:
            raise ToolError(str(e))
        return {"borrower": b, "signed_off": True, "by": by, "status": "signed_off"}

    server.jobs = jobs
    return server


def add_dev_host(server):
    """A minimal MCP Apps host for trying the review screen locally: http://127.0.0.1:<port>/ . It connects back to
    this server's /mcp endpoint, calls open_review and renders the view in a sandboxed iframe. For local testing only."""
    from starlette.responses import HTMLResponse, Response

    @server.custom_route("/", methods=["GET"])
    async def host_page(request):
        return HTMLResponse((UI_DIR / "dev_host.html").read_text(encoding="utf-8"))

    @server.custom_route("/dev_host.js", methods=["GET"])
    async def host_js(request):
        return Response((UI_DIR / "dev_host.js").read_text(encoding="utf-8"), media_type="text/javascript")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", action="store_true", help="streamable HTTP instead of stdio")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--dev-host", action="store_true", help="also serve a local test host page at / (with --http)")
    a = ap.parse_args()
    s = create_server()
    if a.http:
        if a.dev_host:
            add_dev_host(s)
        s.run("streamable-http", host="127.0.0.1", port=a.port)
    else:
        s.run("stdio")


if __name__ == "__main__":
    main()
