# MCP server + MCP App widget: working rules

Short rules for building MCP servers whose tools render a widget (MCP Apps `ui://` view) in ChatGPT, Claude and other
hosts. Each rule says where it comes from: **[probed]** = we tested it here; **[doc]** = a vendor doc says so (a doc
informs, a probe decides); **[open]** = not yet tested in a real host.

## Widget resource
- **Content-hash the widget URI.** Hosts cache a `ui://` resource by URI. Name it `ui://<app>/<view>-<sha256[:12]>.html`
  computed from the built HTML, so every new build is fetched fresh and an unchanged build keeps its URI.
  Use one constant for the resource and for every tool's `_meta.ui.resourceUri` and `_meta["openai/outputTemplate"]`.
  [probed: `mcp_server.view_uri`, test_view_uri_is_a_content_hash] [doc: sunpeak "versioned UI"; OpenAI forum says
  ChatGPT no longer snapshots HTML permanently, so the hash is belt and braces]
- **Keep old URIs readable.** Old conversations and published metadata still point at earlier URIs. We serve the
  pre-hash URI as an alias of the current HTML. [probed] [doc: sunpeak]
- Serve as `text/html;profile=mcp-app`, one self-contained file: bundle JS and CSS inline (esbuild), no external
  script URLs, no `data:` script src. Watch the size: ours is ~620 KB; hosts cap it (8 MB default in some). [probed] [doc]
- Build output is committed (`npm run build:ui`) so Python users need no Node. Rebuild before every commit that touches
  `ui-src/`; the hash changes by itself. [probed]

## Tools and data
- The model-facing tool (`open_review`) returns a short text summary plus the full screen payload in
  `structuredContent`. Keep `ui://` URIs and HTML out of the text the model reads. [probed] [doc: nanohub]
- **Decisions are app-only tools** (`_meta.ui.visibility = ["app"]`): the model never sees them, so a recorded decision
  is a person's click. Identity comes from the connection (auth principal / reviewer URL), never from a tool argument.
  Four-eyes rules live in code, not in the prompt. [probed: test_tools_and_who_decides]
- Model-callable decision tools ask the person with an MCP elicitation form; the model can only pre-fill. Headless
  clients cancel forms, so nothing is recorded. [probed] Which hosts render forms: [open]
- Long work returns a `job_id` at once and the model polls `run_status`; never block a tool call for minutes. [probed]
- Already-done work isn't refused: report "already on file" and continue the same flow (job finishes at once). [probed]

## Display mode and layout
- Declare `availableDisplayModes: ["inline","fullscreen"]` in `ui/initialize` (the `App` capabilities). [doc: ext-apps]
- For ChatGPT, also set `_meta["openai/ui"].availableDisplayModes` on the resource; `["fullscreen"]` opens it full
  screen with no inline state. [doc: OpenAI Apps SDK reference] Confirmed in ChatGPT: [open]
- Request full screen on load with the standard `ui/request-display-mode`; fall back to
  `window.openai.requestDisplayMode` if the host rejects it. If both fail, the inline layout must still work. [probed in
  local dev host] Does ChatGPT honour the standard request: [open]
- Lay out both modes: inline = stacked and height-capped; fullscreen = fill the frame from `containerDimensions`, with a
  bottom inset from `safeAreaInsets` (the chat composer overlays the frame). No `100vh` + `overflow:hidden` inline: it
  pins the iframe. Report size changes (`autoResize`). [probed] [doc]
- Follow `hostContext.theme`; fall back to `window.openai` globals only when the standard context lacks a field. [probed]

## Robustness
- Every pane has an empty state. Never render an `<img>` without a source (it shows as a broken image); handle
  `onerror`. Example: no agreement PDF on the server -> "pages aren't available" + the item's quote. [probed]
- Errors from app-only tools show in the widget as plain sentences, not stack traces. [probed]
- Gate `callServerTool` on the handshake; don't poll in render loops (hosts throttle the bridge). [doc: nanohub]
- Images from tools as `data:` URIs work in our local host; in ChatGPT: [open]. Declare any external domains in
  `_meta.ui.csp` (`connectDomains`, `resourceDomains`); empty lists are the secure default. [doc]

## Testing
1. In-memory MCP client tests (`mcp.Client`) for tools, visibility, identity and four eyes. [probed]
2. Local dev host (`--http --dev-host`, ext-apps `AppBridge`) to click through the widget headless with Playwright. [probed]
3. A real-host smoke test per host (ChatGPT, Claude) from the UAT script before claiming it works there.
Say which of these a claim rests on.
