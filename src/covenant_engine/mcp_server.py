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

UI_DIR = Path(__file__).parent / "ui"
UI_LEGACY_URI = "ui://covenant-agent/review.html"     # pre-hash URI: still served so old conversations render


def view_html():
    p = UI_DIR / "review.html"
    return p.read_text(encoding="utf-8") if p.exists() else "<p>Review screen not built: run npm run build:ui</p>"


def view_uri(html):
    """Content-hashed widget URI: hosts cache a ui:// resource by URI, so a new build gets a new URI and is fetched
    fresh instead of a stale cached copy. Same HTML -> same URI (stable across restarts and machines)."""
    import hashlib
    return f"ui://covenant-agent/review-{hashlib.sha256(html.encode('utf-8')).hexdigest()[:12]}.html"


UI_URI = view_uri(view_html())
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

    def runner(b, pdf, progress, fetch_url=None, on_file=None, **kw):
        # A deal already on file: report it and finish (state as the sign-off record has it); the engine doesn't run.
        if on_file:
            progress(f"on file: abstract built from {on_file['source_file']} (status {on_file['status']}); not re-run")
            progress(f"edgar: {on_file['match']}")
            return {"status": "ready_for_review" if on_file["status"] == "draft" else on_file["status"], "deal": b,
                    "on_file": True, "next": "open_review"}
        # An EDGAR intake job fetches and renders the exhibit first, inside the background job, then abstracts it.
        if fetch_url:
            edgar.fetch_pdf(b, fetch_url, progress=progress)
        return abstract_runner(b, pdf, progress=progress, **kw)

    st = store(abs_dir)
    jobs = Jobs(runner, job_dir or config.ROOT / "data" / "jobs")
    user = user if user is not None else os.environ.get("COVENANT_USER")
    apps = Apps()

    # ---------------- the review screen (MCP App view) ----------------
    html = view_html()
    ui_uri = view_uri(html)
    # Every tool that renders the screen points at the hashed URI: _meta.ui.resourceUri (MCP Apps standard) and
    # openai/outputTemplate (ChatGPT's alias, same value).
    VIEW = dict(resource_uri=ui_uri, meta={"openai/outputTemplate": ui_uri})
    # open_review is for the model and the view. The rest are app-only (_meta.ui.visibility = ["app"]): the model never
    # sees them, so a decision recorded through them is a person's click in the view. Identity still comes from the
    # connection, never from an argument.

    def _view(ctx, b):
        return RV.payload(b, who_or_none(ctx), abs_dir)

    @apps.tool(**VIEW, annotations=READ)
    def open_review(borrower: str, ctx: Context) -> dict[str, Any]:
        """Open the review screen for a deal: open items with their evidence, the covenant schedules, and the agreement
        page with each quote highlighted. The person resolves items, verifies and signs off in the screen."""
        b = need(borrower)
        p = _view(ctx, b)
        pkg = RV.package(b, who_or_none(ctx), abs_dir)
        return {k: p[k] for k in ("borrower", "name", "status", "open", "total", "verified_by", "signed_by")} | \
            {"summary": RV.brief(pkg), "screen": p, "view": ui_uri,
             "note": "The review screen shows this deal; decisions are made by the person in the screen."}


    @apps.tool(**VIEW, visibility=["app"], annotations=READ)
    def review_page(borrower: str, page: int) -> dict[str, Any]:
        """(screen only) One agreement page as an image."""
        b = need(borrower)
        try:
            return {"page": page, "image": RV.page_image(b, page, abs_dir)}
        except (FileNotFoundError, IndexError, ValueError) as e:
            raise ToolError(str(e))

    @apps.tool(**VIEW, visibility=["app"], annotations=WRITE)
    def review_decide(borrower: str, key: str, decision: Literal["accept", "correct", "reject", "defer"], ctx: Context,
                      note: str = "", correction: str = "", changes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """(screen only) Record the person's decision on one item. A correction with `changes` ([{path, value}] from the
        item's fields) is applied to the abstract, re-validated and logged before -> after."""
        b = need(borrower)
        try:
            SO.resolve(b, key, decision, who(ctx), note, correction, abs_dir, changes=changes)
        except SO.SignoffError as e:
            raise ToolError(str(e))
        return _view(ctx, b)

    @apps.tool(**VIEW, visibility=["app"], annotations=WRITE)
    def review_verify(borrower: str, ctx: Context) -> dict[str, Any]:
        """(screen only) Verify the abstract (first pair of eyes)."""
        b = need(borrower)
        try:
            SO.verify(b, who(ctx), abs_dir)
        except SO.SignoffError as e:
            raise ToolError(str(e))
        return _view(ctx, b)

    @apps.tool(**VIEW, visibility=["app"], annotations=WRITE)
    def review_sign(borrower: str, ctx: Context) -> dict[str, Any]:
        """(screen only) Sign off a verified abstract (second pair of eyes)."""
        b = need(borrower)
        try:
            SO.sign(b, who(ctx), abs_dir)
        except SO.SignoffError as e:
            raise ToolError(str(e))
        return _view(ctx, b)

    # Built by hand (add_html_resource can't carry extra _meta). "openai/ui".availableDisplayModes = ["fullscreen"]
    # tells ChatGPT to open the screen full screen straight away, with no inline loading state. Hosts that ignore the
    # openai/ keys use the standard bridge; the view declares inline+fullscreen there and asks for fullscreen on load.
    meta = {"openai/ui": {"availableDisplayModes": UI_DISPLAY_MODES_OPENAI},
            "openai/widgetDescription": "Covenant review screen: open items with evidence, covenant schedules and "
                                        "the agreement page. The person decides, verifies and signs off here."}
    for uri in (ui_uri, UI_LEGACY_URI):   # current (hashed) + legacy alias serving the same, current HTML
        apps.add_resource(TextResource(uri=uri, name="Covenant review", title="Covenant review", text=html, meta=meta,
                                       description="Checker screen: open items, covenant schedules, highlighted source pages"))

    server = MCPServer(
        "covenant-agent", version="0.1.0",
        instructions=("Covenant abstraction: credit agreement PDF in, checked covenant abstract out, human sign-off. "
                      "Code builds and checks the abstract; you explain it and route the person to open items. "
                      "Never do covenant arithmetic yourself: every value and date comes from get_abstract. "
                      "resolve_item, verify and sign_off ask the person directly with a form; you can propose a "
                      "decision but only their answer counts. open_review shows the review screen. "
                      "To start a new company, start_abstraction takes its name or ticker, finds its credit agreement on SEC "
                      "EDGAR and starts the run; find_credit_agreements lists the options."),
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
    def view_covenant_package(borrower: str, ctx: Context) -> dict[str, Any]:
        """Everything about one deal's review in a single call: status, covenants and schedules, step-up / relief
        regimes, every review item with its evidence quote, GPT's independent answer, how it is stored, the fields a
        correction can change and any decision or change already made, flagged definitions, and the recent trail.
        Call it whenever the person asks about the deal or an item while reviewing (it reflects their latest clicks).
        Reads only; values come from the abstract, never compute them."""
        return RV.package(need(borrower), who_or_none(ctx), abs_dir)


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
    def find_credit_agreements(company: str, years: int = 5) -> dict[str, Any]:
        """List a public company's credit agreements and amendments on SEC EDGAR (filed as exhibits to its 8-Ks, 10-Qs
        and 10-Ks), newest first, each typed by code from its cover. Use when the person wants to see the options;
        start_abstraction picks one itself."""
        try:
            b, _, _ = edgar.resolve(company)
            r = edgar.find_agreements(b, years=max(1, min(int(years), 15)))
        except edgar.EdgarError as e:
            raise ToolError(str(e))
        r["already_abstracted"] = _done(b)
        return r

    def _done(b):
        if not st.exists(b, "abstract"):
            return None
        return {"status": SO.load(b, abs_dir)[1]["status"], "source_file": st.load(b, "abstract")["header"]["source_file"]}

    def _on_file_url(b, source_file):
        """EDGAR url of the document an on-file abstract was built from (data/manifest.csv), if known."""
        import csv
        f = config.DATA / "manifest.csv"
        if not f.exists():
            return None
        stem = Path(source_file).stem
        for r in csv.DictReader(open(f, encoding="utf-8")):
            if r.get("ticker") == b and Path(r.get("local_path", "")).stem == stem:
                return r.get("url")
        return None

    @server.tool(annotations=WEB_WRITE)
    def start_abstraction(company: str, url: str = "", gpt_check: bool = False) -> dict[str, Any]:
        """Start the covenant abstraction for a public company ("start the covenant abstraction for Cars.com").
        Takes a company name or ticker, looks it up on SEC EDGAR, finds its credit agreement among the exhibits to its
        8-K / 10-Q / 10-K filings, picks the newest full-text agreement (code decides; pass url only if the person
        chose a different one from find_credit_agreements), downloads it, renders it to PDF and starts the background
        abstraction (minutes; poll run_status, then open_review).
        A company already on file is not re-run: the job uses the abstract on file and finishes at once, so the flow
        continues the same way (run_status -> open_review). Nothing here marks the abstract verified."""
        try:
            b, cik, name = edgar.resolve(company)
        except edgar.EdgarError as e:
            raise ToolError(str(e))
        done = _done(b)
        chosen, why, others, edgar_note = None, "", [], None
        try:
            if url:
                edgar.check_url(b, url)
                chosen, why = {"url": url}, "chosen by the person"
            else:
                found = edgar.find_agreements(b)
                chosen, why = edgar.pick(found["candidates"])
                others = [c for c in found["candidates"] if c is not chosen][:5]
                if chosen is None and not done:
                    return {"started": False, "company": name, "borrower": b, "reason": why,
                            "candidates": found["candidates"],
                            "next": "show the person the candidates; call again with the url they choose"}
        except edgar.EdgarError as e:
            if not done:
                raise ToolError(str(e))
            edgar_note = f"EDGAR lookup skipped ({e}); using the abstract on file"

        if done:
            # Already on file: don't re-run the engine. A job that finishes at once keeps the flow identical.
            on_url = _on_file_url(b, done["source_file"])
            if chosen and on_url:
                match = "EDGAR's newest full-text agreement is the one on file" if chosen["url"] == on_url else \
                    f"EDGAR has a different/newer agreement than the one on file: {chosen['url']}"
            else:
                match = edgar_note or "on-file source not matched to an EDGAR url"
            pdf = f"data/pdf/{b}/{done['source_file']}"
            j = jobs.start(b, pdf, on_file={"status": done["status"], "source_file": done["source_file"], "match": match})
            return {"started": True, "on_file": True, "job_id": j["job_id"], "company": name, "borrower": b,
                    "message": f"{name} is already on file (abstract built from {done['source_file']}, status "
                               f"{done['status']}); using it instead of re-running.",
                    "edgar_check": match, "document": chosen, "other_candidates": others,
                    "next": "poll run_status (finishes at once), then open_review"}

        pdf = edgar.pdf_path(b, chosen["url"])
        j = jobs.start(b, pdf, run_ingest=True, gpt=gpt_check, fetch_url=chosen["url"])
        return {"started": True, "on_file": False, "job_id": j["job_id"], "company": name, "borrower": b,
                "document": chosen, "why_this_one": why, "other_candidates": others, "pdf": pdf,
                "next": "tell the person which agreement was picked; poll run_status; when ready_for_review, open_review"}

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
