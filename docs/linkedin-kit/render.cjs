const { chromium } = require('playwright');
(async () => {
  const [src, out, w, h] = process.argv.slice(2);
  const b = await chromium.launch(); const pg = await b.newPage({ viewport: { width: +w, height: +h }, deviceScaleFactor: 2 });
  await pg.goto('file://' + src); await pg.waitForTimeout(500);
  const over = await pg.evaluate(() => document.body.scrollHeight > window.innerHeight || document.body.scrollWidth > window.innerWidth);
  console.log('overflow:', over, await pg.evaluate(() => document.body.scrollHeight));
  await pg.screenshot({ path: out }); await b.close();
})();
