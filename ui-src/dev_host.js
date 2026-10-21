// Local MCP Apps test host: connects to this server's /mcp, calls open_review, and renders the review view in a
// sandboxed iframe through the ext-apps AppBridge. It logs every tool call the view makes, so you can see that
// decisions go through app-only tools. For local testing only.
import { Client, StreamableHTTPClientTransport } from "@modelcontextprotocol/client";
import { AppBridge, PostMessageTransport } from "@modelcontextprotocol/ext-apps/app-bridge";

const $ = id => document.getElementById(id);
const log = (t, cls = "") => { const d = document.createElement("div"); d.className = cls; d.textContent = `${new Date().toLocaleTimeString()}  ${t}`; $("log").prepend(d); };
const INFO = { name: "covenant-dev-host", version: "0.1.0" };
let client, tools = [], bridge = null, mode = "inline";

const theme = () => $("theme").value === "system" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : $("theme").value;
const dims = () => { const r = $("stage").getBoundingClientRect(); return mode === "fullscreen" ? { width: Math.round(r.width), height: Math.round(r.height) } : { width: Math.round(r.width), maxHeight: 900 }; };
const ctx = () => ({ theme: theme(), displayMode: mode, availableDisplayModes: ["inline", "fullscreen"], containerDimensions: dims(),
  safeAreaInsets: { top: 0, right: 0, bottom: mode === "fullscreen" ? 64 : 0, left: 0 }, platform: "web", locale: navigator.language });

function setMode(m) {
  mode = m;
  document.body.dataset.mode = m;
  $("mode").textContent = m === "fullscreen" ? "Exit full screen" : "Full screen";
  if (bridge) bridge.setHostContext(ctx());
}

async function start() {
  client = new Client(INFO);
  await client.connect(new StreamableHTTPClientTransport(new URL("/mcp", location.href)));
  const v = client.getServerVersion();
  $("server").textContent = `${v.name} ${v.version}`;
  tools = (await client.listTools()).tools;
  $("tools").innerHTML = tools.map(t => { const vis = (t._meta && t._meta.ui && t._meta.ui.visibility) || ["model", "app"];
    return `<li><code>${t.name}</code> <span class="${vis.includes("model") ? "m" : "a"}">${vis.includes("model") ? "model" + (vis.includes("app") && t._meta && t._meta.ui ? " + screen" : "") : "screen only"}</span></li>`; }).join("");
  const deals = (await client.callTool({ name: "list_deals", arguments: {} })).structuredContent.deals;
  $("deal").innerHTML = deals.map(d => `<option value="${d.borrower}">${d.borrower} · ${d.status} · ${d.open} open</option>`).join("");
  $("deal").value = deals.some(d => d.borrower === "MEC") ? "MEC" : deals[0].borrower;
  log("connected; " + tools.length + " tools");
}

async function openReview() {
  const borrower = $("deal").value;
  const tool = tools.find(t => t.name === "open_review");
  const uri = tool._meta.ui.resourceUri;
  log(`model → open_review(${borrower})`, "m");
  const result = await client.callTool({ name: "open_review", arguments: { borrower } });
  const html = (await client.readResource({ uri })).contents[0].text;
  if (bridge) { try { await bridge.teardownResource({}); } catch (e) {} bridge = null; }
  const old = $("frame"); if (old) old.remove();
  const f = document.createElement("iframe");
  f.id = "frame"; f.title = "Covenant review"; f.setAttribute("sandbox", "allow-scripts");
  $("stage").appendChild(f);
  // client = null: this host answers the view's requests itself (below), so every call is checked and logged
  bridge = new AppBridge(null, INFO, { serverTools: {}, openLinks: {}, logging: {} }, { hostContext: ctx() });
  bridge.oninitialized = () => { log("view initialized"); bridge.sendToolInput({ arguments: { borrower } }); bridge.sendToolResult(result); };
  bridge.oncalltool = async (params) => {
    const app = tools.find(t => t.name === params.name);
    const vis = (app && app._meta && app._meta.ui && app._meta.ui.visibility) || ["model", "app"];
    if (!vis.includes("app")) { log(`blocked: view tried ${params.name} (not available to the screen)`, "err"); throw new Error("tool not available to the app"); }
    const shown = params.name === "review_page" ? `page ${params.arguments.page}` : JSON.stringify(params.arguments);
    log(`screen → ${params.name}(${shown})`, "a");
    const r = await client.callTool(params);
    if (r.isError) log(`  refused: ${(r.content || []).map(c => c.text).join(" ")}`, "err");
    return r;
  };
  bridge.onrequestdisplaymode = async ({ mode: m }) => { setMode(m); log(`screen asked for ${m}`); return { mode: m }; };
  bridge.onsizechange = ({ height }) => { if (mode === "inline" && height) f.style.height = Math.min(height, 900) + "px"; };
  bridge.onmessage = async ({ content }) => { log("screen → chat: " + JSON.stringify(content).slice(0, 120)); return {}; };
  await bridge.connect(new PostMessageTransport(f.contentWindow, f.contentWindow));
  f.srcdoc = html;
}

$("open").onclick = () => openReview().catch(e => log("error: " + e.message, "err"));
$("mode").onclick = () => setMode(mode === "fullscreen" ? "inline" : "fullscreen");
$("theme").onchange = () => { document.documentElement.dataset.theme = theme(); if (bridge) bridge.setHostContext(ctx()); };
addEventListener("resize", () => { if (bridge) bridge.setHostContext(ctx()); });
start().then(openReview).catch(e => log("error: " + e.message, "err"));
