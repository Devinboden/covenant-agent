// Screenshot the review widget on the local test host, the way ChatGPT shows it (full screen), for fast UI checks.
//   python -m covenant_engine.mcp_server --http --dev-host --port 8765 &
//   NODE_PATH=$(npm root -g) node scripts/ui_preview.cjs CARS [items|cov] [out.png] [--type "<card text>" "<value>"]
const { chromium } = require('playwright');
(async () => {
  const [deal = 'CARS', tab = 'items', out = `${deal}-${tab}.png`] = process.argv.slice(2).filter(a => !a.startsWith('--'));
  const ti = process.argv.indexOf('--type');
  const b = await chromium.launch(); const pg = await b.newPage({ viewport: { width: 1600, height: 1100 } });
  pg.on('console', m => { if (m.type() === 'error') console.log('page error:', m.text()); });
  await pg.goto(process.env.HOST || 'http://localhost:8765/');
  await pg.waitForFunction(() => document.querySelectorAll('#deal option').length > 0, null, { timeout: 30000 });
  await pg.selectOption('#deal', deal); await pg.click('#open'); await pg.waitForTimeout(3000);
  const f = pg.frames().find(x => x !== pg.mainFrame());
  await f.waitForSelector('.item', { state: 'attached' });
  await pg.addStyleTag({ content: '#stage{max-width:1400px!important} #stage iframe{height:1000px!important} #stage::after{display:none!important}' });
  await f.evaluate(() => { document.body.classList.remove('inline'); document.body.classList.add('fullscreen'); document.documentElement.style.setProperty('--frame-h', '1000px'); });
  if (tab === 'cov') await f.click('#tab-cov');
  if (ti > 0) {                                   // type a value into the first inline box of the card containing <card text>
    const card = f.locator('.item', { hasText: process.argv[ti + 1] }).first();
    await card.scrollIntoViewIfNeeded();
    await card.locator('[data-edit]').last().click();            // values read as text until clicked
    await card.locator('input.inl').last().pressSequentially(process.argv[ti + 2], { delay: 20 });
  }
  await pg.waitForTimeout(1500);
  await pg.screenshot({ path: out });
  console.log(out);
  await b.close();
})();
