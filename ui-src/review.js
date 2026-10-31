// Covenant review screen (MCP App view). Talks to the host through the MCP Apps bridge (ext-apps App class).
// Data comes from the open_review tool result; every decision is an app-only tool call made by the person's click.
import { App } from "@modelcontextprotocol/ext-apps";

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const money = v => "$" + (Number(v) / 1e6).toLocaleString("en-US", { maximumFractionDigits: 1 }) + "M";
const fmtD = d => /^\d{4}-\d{2}-\d{2}$/.test(d || "") ? new Date(d + "T00:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : (d === "open" ? "thereafter" : (d || "—"));
const isMoney = c => c.unit === "usd" || /Expenditure|Liquidity|Cash|Net Worth/i.test(c.metric) && !/Ratio/i.test(c.metric);
const fmtV = (c, v) => isMoney(c) ? money(v) : Number(v).toFixed(2) + "x";
const ORIGIN = { flag: "Flagged by the extraction model", "review/3": "Term check (code) could not confirm", "review/loan": "Loan step left for review", "review/gpt": "Second model disagreed" };
const TYPE = { ambiguous_date: "Ambiguous date", conflict: "Conflict", dangling_reference: "Dangling reference", missing_schedule: "Missing schedule", other: "Needs a look" };

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
  busy = true; setBusy(true);
  try { render(await call(name, args)); if (okText) say(okText, true); }
  catch (e) { say(e.message); }
  finally { busy = false; setBusy(false); }
}
function setBusy(b) { document.querySelectorAll(".dbtn,.primary").forEach(x => x.toggleAttribute("aria-busy", b)); }

// ---------- source pane ----------
$("pageImg").onerror = () => { $("page").hidden = true; $("cap").innerHTML = '<span class="err">This page image could not be loaded.</span>'; };
async function goPage(p) {
  curPage = p;
  $("pgLabel").textContent = `p. ${p + 1} / ${D.locs.npages}`;
  if (!pageCache[p]) {
    $("pageImg").removeAttribute("src"); $("pageImg").alt = "Loading page…";
    try { pageCache[p] = (await call("review_page", { borrower: D.borrower, page: p })).image; }
    catch (e) { $("cap").innerHTML = `<span class="err">${esc(e.message)}</span>`; return; }
  }
  if (curPage === p) { $("page").hidden = false; $("pageImg").src = pageCache[p]; $("pageImg").alt = `Agreement page ${p + 1}`; }
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
  await goPage(l.page);
  drawHL(l);
  $("cap").textContent = (caption ? caption + " · " : "") + `page ${l.page + 1}`;
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
  const scroll = () => { const sc = $("scroller"); sc.scrollTo({ top: Math.max(0, first.offsetTop - sc.clientHeight / 3) }); };
  if (first) $("pageImg").complete ? requestAnimationFrame(scroll) : $("pageImg").addEventListener("load", scroll, { once: true });
}
const step = dir => { const ps = D.locs.pages, i = ps.indexOf(curPage); const n = ps[Math.min(ps.length - 1, Math.max(0, (i < 0 ? 0 : i) + dir))]; if (n !== undefined) goPage(n).then(() => drawHL(D.locs.loc[sel])); };
$("prev").onclick = () => step(-1);
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
function items() {
  const editable = D.status === "draft";
  $("view-items").innerHTML = D.items.map(it => {
    const r = it.resolution || {};
    const label = it.kind === "flag" ? (TYPE[it.type] || "Flag") : "Review";
    const hasSrc = !!D.locs.loc["item:" + it.key];
    const btn = (k, t) => `<button class="dbtn" data-d="${k}" aria-pressed="${r.decision === k}" ${editable ? "" : "disabled"}>${t}</button>`;
    const corr = correcting === it.key;
    return `<article class="item" data-key="${it.key}" data-loc="item:${it.key}" data-state="${r.decision || ""}" tabindex="0">
      <div class="item-top"><span class="kind${it.kind === "flag" ? "" : " review"}">${label}</span><span class="ref">§${esc(it.where || "—")}</span><span class="origin">${ORIGIN[it.kind] || esc(it.kind)}</span></div>
      ${it.detail && it.why ? `<div class="why">${esc(it.why)}</div>` : ""}
      <div class="what">${esc(it.detail && it.reading ? "Model's reading: " + it.reading : it.what)}</div>
      ${it.evidence ? (hasSrc ? `<blockquote><mark>${esc(it.evidence)}</mark></blockquote>` : `<blockquote>${esc(it.evidence)}</blockquote>${D.locs.pdf ? '<div class="nosrc">Not found word for word in the PDF, so there is nothing to highlight.</div>' : ""}`) : ""}
      ${it.detail ? context(it) : ""}
      ${gptLine(it)}
      ${editable ? `<div class="fields"><textarea id="note-${it.key}" placeholder="Note for the sign-off record (optional)">${esc(r.note || "")}</textarea>
        ${corr ? correctionForm(it, r) : ""}</div>` : ""}
      <div class="decide">${btn("accept", "Accept")}${btn("correct", "Correct")}${btn("reject", "Reject")}${btn("defer", "Defer")}
        ${r.by ? `<span class="stamp">${esc(r.decision)} · ${esc(r.by)} · ${esc(r.date)}${r.correction && !(r.changes || []).length ? " · " + esc(r.correction) : ""}</span>` : ""}</div>
      ${(r.changes || []).length ? `<ul class="changes">${r.changes.map(c => `<li><b>${esc(c.label)}</b> <s>${esc(c.before)}</s> → ${esc(c.after)} <small>${esc(c.group)}</small></li>`).join("")}</ul>` : ""}
    </article>`;
  }).join("");
  $("view-items").querySelectorAll(".item").forEach(card => {
    const it = D.items.find(i => i.key === card.dataset.key);
    const note = () => (card.querySelector("textarea") || {}).value || "";
    card.addEventListener("click", e => {
      const j = e.target.closest(".jump");
      if (j) { e.preventDefault(); const t = j.dataset.key && document.querySelector(`.item[data-key="${j.dataset.key}"]`); if (t) { t.scrollIntoView({ block: "center" }); show("item:" + j.dataset.key, ""); } return; }
      if (!e.target.closest("button,input,textarea,select,summary,details,a")) show(card.dataset.loc, `§${it.where}`);
    });
    card.querySelectorAll(".dbtn").forEach(b => b.onclick = () => {
      if (b.dataset.d === "correct") { correcting = it.key; items(); const i = $("corr-" + it.key); if (i) i.focus(); return; }
      correcting = null;
      act("review_decide", { borrower: D.borrower, key: it.key, decision: b.dataset.d, note: note() });
    });
    const save = card.querySelector("[data-save]");
    if (save) save.onclick = () => {
      if (it.fields && it.fields.length) {
        const changes = [];
        card.querySelectorAll("[data-path]").forEach(el => {
          const f = it.fields[Number(el.dataset.i)];
          if (String(el.value).trim() !== String(f.value)) changes.push({ path: f.path, value: el.value });
        });
        if (!changes.length) { say("Change at least one field, or Accept if the abstract is right."); return; }
        correcting = null;
        act("review_decide", { borrower: D.borrower, key: it.key, decision: "correct", note: note(), changes }, "Correction applied to the abstract.");
      } else {
        const v = ($("corr-" + it.key).value || "").trim();
        if (!v) { say("Describe the correction first."); return; }
        correcting = null;
        act("review_decide", { borrower: D.borrower, key: it.key, decision: "correct", note: note(), correction: v });
      }
    };
    const cancel = card.querySelector("[data-cancel]");
    if (cancel) cancel.onclick = () => { correcting = null; items(); };
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
      <div><dt>Stored as</dt><dd>${esc(d.effect)} · category <b>${esc(d.category)}</b></dd></div>
      <div><dt>Caps / limits</dt><dd>${d.caps.length ? d.caps.map(esc).join("; ") : "None stored. Check the clause for a dollar or % limit."}</dd></div>
      <div><dt>Number comes from</dt><dd>${esc(d.source)}</dd></div>
      <div><dt>Feeds</dt><dd>${d.affects.map(esc).join("; ") || "—"}</dd></div>
      ${rel ? `<div><dt>Shares wording with</dt><dd>${rel}</dd></div>` : ""}
    </dl>
    ${t ? `<details class="defn"><summary>Whole definition: ${esc(d.term)} (${t.rows.length} clauses, starts from ${esc(t.starts_from || "—")})</summary>
      <table><thead><tr><th>Clause</th><th>Effect</th><th>Category</th><th>Text</th></tr></thead><tbody>${rows}</tbody></table></details>` : ""}`;
}
// What GPT independently read for the claims behind this item (second-model check), when it has run.
function gptLine(it) {
  const g = it.gpt;
  if (!g || !g.total) return "";
  const bad = g.claims.filter(c => c.verdict !== "agree");
  const head = bad.length ? `GPT (independent check) disagrees on ${bad.length} of ${g.total}` : `GPT (independent check) agrees on ${g.total === 1 ? "this clause" : "all " + g.total + " related claims"}`;
  return `<div class="gpt ${bad.length ? "gpt-no" : "gpt-ok"}">${bad.length ? "⚠" : "✓"} ${esc(head)}${bad.map(c => `<div class="gpt-d">${esc(c.id)}: reads “${esc(c.value_in_text || c.verdict)}”${c.evidence ? ` — <q>${esc(c.evidence.slice(0, 160))}</q>` : ""}${c.evidence_verbatim ? "" : " <small>(quote not found verbatim)</small>"}</div>`).join("")}</div>`;
}

// Correct = the fields this item points at, pre-filled with what the abstract stores now. Saving applies the change.
function correctionForm(it, r) {
  if (!it.fields || !it.fields.length) {
    return `<div class="corr-box"><label class="corr-free">What should it say? <small>(no structured field for this item: recorded in the sign-off note, not applied)</small>
      <input id="corr-${it.key}" value="${esc(r.correction || "")}"></label>
      <div class="corr-actions"><button class="primary" data-save="1">Save correction</button><button class="dbtn" data-cancel="1">Cancel</button></div></div>`;
  }
  const groups = {};
  it.fields.forEach((f, i) => { (groups[f.group] = groups[f.group] || []).push([f, i]); });
  const input = (f, i) => f.type === "enum"
    ? `<select data-path data-i="${i}">${f.options.map(o => `<option value="${esc(o)}" ${o === f.value ? "selected" : ""}>${esc((f.labels || {})[o] || o)}</option>`).join("")}</select>`
    : `<input data-path data-i="${i}" type="${f.type === "date" ? "date" : "text"}" value="${esc(f.value)}">`;
  return `<div class="corr-box"><div class="corr-head">Change what the abstract stores. Only fields you change are saved; the second reviewer sees before → after.</div>
    ${Object.entries(groups).map(([g, fs]) => `<fieldset><legend>${esc(g)}</legend>${fs.map(([f, i]) => `<label><span>${esc(f.label)}</span>${input(f, i)}</label>`).join("")}</fieldset>`).join("")}
    <div class="corr-actions"><button class="primary" data-save="1">Save correction</button><button class="dbtn" data-cancel="1">Cancel</button></div></div>`;
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
  const chk = `<span class="checks"><span class="chk">quote found ✓</span><span class="chk">test date ✓</span><span class="chk">no overlap ✓</span></span>`;
  $("view-cov").innerHTML = `<div class="items">` + D.covs.map(c => {
    const rel = D.regs.flatMap(g => g.rows.filter(r => r.cov === c.id).map(r => ({ ...r, g })));
    const row = (r, band, cap) => `<tr class="row" data-loc="${r.loc}" data-cap="${esc(cap)}"><td class="d">${fmtD(r.from)} → ${fmtD(r.to)}</td><td class="v">${fmtV(c, r.value)}</td><td>${band}</td><td>${chk}</td></tr>`;
    const rows = c.rows.map(r => row(r, `<span class="band std">Standard</span>`, `§${c.ref} · standard schedule`)).join("") +
      rel.map(r => row(r, `<span class="band rel">${r.g.type === "relief_period" ? "Relief" : "Regime"}</span>`, `${r.g.name}`)).join("");
    const regs = D.regs.filter(g => g.applies_to.includes(c.id)).map(g => {
      const ex = g.early_exit && g.early_exit.allowed ? " Can end early at the borrower's election." : "";
      const su = g.type === "step_up" ? ` Step-up${g.increment ? " +" + g.increment + "x" : ""}${g.level ? " to " + g.level + "x" : ""}${g.ceiling ? ", ceiling " + g.ceiling + "x" : ""}${g.duration && g.duration.count ? `, ${g.duration.count} ${g.duration.unit}` : ""}${g.max_uses ? `, max ${g.max_uses} uses` : ""}.` : "";
      return `<div class="regime row" data-loc="reg:${g.id}" data-cap="${esc(g.name)}"><b>${esc(g.name)}</b><span>${g.type === "relief_period" ? "Relief period: replaces the standard schedule while it runs." : ""}${su}${ex}</span></div>`;
    }).join("");
    return `<article class="cov"><div class="cov-head row" data-loc="cov:${c.id}" data-cap="§${esc(c.ref)} ${esc(c.metric)}"><b>${esc(c.metric)}</b><span class="ref">§${esc(c.ref)}</span><span class="tag">${c.direction === "max" ? "Maximum" : "Minimum"} · tested ${c.frequency}${c.applies_during ? " · only during " + esc(c.applies_during) : ""}</span></div>
      ${chart(c)}<div class="tblwrap"><table><thead><tr><th>Test dates</th><th>Level</th><th>Schedule</th><th>Code checks</th></tr></thead><tbody>${rows}</tbody></table></div>${regs}</article>`;
  }).join("") + `<p class="note">Dashed purple = relief schedule. Every row passed the code checks shown; rows that fail a check become open items instead.</p></div>`;
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
