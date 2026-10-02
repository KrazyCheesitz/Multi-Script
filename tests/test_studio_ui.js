// SPDX-License-Identifier: GPL-3.0-or-later
// Studio UI regression (real Chromium, real content-script chain, mock Notion page):
//  - the chat bar lines up with the Notion composer's text column
//  - settings are organised into Studio / Agent / Notion / Engines tabs
//  - the Notion tab detects models, shows locked ones, selects a model + effort in
//    Notion's own picker, leaves the picker closed and our menu open
//  - the trial estimator turns typed readings into a rough "prompts left"
//  - Engines → Bridge buttons drive the same host calls as the terminal icon
const h = require('../tools/studio_harness');
let passed = 0;
const ok = (c, m) => { if (!c) throw new Error('FAIL ' + m); passed++; console.log('PASS ' + m); };

(async () => {
  const x = await h.open({ bridge: 'up', host: 'installed', theme: 'dark' });
  if (!x) { console.log('SKIP studio UI (no browser available)'); return; }
  const { page } = x;
  try {
    // ── bar ↔ composer alignment ────────────────────────────────────────────
    const geo = await page.evaluate(() => {
      const r = (s) => { const e = document.querySelector(s); const b = e && e.getBoundingClientRect(); return b && { l: b.left, r: b.right, t: b.top, b: b.bottom, w: b.width }; };
      return { bar: r('#zs-bar'), comp: r('.composer'), ed: r('#agent-editor'), dot: r('#zs-dot'), last: r('#zs-bridge-run'), send: r('#send') };
    });
    ok(Math.abs(geo.bar.l - geo.comp.l) <= 1 && Math.abs(geo.bar.w - geo.comp.w) <= 2, 'bar spans exactly the composer card');
    ok(Math.abs(geo.dot.l - geo.ed.l) <= 2, `brand dot lines up with the composer text column (${Math.round(geo.dot.l)} vs ${Math.round(geo.ed.l)})`);
    ok(geo.last.r <= geo.ed.r + 1, 'last bar control stays inside the text column on the right');
    ok(geo.bar.b <= geo.ed.t + 2, 'bar sits above the editor, not over it');

    // resize: the composer narrows, the bar follows and compacts
    await page.setViewportSize({ width: 760, height: 800 });
    await page.evaluate(() => { document.querySelector('.composer').style.width = '480px'; });
    await page.waitForTimeout(300);
    const nar = await page.evaluate(() => { const b = document.querySelector('#zs-bar'), c = document.querySelector('.composer'); const bb = b.getBoundingClientRect(), cc = c.getBoundingClientRect(); const kids = [...b.children].filter((k) => k.offsetWidth); const maxR = Math.max(...kids.map((k) => k.getBoundingClientRect().right)); return { dl: Math.abs(bb.left - cc.left), dw: Math.abs(bb.width - cc.width), compact: b.classList.contains('zs-bar-compact'), over: maxR - cc.right }; });
    ok(nar.dl <= 1 && nar.dw <= 2, 'bar re-docks when the composer is resized');
    ok(nar.compact && nar.over <= 1, 'compact mode keeps every control inside a 480px composer');
    await page.setViewportSize({ width: 1280, height: 800 });
    await page.evaluate(() => { document.querySelector('.composer').style.width = '720px'; });
    await page.waitForTimeout(300);

    // ── settings tabs ───────────────────────────────────────────────────────
    await page.click('#zs-model'); await page.waitForTimeout(300);
    const tabs = await page.evaluate(() => [...document.querySelectorAll('.zs-menu-tabs button')].map((b) => b.dataset.tab));
    ok(['setup', 'appearance', 'studio', 'agent', 'notion', 'interface', 'engines', 'sites', 'help'].every((t) => tabs.includes(t)), 'tabs: ' + tabs.join(','));
    const labelsOn = async (tab) => page.evaluate((t) => { document.querySelector(`.zs-menu-tabs button[data-tab="${t}"]`).click(); return [...document.querySelectorAll('#zs-menu [data-zs-tab]')].filter((e) => !e.hidden).map((e) => (e.querySelector('.zs-sec-label span') || {}).textContent || '').join(' | '); }, tab);
    const studio = await labelsOn('studio'), agent = await labelsOn('agent'), engines = await labelsOn('engines');
    ok(/Execution effort/.test(studio) && /Prompt rewriting|Creative output|Studio skill coverage/.test(studio) && /Usage optimizer/.test(studio), 'Studio tab holds effort, prompt rewriting, creative output and usage');
    ok(/Custom instructions/.test(agent) && !/Execution effort/.test(agent), 'Agent tab keeps autonomy/pacing/custom instructions only');
    ok(/Bridge/.test(engines) && /Engines & MCP servers/.test(engines), 'Engines tab leads with the Bridge section');

    // a control on the Studio tab must not bounce the menu back to Agent
    await page.evaluate(() => document.querySelector('.zs-menu-tabs button[data-tab="studio"]').click());
    await page.evaluate(() => document.querySelector('[data-effort="deep"]').click());
    await page.waitForTimeout(150);
    ok(await page.evaluate(() => document.querySelector('.zs-menu-tabs .active').dataset.tab) === 'studio', 'changing a Studio setting keeps you on the Studio tab');

    // ── Notion tab: detection + selection ───────────────────────────────────
    await page.evaluate(() => document.querySelector('.zs-menu-tabs button[data-tab="notion"]').click());
    await page.click('#zs-nm-detect'); await page.waitForTimeout(2600);
    const det = await page.evaluate(() => ({ st: document.querySelector('#zs-nm-status').textContent, rows: [...document.querySelectorAll('.zs-nm-model')].map((b) => ({ n: b.dataset.nmModel, off: b.disabled })), eff: [...document.querySelectorAll('.zs-nm-effort')].map((b) => b.dataset.nmEffort), flags: [...document.querySelectorAll('.zs-nm-flag')].map((f) => f.textContent), picker: window.__mock.open, menuOpen: !document.querySelector('#zs-menu').hidden }));
    ok(det.rows.some((r) => r.n === 'Claude Opus 5.5' && !r.off) && det.rows.some((r) => r.n === 'Claude Opus 5' && !r.off), 'detects Opus 5.5 and Opus 5 as separate enabled models');
    ok(det.rows.some((r) => /GPT-5.6/.test(r.n) && r.off), 'a model Notion marks Upgrade/locked is listed but disabled');
    ok(det.rows.some((r) => r.n === 'Kimi K3'), 'models behind "More models" are expanded and read');
    ok(det.eff.join() === 'low,medium,high,max', 'effort levels come from the picker: ' + det.eff.join());
    ok(!det.picker && det.menuOpen, "Notion's picker is closed again and our menu stayed open");
    ok(det.flags.some((f) => /Opus 5.5 enabled/.test(f)), 'Opus 5.5 flag shows enabled');

    await page.click('.zs-nm-model[data-nm-model="Claude Opus 5.5"]'); await page.waitForTimeout(1500);
    let mock = await page.evaluate(() => ({ cur: window.__mock.cur, st: document.querySelector('#zs-nm-status').textContent, open: window.__mock.open }));
    ok(mock.cur === 'Claude Opus 5.5' && !mock.open, 'selecting Opus 5.5 clicks the right row (not Opus 5) and closes the picker');
    ok(/Notion now shows/.test(mock.st), 'result is verified against the picker label: ' + mock.st.slice(0, 70));
    await page.click('.zs-nm-effort[data-nm-effort="high"]'); await page.waitForTimeout(1500);
    mock = await page.evaluate(() => ({ eff: window.__mockEffort, cur: window.__mock.cur }));
    ok(mock.eff === 'High' && mock.cur === 'Claude Opus 5.5', 'effort High is applied without changing the model');
    const chip = await page.evaluate(() => document.querySelector('#zs-model-text').textContent);
    ok(/Opus 5\.5/.test(chip) && /High/.test(chip), 'bar chip shows model · effort: ' + chip);

    // ── trial estimator ─────────────────────────────────────────────────────
    await page.fill('#zs-nu-days', '5'); await page.fill('#zs-nu-credits', '1000'); await page.click('#zs-nu-save'); await page.waitForTimeout(200);
    const sum = await page.evaluate(() => document.querySelector('#zs-nu-summary').textContent);
    ok(/~\d+ prompts? left/.test(sum), 'estimate appears after entering credits + trial days: ' + sum);
    const chip2 = await page.evaluate(() => document.querySelector('#zs-model-text').textContent);
    ok(/left/.test(chip2), 'chip carries the estimate: ' + chip2);
    await page.click('#zs-nu-scan'); await page.waitForTimeout(150);
    ok(/Read from the page/.test(await page.evaluate(() => document.querySelector('#zs-nu-status').textContent)), 'reads "Trial ends in 6 days" from the page');

    // ── Bridge section ──────────────────────────────────────────────────────
    await page.evaluate(() => document.querySelector('.zs-menu-tabs button[data-tab="engines"]').click());
    await page.click('#zs-br-restart'); await page.waitForTimeout(300);
    ok((await page.evaluate(() => window.__ms.calls)).includes('host_restart'), 'Engines → Restart calls the native host');
    ok(/Running/.test(await page.evaluate(() => document.querySelector('#zs-br-status').textContent)), 'Bridge section reports the result');

    // ── Radix-style picker (opens on pointerdown, rows act on pointerup) ─────
    const rx = await page.evaluate(async () => {
      const old = document.getElementById('model'); const nb = old.cloneNode(true); old.replaceWith(nb);
      let menu = null;
      nb.addEventListener('pointerdown', () => {
        if (menu) { menu.remove(); menu = null; return; }
        menu = document.createElement('div'); menu.setAttribute('role', 'menu'); menu.style.cssText = 'position:fixed;left:300px;bottom:130px;width:240px;background:#333;z-index:99999';
        for (const t of ['Auto', 'Claude Opus 5.5', 'Claude Opus 5']) { const r = document.createElement('div'); r.setAttribute('role', 'menuitem'); r.textContent = t; r.style.padding = '6px'; r.addEventListener('pointerup', () => { nb.textContent = t; menu.remove(); menu = null; }); menu.appendChild(r); }
        document.body.appendChild(menu);
      });
      document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && menu) { menu.remove(); menu = null; } });
      const d = await ZSProvider.detectModels(); const s = await ZSProvider.selectModel({ model: 'opus 5' });
      return { n: d.models.length, picked: s.picked, label: s.label, ok: s.ok, open: !!menu };
    });
    ok(rx.n === 3 && rx.ok && rx.picked === 'Claude Opus 5' && !rx.open, 'works with a pointer-event (Radix-style) picker too: ' + JSON.stringify(rx));

    ok(x.errors.length === 0, 'no page errors: ' + x.errors.join(' | ').slice(0, 200));
  } finally { await x.close(); }
  // ── theme coherence: Mono/Auto follow the page's light/dark mode ─────────────
  const lt = await h.open({ bridge: 'up', host: 'installed', theme: 'light' });
  try {
    const th = await lt.page.evaluate(() => ({ t: document.querySelector('#zs-root').dataset.msTheme, light: document.documentElement.classList.contains('zs-light') }));
    ok(th.light && th.t === 'frost', 'light Notion + default Mono theme renders the light (Frost) menu, not light text on a dark panel');
    ok(lt.errors.length === 0, 'light theme: no page errors');
  } finally { await lt.close(); }
  const dk = await h.open({ bridge: 'up', host: 'installed', theme: 'dark' });
  try {
    const th = await dk.page.evaluate(() => ({ t: document.querySelector('#zs-root').dataset.msTheme, light: document.documentElement.classList.contains('zs-light') }));
    ok(!th.light && th.t === 'mono', 'dark Notion keeps the Mono menu');
  } finally { await dk.close(); }
  console.log(`PASS studio UI (${passed} checks)`);
})().catch((e) => { console.error(e.message || e); process.exit(1); });
