// Build the MCP App view and the local test host into src/covenant_engine/ui/ (committed, so Python users need no Node).
//   npm install && npm run build:ui
import { build } from "esbuild";
import { readFileSync, writeFileSync } from "node:fs";

const out = "src/covenant_engine/ui";
const bundle = async entry => (await build({ entryPoints: [entry], bundle: true, format: "iife", minify: true, write: false,
  target: "es2020", platform: "browser", legalComments: "none" })).outputFiles[0].text;

const css = readFileSync("ui-src/review.css", "utf8");
const body = readFileSync("ui-src/review.body.html", "utf8");
const js = (await bundle("ui-src/review.js")).replaceAll("</script", "<\\/script");
const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark">
<title>Covenant review</title>
${css}
</head>
<body>
${body}
<script>${js}</script>
</body>
</html>
`;
writeFileSync(`${out}/review.html`, html);
writeFileSync(`${out}/dev_host.js`, await bundle("ui-src/dev_host.js"));
console.log(`review.html ${(html.length / 1024).toFixed(0)} KB, dev_host.js built`);
