// usage: node test.js <url> [shotPrefix]
const { chromium } = require('playwright-core');
const URL_ = process.argv[2] || 'http://127.0.0.1:18932/';
const PRE = process.argv[3] || 'local';
let fails = 0; const ok = (c, m) => { console.log((c ? 'PASS ' : 'FAIL ') + m); if (!c) fails++; };
const PLACES = {Glasgow: [55.8642, -4.2518], London: [51.5074, -0.1278], Parbold: [53.5920, -2.7708]};
(async () => {
  const b = await chromium.launch({executablePath: '/usr/bin/google-chrome', headless: true});
  // ---- mobile 390, simulated mid-range Android on 4G ----
  for (const [w, h, name] of [[390, 844, 'mobile'], [1280, 900, 'desktop']]) {
    const ctx = await b.newContext({viewport: {width: w, height: h}, deviceScaleFactor: name === 'mobile' ? 2 : 1, isMobile: name === 'mobile', hasTouch: name === 'mobile',
      permissions: ['geolocation'], geolocation: {latitude: PLACES.Glasgow[0], longitude: PLACES.Glasgow[1]}});
    const p = await ctx.newPage(); const errs = [], bad = [];
    p.on('console', m => { if (m.type() === 'error') errs.push(m.text()); }); p.on('pageerror', e => errs.push(e.message));
    p.on('response', r => { if (r.status() >= 400) bad.push(r.status() + ' ' + r.url()); });
    if (name === 'mobile') {
      const cdp = await ctx.newCDPSession(p);
      await cdp.send('Network.enable');
      await cdp.send('Network.emulateNetworkConditions', {offline: false, latency: 150, downloadThroughput: 9e6 / 8, uploadThroughput: 3e6 / 8}); // ~9 Mbps 4G
      await cdp.send('Emulation.setCPUThrottlingRate', {rate: 4}); // mid-range phone
    }
    const t0 = Date.now();
    await p.goto(URL_, {waitUntil: 'domcontentloaded'});
    await p.waitForFunction(() => window.__state, null, {timeout: 60000});
    const ready = Date.now() - t0;
    const perf = await p.evaluate(() => window.__perf);
    const dataRes = await p.evaluate(() => { const e = performance.getEntriesByType('resource').find(r => r.name.includes('data.json')); return e ? {transfer: e.transferSize, body: e.decodedBodySize, enc: e.encodedBodySize} : null; });
    console.log(`  ${name}: ready in ${ready} ms (data ${perf.dataMs} ms, first render ${perf.renderMs} ms), data.json`, JSON.stringify(dataRes));
    let s = await p.evaluate(() => window.__state);
    ok(s.count > 7000 && !s.place, `${name}: default = whole UK (${s.count} stations), no place`);
    ok(await p.isVisible('#prompt') && !(await p.isVisible('#topCard')), `${name}: prompt shown, no cheapest-5 before location`);
    if (name === 'mobile') await p.screenshot({path: `shots/${PRE}-mobile-default.png`});
    // Near me x3
    for (const [city, ll] of Object.entries(PLACES)) {
      await ctx.setGeolocation({latitude: ll[0], longitude: ll[1]});
      const seq = await p.evaluate(() => window.__placeSeq || 0);
      await p.click('#nearBtn');
      await p.waitForFunction(q => (window.__placeSeq || 0) > q, seq, {timeout: 10000});
      await p.waitForFunction(() => /your location\)/.test(window.__state.place || ''), null, {timeout: 8000}).catch(() => {});
      s = await p.evaluate(() => window.__state); const top = await p.evaluate(() => window.__top);
      ok(s.count > 0 && top.length === 5 && top.every(t => t.d <= 10), `${name}: Near me ${city}: ${s.count} within 10mi, place "${s.place}"; cheapest ${top[0].p}p ${top[0].brand} ${top[0].town} ${top[0].d}mi`);
      if (name === 'mobile' && city === 'Parbold') await p.screenshot({path: `shots/${PRE}-mobile-parbold.png`});
    }
    // fuel toggles
    for (const f of ['B7', 'E5', 'SDV', 'E10']) { await p.click(`.fuelbar button[data-f="${f}"]`); s = await p.evaluate(() => window.__state); ok(s.fuel === f && s.count > 0, `${name}: toggle ${f} (${s.count})`); }
    const tops = (await p.evaluate(() => window.__top)).map(t => t.p); ok(tops.every((v, i) => !i || v >= tops[i - 1]), `${name}: top5 ascending`);
    // radius
    await p.click('#radBtns button[data-r="5"]'); const c5 = (await p.evaluate(() => window.__state)).count;
    await p.click('#radBtns button[data-r="50"]'); const c50 = (await p.evaluate(() => window.__state)).count;
    ok(c5 < c50, `${name}: radius 5 (${c5}) < 50 (${c50})`);
    await p.click('#radBtns button[data-r="0"]'); const call = (await p.evaluate(() => window.__state)).count; ok(call > 7000, `${name}: All UK ${call}`);
    // pagination
    let rows = await p.$$eval('#tbl tbody tr', t => t.length); ok(rows === 100, `${name}: table paginated to 100 rows`);
    await p.click('#more'); rows = await p.$$eval('#tbl tbody tr', t => t.length); ok(rows === 200, `${name}: show more -> 200`);
    await p.click('#radBtns button[data-r="10"]');
    // sort
    await p.click('#tbl th[data-k="dist"]');
    const d = await p.$$eval('#tbl tbody tr td:nth-child(7)', t => t.map(x => parseFloat(x.textContent)));
    ok(d.length > 0 && d.every((v, i) => !i || v >= d[i - 1]), `${name}: sort by distance`);
    await p.click('#tbl th[data-k="price"]');
    // fresh filter
    const before = (await p.evaluate(() => window.__state)).count; await p.click('#freshOnly'); const after = (await p.evaluate(() => window.__state)).count; await p.click('#freshOnly');
    ok(after <= before && after > 0, `${name}: 7-day filter ${before} -> ${after}`);
    // postcode + town search
    await p.fill('#placeInput', 'EH1 1YZ'); await p.click('#placeGo'); await p.waitForFunction(() => /EH1/.test(window.__state.place || ''), null, {timeout: 10000}).catch(() => {});
    s = await p.evaluate(() => window.__state); let top = await p.evaluate(() => window.__top);
    ok(/EH1 1YZ/.test(s.place) && top[0] && top[0].region === 'Scotland', `${name}: postcode EH1 1YZ -> ${s.place}, cheapest ${top[0] && top[0].p}p ${top[0] && top[0].town}`);
    await p.fill('#placeInput', 'Cardiff'); await p.click('#placeGo'); await p.waitForFunction(() => /Cardiff/i.test(window.__state.place || ''), null, {timeout: 10000}).catch(() => {});
    s = await p.evaluate(() => window.__state); top = await p.evaluate(() => window.__top);
    ok(/Cardiff/i.test(s.place) && top[0] && top[0].region === 'Wales', `${name}: town Cardiff -> ${s.place}, cheapest ${top[0] && top[0].p}p`);
    await p.fill('#placeInput', 'BT1 5GS'); await p.click('#placeGo'); await p.waitForFunction(() => /BT1/.test(window.__state.place || ''), null, {timeout: 10000}).catch(() => {});
    top = await p.evaluate(() => window.__top); ok(top[0] && top[0].region === 'Northern Ireland', `${name}: Belfast BT1 5GS cheapest ${top[0] && top[0].p}p ${top[0] && top[0].town}`);
    // search filter
    await p.click('#radBtns button[data-r="0"]');
    await p.fill('#search', 'tesco'); await p.waitForTimeout(500);
    const br = await p.$$eval('#tbl tbody tr td:nth-child(2)', t => [...new Set(t.map(x => x.textContent))]); ok(br.length === 1 && br[0] === 'Tesco', `${name}: search tesco -> ${br}`);
    await p.fill('#search', ''); await p.waitForTimeout(400);
    // region table
    const regs = await p.$$eval('#regTable tr', t => t.length); ok(regs >= 14, `${name}: region table rows ${regs}`);
    await p.click('#areaBox summary'); await p.waitForTimeout(500); const areas = await p.$$eval('#areaTable tr', t => t.length); ok(areas > 100, `${name}: postcode area table rows ${areas}`);
    // map
    await p.click('#radBtns button[data-r="10"]');
    await ctx.setGeolocation({latitude: PLACES.Parbold[0], longitude: PLACES.Parbold[1]}); await p.click('#nearBtn'); await p.waitForFunction(() => /WN8/.test(window.__state.place || ''), null, {timeout: 10000}).catch(() => {});
    await p.locator('#mapCard').scrollIntoViewIfNeeded(); await p.waitForTimeout(4000);
    const mk = await p.evaluate(() => window.__mapMarkers); ok(mk > 0 && await p.$('.leaflet-tile-loaded') !== null, `${name}: map drawn with ${mk} markers`);
    await p.click('#top5 li'); await p.waitForTimeout(2500); ok(await p.$('.leaflet-popup') !== null, `${name}: tapping a cheapest station opens its map popup`);
    await p.screenshot({path: `shots/${PRE}-${name}-map.png`});
    // saved place after reload
    await p.reload({waitUntil: 'domcontentloaded'}); await p.waitForFunction(() => window.__state, null, {timeout: 60000});
    s = await p.evaluate(() => window.__state); ok(!!s.place, `${name}: last location remembered after reload ("${s.place}")`);
    await p.evaluate(() => window.scrollTo(0, 0)); await p.waitForTimeout(300);
    await p.screenshot({path: `shots/${PRE === 'live' ? '' : PRE + '-'}${name}${PRE === 'live' ? '-live' : ''}.png`});
    if (name === 'mobile') { const sw = await p.evaluate(() => document.documentElement.scrollWidth); ok(sw <= 392, `mobile: no sideways scroll (${sw})`); }
    ok(errs.length === 0, `${name}: no console errors ${errs.join(' | ')}`);
    ok(bad.length === 0, `${name}: no failed requests ${bad.join(' | ')}`);
    await ctx.close();
  }
  await b.close(); console.log(fails ? `${fails} FAILED` : 'ALL PASSED'); process.exit(fails ? 1 : 0);
})();
