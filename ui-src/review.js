// Covenant review screen (MCP App view). Talks to the host through the MCP Apps bridge (ext-apps App class).
// Data comes from the open_review tool result; every decision is an app-only tool call made by the person's click.
import { App } from "@modelcontextprotocol/ext-apps";

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = v => "$" + (Number(v) / 1e6).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "M";
const fmtD = d => /^\d{4}-\d{2}-\d{2}$/.test(d || "") ? new Date(d + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : (d === "open" ? "thereafter" : (d || "—"));
const isMoney = c => c.unit === "usd" || /Expenditure|Liquidity|Cash|Net Worth/i.test(c.metric) && !/Ratio/i.test(c.metric);
const fmtV = (c, v) => isMoney(c) ? money(v) : Number(v).toFixed(2) + "x";
const ORIGIN = { flag: "Claude's question", "review/3": "Definition check", "review/loan": "Loan terms", "review/gpt": "", "review/calc": "Document not in the filings", "review/pricing": "Document not in the filings" };
const TYPE = { ambiguous_date: "Ambiguous date", conflict: "Conflict", dangling_reference: "Missing reference", missing_schedule: "Missing schedule", other: "" };

let D = null;                // the screen payload (from open_review / any review_* tool)
let sel = null, curPage = null, busy = false, correcting = null;
const pageCache = {};

// Declare both modes in ui/initialize (MCP Apps) - ChatGPT asks for this alongside the resource's openai/ui meta.
const MODES = ["inline", "fullscreen"];
const app = new App({ name: "covenant-review", version: "0.1.0" }, { availableDisplayModes: MODES }, { autoResize: true });
// ChatGPT's own globals (window.openai) - used only as a fallback when the standard bridge lacks something.
const oai = () => (typeof window !== "undefined" && window.openai) || null;

// ---------- host context: theme, display mode, safe area ----------
function applyContext(ctx) {
  if (!ctx) return;
  const o = oai();
  if (ctx.theme || (o && o.theme)) document.documentElement.setAttribute("data-theme", ctx.theme || o.theme);
  const mode = ctx.displayMode || (o && o.displayMode) || "inline";
  document.body.classList.toggle("fullscreen", mode === "fullscreen");
  document.body.classList.toggle("inline", mode !== "fullscreen");
  // Bottom inset: the chat composer overlays the frame in fullscreen. Standard field first, then ChatGPT's safeArea.
  const ins = ctx.safeAreaInsets || (o && o.safeArea && o.safeArea.insets) || {};
  document.documentElement.style.setProperty("--safe-b", (ins.bottom || 0) + "px");
  const h = ctx.containerDimensions && (ctx.containerDimensions.height || ctx.containerDimensions.maxHeight);
  if (h) document.documentElement.style.setProperty("--frame-h", h + "px");
  else document.documentElement.style.removeProperty("--frame-h");   // fall back to 100vh (the iframe itself)
  const modes = ctx.availableDisplayModes || (o ? MODES : []);
  const fs = $("fsBtn");
  fs.hidden = !modes.includes("fullscreen");
  fs.textContent = mode === "fullscreen" ? "Exit full screen" : "Full screen";
  requestAnimationFrame(() => document.documentElement.style.setProperty("--bar-h", document.querySelector(".bar").offsetHeight + "px"));
}

// Standard ui/request-display-mode first; ChatGPT's window.openai.requestDisplayMode if the host rejects it.
async function setMode(mode) {
  try {
    const r = await app.requestDisplayMode({ mode });
    applyContext({ ...(app.getHostContext() || {}), displayMode: r.mode });
    return r.mode;
  } catch (e) {
    const o = oai();
    if (!o || !o.requestDisplayMode) throw e;
    const r = await o.requestDisplayMode({ mode });
    const got = (r && r.mode) || mode;
    applyContext({ ...(app.getHostContext() || {}), displayMode: got });
    return got;
  }
}
$("fsBtn").onclick = async () => {
  const mode = document.body.classList.contains("fullscreen") ? "inline" : "fullscreen";
  try { await setMode(mode); }
  catch (e) { say("This host can't switch display mode: " + e.message); }
};

// ---------- server calls (app-only tools) ----------
async function call(name, args) {
  const r = await app.callServerTool({ name, arguments: args });
  if (r.isError) throw new Error(((r.content || []).map(c => c.text).join(" ") || "the server refused").replace(/^Error executing tool \w+: /, ""));
  return r.structuredContent;
}
async function act(name, args, okText) {
  if (busy) return;
  if (args && args.key) reopened.delete(args.key);
  busy = true; setBusy(true);
  try { render(await call(name, args)); if (okText) say(okText, true); }
  catch (e) { say(e.message); }
  finally { busy = false; setBusy(false); }
}
function setBusy(b) { document.querySelectorAll(".dbtn,.primary").forEach(x => x.toggleAttribute("aria-busy", b)); }

// ---------- source pane ----------
$("pageImg").onerror = () => { $("page").hidden = true; $("cap").innerHTML = '<span class="err">This page image could not be loaded.</span>'; };
async function pageImg(p) {
  if (!pageCache[p]) pageCache[p] = call("review_page", { borrower: D.borrower, page: p }).then(r => r.image);
  try { return await pageCache[p]; } catch (e) { delete pageCache[p]; throw e; }
}
// One page at a time, sized to fit the pane. ‹ › step through every page; the cited page carries the highlight.
let citedPage = null, citedCap = "";
const SPAN = 2;                                     // pages you can flip to either side of the cited page
const win = () => citedPage === null ? [0, D.locs.npages - 1]
  : [Math.max(0, citedPage - SPAN), Math.min(D.locs.npages - 1, citedPage + SPAN)];
async function goPage(p) {
  const [lo, hi] = win();
  p = Math.max(lo, Math.min(hi, p));
  curPage = p;
  $("pgLabel").textContent = `p. ${p + 1} / ${D.locs.npages}`;
  $("prev").disabled = p <= lo; $("next").disabled = p >= hi;
  $("page").style.aspectRatio = `${D.locs.pw} / ${D.locs.ph}`;
  if (!pageCache[p]) { $("pageImg").removeAttribute("src"); $("pageImg").alt = "Loading page…"; }
  let src;
  try { src = await pageImg(p); }
  catch (e) { $("cap").innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
  if (curPage !== p) return;
  $("page").hidden = false; $("pageImg").src = src; $("pageImg").alt = `Agreement page ${p + 1}`;
  drawHL(D.locs.loc[sel]);
  if (citedPage !== null && p !== citedPage) {
    $("cap").innerHTML = `${esc(citedCap)} · you're on page ${p + 1} <a href="#" id="backCited">back to page ${citedPage + 1}</a>`;
    $("backCited").onclick = e => { e.preventDefault(); goPage(citedPage); };
  } else if (citedPage !== null) $("cap").textContent = `${citedCap} · page ${p + 1}`;
  [p - 1, p + 1].filter(n => n >= lo && n <= hi).forEach(n => pageImg(n).catch(() => {}));   // next flip is instant
}
async function show(key, caption) {
  sel = key;
  document.querySelectorAll(".sel").forEach(e => e.classList.remove("sel"));
  document.querySelectorAll(`[data-loc="${CSS.escape(key)}"]`).forEach(e => e.classList.add("sel"));
  const l = D.locs.loc[key];
  if (!D.locs.pdf) {
    const it = key.startsWith("item:") && D.items.find(i => "item:" + i.key === key);
    $("noPdfQuote").textContent = it && it.evidence ? `${caption ? caption + " — " : ""}“${it.evidence}”` : "";
    $("cap").textContent = "No agreement PDF on this server, so pages can't be shown."; return;
  }
  if (!l) { $("cap").textContent = (caption ? caption + " · " : "") + "no quote from the agreement to highlight."; drawHL(null); return; }
  citedPage = l.page; citedCap = caption || "Cited text";
  await goPage(l.page);
}
function drawHL(l) {
  document.querySelectorAll(".hl").forEach(e => e.remove());
  if (!l || l.page !== curPage) return;
  let first = null;
  for (const [x0, y0, x1, y1] of l.rects) {
    const d = document.createElement("div");
    d.className = "hl";
    Object.assign(d.style, { left: x0 / D.locs.pw * 100 + "%", top: y0 / D.locs.ph * 100 + "%", width: (x1 - x0) / D.locs.pw * 100 + "%", height: (y1 - y0) / D.locs.ph * 100 + "%" });
    $("page").appendChild(d);
    first = first || d;
  }
  const scroll = () => { const sc = $("scroller"); sc.scrollTo({ top: Math.max(0, $("page").offsetTop + first.offsetTop - sc.clientHeight / 3) }); };
  if (first) $("pageImg").complete ? requestAnimationFrame(scroll) : $("pageImg").addEventListener("load", scroll, { once: true });
}
const step = dir => { if (curPage !== null) goPage(curPage + dir); };
$("prev").onclick = () => step(-1);
document.addEventListener("keydown", e => {        // ← → flip pages, unless the person is typing
  if (e.target.closest("input,textarea,select") || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === "ArrowLeft") step(-1); else if (e.key === "ArrowRight") step(1);
});
$("next").onclick = () => step(1);

// ---------- render ----------
function render(p) {
  D = p;
  $("loading").hidden = true;
  $("viewer").textContent = p.viewer || "not signed in";
  $("dealName").textContent = `${p.name} (${p.borrower})`;
  $("dealSub").textContent = `${p.header.source_file} · built ${p.header.built} · spec ${p.header.spec}`;
  $("file").textContent = p.header.source_file;
  // No PDF on the server: show an empty state instead of an empty <img> (which renders as a broken image).
  $("noPdf").hidden = !!p.locs.pdf;
  if (!p.locs.pdf) { $("page").hidden = true; $("pgLabel").textContent = ""; $("prev").hidden = $("next").hidden = true; }
  const f = p.facts;
  const facts = [["Agent", (f.agent || "—").replace(/, NATIONAL ASSOCIATION/i, "")],
    ["Facility", f.commitment ? `${money(f.commitment)} ${f.facility || ""}` : "—"], ["Matures", fmtD(f.maturity)],
    ["Latest amendment", fmtD(f.latest_amendment)], ["Covenants", `${p.covs.length} tests · ${p.regs.length} regimes`], ["Closing date", fmtD(f.closing)]];
  $("facts").innerHTML = facts.map(([k, v]) => `<div class="fact"><small>${k}</small><div>${esc(v)}</div></div>`).join("");
  pipeline(); items(); covenants(); trail(); progress();
  if (!sel && p.items.length) show("item:" + p.items[0].key, `§${p.items[0].where}`);
  else if (sel) document.querySelectorAll(`[data-loc="${CSS.escape(sel)}"]`).forEach(e => e.classList.add("sel"));
}
function pipeline() {
  const st = D.status;
  $("stDraft").className = "stage " + (st === "draft" ? "now" : "done");
  $("stVer").className = "stage " + (st === "verified" ? "now" : st === "signed_off" ? "done" : "");
  $("stSign").className = "stage " + (st === "signed_off" ? "done" : "");
  $("stVer").textContent = D.verified_by ? `Verified · ${D.verified_by}` : "Verified";
  $("stSign").textContent = D.signed_by ? `Signed off · ${D.signed_by}` : "Signed off";
}
// ---------- open items: grouped by review level ----------
// decide  = a person judges it (full card). confirm = both models read it the same way: one line each, Accept all
// (still one recorded decision per item, by the signed-in reviewer). done = resolved, collapsed.
let openGroups = { done: false, confirm: false };
const reopened = new Set();     // cards the person reopened to change a decision (a new decision overwrites the old)
const TIER = { 1: ["t1", "Changes pass/fail"], 2: ["t2", "Changes an input to the ratio"] };
function items() {
  const editable = D.status === "draft";
  const open = D.items.filter(i => !i.resolution);
  const decide = open.filter(i => i.level !== "confirm"), confirm = open.filter(i => i.level === "confirm");
  const done = D.items.filter(i => i.resolution);
  const head = `<div class="ihead"><b>${decide.length}</b> need your decision · <b>${confirm.length}</b> quick confirm · <b>${done.length}</b> done</div>`;
  const conf = confirm.length ? `<section class="group confirm-group"><div class="ghead"><h2>Quick confirm <em>${confirm.length} lower-risk item${confirm.length > 1 ? "s" : ""}: check each, or accept all</em></h2>
      ${editable ? `<button class="primary" id="acceptAll">Accept all ${confirm.length}</button>` : ""}</div>
      <ul class="clist">${confirm.map(it => `<li class="crow" data-key="${it.key}" data-loc="item:${it.key}"><span class="ref">${esc(it.where_label || whereLabel(it))}</span>
        <span class="csent">${esc(plain(it))}${it.kind === "flag" ? `<span class="cnote">Note: ${esc(it.what)}</span>` : ""}</span>
        ${editable ? `<span class="cbtns"><button class="dbtn" data-d="accept">Accept</button><button class="dbtn" data-open="1">Review in full</button></span>` : ""}</li>`).join("")}</ul></section>` : "";
  const dec = `<section class="group"><h2>Needs your decision <em>${decide.length ? "read the sentence, check it against the highlighted page, answer the question" : "nothing left to decide"}</em></h2>
      <div class="items">${decide.map(it => card(it, editable)).join("")}</div></section>`;
  const dn = done.length ? `<details class="group done-group" ${openGroups.done ? "open" : ""}><summary><h2>Done <em>${done.length} decided</em></h2></summary>
      <div class="items">${done.map(it => card(it, editable)).join("")}</div></details>` : "";
  $("view-items").innerHTML = head + dec + conf + dn;
  const dg = $("view-items").querySelector(".done-group");
  if (dg) dg.ontoggle = () => { openGroups.done = dg.open; };
  wireCards(editable);
  $("view-items").querySelectorAll(".crow").forEach(row => {
    const it = D.items.find(i => i.key === row.dataset.key);
    row.onclick = e => { if (!e.target.closest("button")) show(row.dataset.loc, (it.where_label || "§" + it.where)); };
    const acc = row.querySelector('[data-d="accept"]');
    if (acc) acc.onclick = () => act("review_decide", { borrower: D.borrower, key: it.key, decision: "accept", note: "Accepted in quick confirm." });
    const op = row.querySelector("[data-open]");
    if (op) op.onclick = () => { it.level = "decide"; items(); const c = document.querySelector(`.item[data-key="${it.key}"]`); if (c) { c.scrollIntoView({ block: "center" }); show("item:" + it.key, (it.where_label || "§" + it.where)); } };
  });
  const all = $("acceptAll");
  if (all) all.onclick = () => ask(`Accept all ${confirm.length} as ${D.viewer}? Each is recorded as accepted by you, one by one.`, "Accept all", async () => {
    busy = true; setBusy(true);
    let last = null;
    try { for (const it of confirm) last = await call("review_decide", { borrower: D.borrower, key: it.key, decision: "accept", note: "Accepted in quick confirm (accept all)." }); }
    catch (e) { say(e.message); }
    finally { busy = false; setBusy(false); if (last) { render(last); say(`Accepted ${confirm.length} items.`, true); } }
  });
}
// The statement as plain text (fields filled in with their current values).
function plain(it) {
  if (!it.statement || !it.statement.length) return it.what;
  return it.statement.filter(s => !s.edit).map(s => s.t != null ? s.t : fv(it.fields[s.f])).join("");
}
const usd = n => n >= 1000 ? "$" + (n / 1e6).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "M" : String(n);
const capText = v => String(v).split("|").map(p => { const m = p.match(/^excess_over:([\d.]+)$/); return m ? `the amount above ${usd(+m[1])}` : /^[\d.]+$/.test(p) ? usd(+p) : p.replace(/_/g, " "); }).join(" or ");
const fv = f => f.value === "none" && f.type === "enum" ? "" : (f.labels || {})[f.value] || (f.value && /^Cap /.test(f.label) ? capText(f.value) : f.value) || "not stated";
function inlineField(it, i, on) {
  const f = it.fields[i];
  // A compound limit (e.g. "the amount above $20M") reads as text; change it through the comment box.
  if (!on || (/^Cap /.test(f.label) && f.value && !/^[\d.,]+$/.test(f.value))) return `<b class="fval">${esc(fv(f))}</b>`;
  if (f.type === "enum") return `<select class="inl" data-i="${i}" title="${esc(f.label)}">${f.options.map(o => `<option value="${esc(o)}" ${o === f.value ? "selected" : ""}>${esc((f.labels || {})[o] || o)}</option>`).join("")}</select>`;
  const w = Math.max(4, Math.min(14, String(f.value || "").length + 2));
  return `<input class="inl" data-i="${i}" value="${esc(f.value)}" placeholder="${f.value ? "" : "none"}" style="width:${w + 1}ch" title="${esc(f.label)}" type="${f.type === "date" ? "date" : "text"}">`;
}
const whereLabel = it => { const w = it.where || ""; const m = w.match(/^term:(.+):\d+$/);
  return m ? m[1] : w.startsWith("cov:") || w.startsWith("reg:") ? w.split(":")[1].replace(/_/g, " ") : /^\d/.test(w) ? "§" + w : w || "—"; };
// The model's own reason for its answer (a flag's note, or its reading of a clause). Nothing about which check
// flagged the item: the reviewer judges the answer against the evidence.
const note = it => it.kind === "flag" ? it.what : (it.reading || "");
// Evidence = the full sentence (or lettered clause) from the agreement, with the quoted words marked.
function evidence(it) {
  const l = D.locs.loc["item:" + it.key];
  const sent = (l && l.sentence) || it.evidence;
  if (!sent) return "";
  const norm = t => String(t || "").replace(/\s+/g, " ").trim();
  const S = norm(sent), Q = norm(it.evidence);
  const at = Q ? S.toLowerCase().indexOf(Q.slice(0, 60).toLowerCase()) : -1;
  const body = at >= 0 ? esc(S.slice(0, at)) + "<mark>" + esc(S.slice(at, at + Q.length)) + "</mark>" + esc(S.slice(at + Q.length)) : esc(S);
  return `<div class="evid"><small>Evidence${l ? ` · page ${l.page + 1}, highlighted` : ""}</small><blockquote>${body}</blockquote>
    ${!l && D.locs.pdf ? '<div class="nosrc">This exact text couldn\'t be found in the PDF, so nothing is highlighted.</div>' : ""}</div>`;
}
function card(it, editable) {
  const r = it.resolution || {};
  const on = editable && (!r.decision || reopened.has(it.key));
  const hasSrc = !!D.locs.loc["item:" + it.key];
  const used = new Set((it.statement || []).filter(s => s.f != null).map(s => s.f));
  const extra = (it.fields || []).map((f, i) => [f, i]).filter(([f, i]) => !used.has(i));
  const sentence = it.statement && it.statement.length
    ? it.statement.filter(s => on || !s.edit).map(s => s.t != null ? esc(s.t) : inlineField(it, s.f, on)).join("")
    : esc(it.what);
  const [tc, tl] = TIER[it.tier] || TIER[1];
  const stated = !!(it.statement && it.statement.length);
  const btn = (k, t, cls = "") => `<button class="dbtn ${cls}" data-d="${k}" aria-pressed="${r.decision === k}" ${on ? "" : "disabled"}>${t}</button>`;
  return `<article class="item" data-key="${it.key}" data-loc="item:${it.key}" data-state="${r.decision || ""}" tabindex="0">
    <div class="item-top"><span class="tier ${tc}">${tl}</span><span class="ref">${esc(it.where_label || whereLabel(it))}</span><span class="origin">Flagged for review</span></div>
    ${stated ? `<div class="says"><small>The abstract says${on ? " · edit any value in place" : ""}</small><p>${sentence}</p></div>`
      : `<div class="says"><small>What needs a decision</small><p class="plainq">${esc(it.what)}</p></div>`}
    ${evidence(it)}
    ${note(it) ? `<div class="asked"><small>Model's note</small><p>${esc(note(it))}</p></div>` : ""}
    ${it.detail || extra.length ? `<details class="more"><summary>More context${extra.length ? " and fields" : ""}</summary>
      ${it.detail ? context(it) : ""}
      ${extra.length ? `<div class="xfields">${extra.map(([f, i]) => `<label><span>${esc(f.label)}</span>${inlineField(it, i, on)}</label>`).join("")}</div>` : ""}</details>` : ""}
    ${on ? `<div class="q"><b>${stated ? "Is the abstract right about this?" : "Is the abstract OK as it stands?"}</b>
      <div class="decide">${btn("accept", "Yes, it's right", "yes")}<button class="dbtn save" data-save="1" disabled>Save my changes</button>${btn("reject", "Doesn't belong")}${btn("defer", "Decide later")}</div>
      <textarea id="note-${it.key}" placeholder="${it.fields && it.fields.length ? "Comment: how should it change, or why (goes in the sign-off record)" : "If it's wrong, say how it should read (recorded with your decision)"}">${esc(r.note || "")}</textarea>
      ${!(it.fields && it.fields.length) ? `<button class="dbtn" data-comment="1">It's wrong: record my comment</button>` : ""}</div>`
    : `<div class="decide">${r.by ? `<span class="stamp ${r.decision}">${{ accept: "Accepted", correct: "Changed", reject: "Doesn't belong", defer: "Deferred" }[r.decision] || esc(r.decision)} · ${esc(r.by)} · ${esc(r.date)}${r.note ? " · “" + esc(r.note) + "”" : ""}</span>` : ""}
      ${editable && r.decision ? `<button class="ghost small" data-undo="1">Change decision</button>` : ""}</div>`}
    ${(r.changes || []).length ? `<ul class="changes">${r.changes.map(c => `<li><b>${esc(c.label)}</b> <s>${esc(c.before)}</s> → ${esc(c.after)} <small>${esc(c.group)}</small></li>`).join("")}</ul>` : ""}
  </article>`;
}
function wireCards(editable) {
  $("view-items").querySelectorAll(".item").forEach(card => {
    const it = D.items.find(i => i.key === card.dataset.key);
    const note = () => (card.querySelector("textarea") || {}).value || "";
    const changed = () => [...card.querySelectorAll(".inl")].filter(el => String(el.value).trim() !== String(it.fields[Number(el.dataset.i)].value));
    card.addEventListener("click", e => {
      const j = e.target.closest(".jump");
      if (j) { e.preventDefault(); const t = j.dataset.key && document.querySelector(`.item[data-key="${j.dataset.key}"]`); if (t) { t.scrollIntoView({ block: "center" }); show("item:" + j.dataset.key, ""); } return; }
      if (!e.target.closest("button,input,textarea,select,summary,details,a")) show(card.dataset.loc, (it.where_label || "§" + it.where));
    });
    const save = card.querySelector("[data-save]");
    card.querySelectorAll(".inl").forEach(el => el.addEventListener("input", () => {
      const n = changed();
      card.querySelectorAll(".inl").forEach(x => x.classList.toggle("dirty", n.includes(x)));
      if (save) { save.disabled = !n.length; save.textContent = n.length ? `Save ${n.length} change${n.length > 1 ? "s" : ""}` : "Save my changes"; }
      const yes = card.querySelector(".yes"); if (yes) yes.disabled = !!n.length;
    }));
    card.querySelectorAll(".dbtn[data-d]").forEach(b => b.onclick = () =>
      act("review_decide", { borrower: D.borrower, key: it.key, decision: b.dataset.d, note: note() }));
    if (save) save.onclick = () => {
      const changes = changed().map(el => ({ path: it.fields[Number(el.dataset.i)].path, value: el.value }));
      if (!changes.length) return;
      act("review_decide", { borrower: D.borrower, key: it.key, decision: "correct", note: note(), changes }, "Change applied to the abstract.");
    };
    const cm = card.querySelector("[data-comment]");
    if (cm) cm.onclick = () => {
      const v = note().trim();
      if (!v) { say("Write how it should read first."); card.querySelector("textarea").focus(); return; }
      act("review_decide", { borrower: D.borrower, key: it.key, decision: "correct", note: v, correction: v });
    };
    const undo = card.querySelector("[data-undo]");
    if (undo) undo.onclick = () => { reopened.add(it.key); items(); };
  });
}
// ---------- term-check context: what the analyst needs to decide ----------
const SIGN = { "+": "add-back", "-": "deduction" };
function context(it) {
  const d = it.detail, t = (D.terms || {})[d.term];
  const relItem = c => D.items.find(i => i.detail && i.detail.term === d.term && i.detail.clause === c);
  const rel = d.related.map(r => { const ri = relItem(r.clause);
    return `<a href="#" class="jump" data-key="${ri ? ri.key : ""}">${esc(r.clause)}</a> (${SIGN[r.sign] || r.sign}, ${esc(r.category)}${r.flagged ? ", also flagged" : ""})`; }).join(", ");
  const rows = t ? t.rows.map(r => `<tr class="${r.clause === d.clause ? "this" : r.flagged ? "flagged" : ""}"><td>${esc(r.clause)}</td><td>${SIGN[r.sign] || esc(r.sign)}</td><td>${esc(r.category)}</td><td>${esc(r.quote)}</td></tr>`).join("") : "";
  return `<dl class="stored">
      <div><dt>Recorded as</dt><dd>${esc(d.effect)} · category <b>${esc(d.category)}</b></dd></div>
      <div><dt>Limits</dt><dd>${d.caps.length ? d.caps.map(esc).join("; ") : "None recorded. Check the clause for a dollar or % limit."}</dd></div>
      <div><dt>Amount comes from</dt><dd>${esc(d.source)}</dd></div>
      <div><dt>Used in</dt><dd>${d.affects.map(esc).join("; ") || "—"}</dd></div>
      ${rel ? `<div><dt>Related clauses</dt><dd>${rel}</dd></div>` : ""}
    </dl>
    ${t ? `<details class="defn"><summary>Whole definition: ${esc(d.term)} (${t.rows.length} clauses, starts from ${esc(t.starts_from || "—")})</summary>
      <table><thead><tr><th>Clause</th><th>Effect</th><th>Category</th><th>Text</th></tr></thead><tbody>${rows}</tbody></table></details>` : ""}`;
}

function chart(c) {
  if (c.frequency !== "quarterly" || isMoney(c)) return "";
  const rel = D.regs.flatMap(g => g.rows.filter(r => r.cov === c.id));
  const dated = [...c.rows, ...rel].filter(r => /^\d{4}/.test(r.from));
  if (!dated.length) return "";
  const years = dated.map(r => +r.from.slice(0, 4));
  const y0 = Math.min(...years), y1 = Math.max(...years) + 1;
  const t0 = new Date(`${y0}-01-01`).getTime(), t1 = new Date(`${y1}-12-31`).getTime();
  const vals = dated.map(r => Number(r.value));
  const lo = Math.floor(Math.min(...vals) * 2) / 2 - 0.5, hi = Math.ceil(Math.max(...vals) * 2) / 2 + 0.25;
  const W = 560, H = 150, L = 40, R = 12, T = 12, B = 24;
  const x = d => L + (new Date(d).getTime() - t0) / (t1 - t0) * (W - L - R);
  const y = v => T + (hi - v) / (hi - lo) * (H - T - B);
  const end = d => /^\d{4}/.test(d) ? d : `${y1}-12-31`;
  const seg = (rows, cls) => rows.filter(r => /^\d{4}/.test(r.from)).map(r => {
    const xa = x(r.from) - 9, xb = x(end(r.to)) + (/^\d{4}/.test(r.to) ? 9 : 0), yy = y(Number(r.value));
    return `<line x1="${xa.toFixed(1)}" x2="${xb.toFixed(1)}" y1="${yy.toFixed(1)}" y2="${yy.toFixed(1)}" class="${cls}"/><text x="${(xa + 2).toFixed(1)}" y="${(yy - 5).toFixed(1)}" class="lbl ${cls}">${Number(r.value).toFixed(2)}x</text>`;
  }).join("");
  let grid = "";
  for (let v = Math.ceil(lo * 2) / 2; v <= hi; v += 0.5) grid += `<line x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}" class="g"/><text x="${L - 6}" y="${y(v) + 4}" class="ax" text-anchor="end">${v.toFixed(1)}</text>`;
  for (let yr = y0 + 1; yr <= y1; yr++) grid += `<line x1="${x(yr + "-01-01")}" x2="${x(yr + "-01-01")}" y1="${T}" y2="${H - B}" class="g"/><text x="${x(yr + "-01-01")}" y="${H - 6}" class="ax" text-anchor="middle">${yr}</text>`;
  return `<div class="chart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(c.metric)} threshold over time">
    <style>.g{stroke:var(--line);stroke-width:1}.ax{fill:var(--muted);font:11px var(--mono)}.std{stroke:var(--ink);stroke-width:2.5;fill:var(--ink)}.rel{stroke:var(--relief);stroke-width:2.5;stroke-dasharray:5 3;fill:var(--relief)}.lbl{stroke:none;font:500 11px var(--mono)}</style>${grid}${seg(c.rows, "std")}${seg(rel, "rel")}</svg></div>`;
}
function covenants() {
  const S = D.sheet || { covenants: [], deal: {} };
  const chk = `<span class="checks"><span class="chk">matches the agreement ✓</span><span class="chk">valid test date ✓</span><span class="chk">no overlapping periods ✓</span></span>`;
  const n = S.covenants.length;
  const intro = `<div class="sheet-intro"><b>${esc(D.name)} has ${n} financial maintenance covenant${n === 1 ? "" : "s"}</b>
    <span>tested every period whether or not the borrower does anything.${(S.deal.other_tests || []).length ? ` The agreement also has ${S.deal.other_tests.length} other ratio tests that only apply when the borrower takes an action (listed below).` : ""}</span></div>`;
  const covCards = S.covenants.map(sc => {
    const c = D.covs.find(x => x.id === sc.id) || { rows: [] };
    const rel = D.regs.flatMap(g => g.rows.filter(r => r.cov === c.id).map(r => ({ ...r, g })));
    const row = (r, band, cap) => `<tr class="row" data-loc="${r.loc}" data-cap="${esc(cap)}"><td class="d">${fmtD(r.from)} → ${fmtD(r.to)}</td><td class="v">${fmtV(c, r.value)}</td><td>${band}</td><td>${chk}</td></tr>`;
    const rows = c.rows.map(r => row(r, `<span class="band std">Standard</span>`, `§${c.ref} · standard schedule`)).join("") +
      rel.map(r => row(r, `<span class="band rel">${r.g.type === "relief_period" ? "Relief" : "Regime"}</span>`, `${r.g.name}`)).join("");
    const f = sc.formula;
    const formula = f ? `<div class="formula"><span class="num">${f.numerator.map(esc).join(" + ") || "—"}${f.less.length ? ` <i>minus</i> ${f.less.map(esc).join(", ")}` : ""}</span><span class="div">÷</span><span class="den">${f.denominator.map(esc).join(" + ") || "—"}</span></div>` : "";
    const defs = sc.definitions.map(d => `<li><b>${esc(d.name)}</b>${d.starts_from ? ` starts from ${esc(d.starts_from)},` : ""} ${d.adds} item${d.adds === 1 ? "" : "s"} added, ${d.subtracts} subtracted.
        ${d.capped.length ? `<div class="capped">Capped: ${d.capped.map(esc).join(" · ")}</div>` : ""}${d.custom.length ? `<div class="sub">Non-standard clauses: ${d.custom.map(esc).join(", ")}</div>` : ""}</li>`).join("");
    const rp = sc.reported;
    const rep = rp ? (rp.cushion != null && rp.threshold
      ? `<div class="kv"><dt>Last reported</dt><dd><b class="mono">${esc(rp.value)}</b> vs ${esc(rp.threshold)} at ${fmtD(rp.period)} · <span class="pill ${rp.passes ? "ok" : "bad"}">${rp.passes ? "passes" : "fails"}, cushion ${esc(rp.cushion)}</span> <small>from ${esc(rp.doc || "the latest filing")}</small></dd></div>`
      : `<div class="kv"><dt>Last reported</dt><dd>${esc(rp.value)} at ${fmtD(rp.period)}</dd></div>`)
      : `<div class="kv"><dt>Last reported</dt><dd class="muted">No reported value in the filings read.</dd></div>`;
    return `<article class="cov"><div class="cov-head row" data-loc="cov:${sc.id}" data-cap="§${esc(sc.ref)} ${esc(sc.metric)}"><b>${esc(sc.metric)}</b><span class="ref">§${esc(sc.ref)}</span><span class="tag">${sc.direction === "max" ? "Maximum" : "Minimum"} · tested ${esc(sc.frequency)}</span></div>
      <p class="sentence">${esc(sc.sentence)}</p>
      <dl class="sheet">
        <div class="kv"><dt>Step-ups and relief</dt><dd>${sc.regimes.length ? sc.regimes.map(x => `<p>${esc(x)}</p>`).join("") : "None in the agreement."}</dd></div>
        ${rep}
      </dl>
      ${formula || defs ? `<details class="sched"><summary>Covenant details: definitions and add-backs</summary><div class="details-body">${formula}${defs ? `<ul class="defs">${defs}</ul>` : ""}</div></details>` : ""}
      <details class="sched"><summary>Schedule of levels (${c.rows.length + rel.length} row${c.rows.length + rel.length === 1 ? "" : "s"})</summary>
        ${chart(c)}<div class="tblwrap"><table><thead><tr><th>Test dates</th><th>Level</th><th>Schedule</th><th>Checks</th></tr></thead><tbody>${rows}</tbody></table></div>
        <blockquote class="row" data-loc="cov:${sc.id}" data-cap="§${esc(sc.ref)}">${esc(sc.quote)}</blockquote></details></article>`;
  }).join("");
  const d = S.deal;
  const pg = d.pricing ? `<div class="kv"><dt>Pricing grid</dt><dd>Margin steps with ${esc(d.pricing.metric)}.
      <div class="tblwrap"><table class="mini"><thead><tr><th>Level</th><th>Ratio</th><th>SOFR +</th><th>Base +</th><th>Commitment fee</th></tr></thead><tbody>${d.pricing.rows.map(r => `<tr><td>${esc(r.level)}</td><td class="d">${esc(r.range)}</td><td class="v">${esc(r.sofr)}</td><td class="v">${esc(r.base)}</td><td class="v">${esc(r.fee)}</td></tr>`).join("")}</tbody></table></div></dd></div>` : "";
  const deal = `<article class="cov"><div class="cov-head"><b>Deal terms that affect testing</b></div><dl class="sheet">
      ${d.reporting ? `<div class="kv"><dt>Reporting</dt><dd>${esc(d.reporting)}${d.fiscal_year_end ? ` Fiscal year ends ${esc(d.fiscal_year_end)}.` : ""}</dd></div>` : ""}
      <div class="kv"><dt>Equity cure</dt><dd>${esc(d.cure)}</dd></div>
      ${(d.deemed || []).length ? `<div class="kv"><dt>Deemed values</dt><dd>${d.deemed.map(esc).join("<br>")}</dd></div>` : ""}
      ${(d.calc_rules || []).length ? `<div class="kv"><dt>Calculation rules</dt><dd><details><summary class="lnk">${d.calc_rules.length} rules (GAAP, pro forma)</summary><ul class="defs">${d.calc_rules.map(x => `<li>${esc(x)}</li>`).join("")}</ul></details></dd></div>` : ""}
      ${pg}</dl></article>`;
  const ot = d.other_tests || [];
  const other = ot.length ? `<article class="cov"><div class="cov-head"><b>Other ratio tests in the agreement</b><span class="tag">found in the agreement text · not reviewed</span></div>
      <p class="sentence sm">These apply only when the borrower takes an action (borrows more, pays a dividend, makes an acquisition). They aren't tested each quarter.</p>
      <div class="tblwrap"><table class="mini"><thead><tr><th>When it applies</th><th>Test</th><th>Section</th></tr></thead><tbody>${ot.map(t => `<tr title="${esc(t.context)}"><td>${esc(t.gates)}</td><td><b>${esc(t.ratio)}</b> ${t.test === "max" ? "≤" : "≥"} <span class="mono">${esc(Number(t.level).toFixed(2))}x</span></td><td class="d">${t.section ? (t.section === "1.01" ? "definitions" : "§" + esc(t.section)) : "—"}</td></tr>
        <tr class="ctx"><td colspan="3">${esc(t.context)}</td></tr>`).join("")}</tbody></table></div></article>` : "";
  $("view-cov").innerHTML = `<div class="items">${intro}${covCards}${deal}${other}<p class="note">Dashed purple in a chart = relief schedule.</p></div>`;
  $("view-cov").querySelectorAll(".row").forEach(e => e.onclick = () => show(e.dataset.loc, e.dataset.cap));
}
function trail() {
  $("view-trail").innerHTML = `<h2>Sign-off record <em>every decision and status change</em></h2><div class="trail">` +
    D.trail.map(r => `<div><span>${esc(r.date)}</span><span style="font-family:var(--ui);color:var(--ink)">${esc(r.by)} · ${esc(r.what)}</span></div>`).join("") + `</div>`;
}
function progress() {
  const n = D.total, done = n - D.open;
  $("nOpen").textContent = D.open;
  $("meter").style.width = (n ? done / n * 100 : 100) + "%";
  $("confirm").hidden = true;
  const btn = $("mainBtn");
  if (D.status === "draft") {
    $("progText").textContent = `${done} of ${n} items resolved` + (D.deferred ? ` · ${D.deferred} deferred (blocks verify)` : "");
    btn.textContent = "Verify abstract";
    btn.disabled = D.open > 0 || D.deferred > 0;
    btn.onclick = () => ask(`Verify as ${D.viewer}: this records that you checked every item against the agreement.`, "Confirm verify",
      () => act("review_verify", { borrower: D.borrower }, "Verified. A different reviewer must sign off."));
  } else if (D.status === "verified") {
    $("progText").textContent = `Verified by ${D.verified_by}. Waiting for a second reviewer to sign off.`;
    btn.textContent = "Sign off"; btn.disabled = false;
    btn.onclick = () => ask(`Sign off as ${D.viewer}. The verifier was ${D.verified_by}.`, "Confirm sign-off",
      () => act("review_sign", { borrower: D.borrower }, "Signed off."));
  } else {
    $("progText").textContent = `Signed off by ${D.signed_by}. Any change to the abstract returns it to draft.`;
    btn.textContent = "Signed off"; btn.disabled = true;
  }
}
function ask(text, ok, fn) {
  const c = $("confirm");
  c.innerHTML = `<span>${esc(text)}</span><button class="primary" id="okBtn">${ok}</button><button class="ghost" id="noBtn">Cancel</button>`;
  c.hidden = false; $("msg").hidden = true;
  $("okBtn").onclick = () => { c.hidden = true; fn(); };
  $("noBtn").onclick = () => { c.hidden = true; };
}
function say(t, good) { const m = $("msg"); m.textContent = t; m.className = "msg" + (good ? " good" : ""); m.hidden = false; }

// ---------- tabs ----------
const TABS = ["items", "cov", "trail"];
TABS.forEach(t => $("tab-" + t).onclick = () => TABS.forEach(u => { $("tab-" + u).setAttribute("aria-selected", u === t); $("view-" + u).hidden = u !== t; }));

// ---------- connect ----------
app.ontoolresult = r => {
  const p = r.structuredContent && (r.structuredContent.screen || r.structuredContent);
  if (p && p.items) render(p);
  else { $("loading").textContent = "The tool didn't return review data."; }
};
app.onhostcontextchanged = ctx => applyContext({ ...(app.getHostContext() || {}), ...ctx });
app.onerror = e => say(String(e && e.message || e));
document.body.classList.add("inline");
// The review screen is a full-screen tool. ChatGPT opens it full screen from the resource meta (openai/ui);
// any host that still starts it inline gets one request to go full screen. Failure is fine: inline layout works.
async function preferFullscreen() {
  const ctx = app.getHostContext() || {};
  const o = oai();
  const mode = ctx.displayMode || (o && o.displayMode) || "inline";
  const can = (ctx.availableDisplayModes || (o ? MODES : [])).includes("fullscreen");
  if (mode !== "fullscreen" && can) { try { await setMode("fullscreen"); } catch (e) { /* stay inline */ } }
}
window.addEventListener("openai:set_globals", () => applyContext(app.getHostContext() || {}));
app.connect()
  .then(() => { applyContext(app.getHostContext()); return preferFullscreen(); })
  .catch(e => { $("loading").textContent = "Couldn't connect to the host: " + e.message; });
