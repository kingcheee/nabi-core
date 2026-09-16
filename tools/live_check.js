#!/usr/bin/env node
// 체험 화면 검수 — 배포된 사이트(또는 로컬)의 /try 가 인스턴스에 붙는지, 붙으면 승인·다시 재기가 도는지.
//   NODE_PATH=<playwright 가 있는 node_modules> node tools/live_check.js https://kingcheee.github.io/nabi-core/ [--act] [--expect-api <주소/>]
//   --act        전부 승인 → 새로고침 뒤 유지 · 다른 방문자 격리 · 스캔001.pdf 다시 재기(SSE, 최대 2분) 까지 실제로 돌린다
//   --expect-api 화면이 고른 인스턴스 주소가 이것이어야 통과(window.nabiApiBase)
// 종료 코드 0 = 통과. 인스턴스가 없으면 「배너 있음 · 읽기 전용」을 찍고 1 로 끝난다(링크 자체는 산 것).
const { chromium } = require('playwright');
const args = process.argv.slice(2);
const SITE = (args.find(a => !a.startsWith('--')) || 'http://127.0.0.1:8098/').replace(/\/?$/, '/');
const ACT = args.includes('--act');
const EXPECT = args.includes('--expect-api') ? args[args.indexOf('--expect-api') + 1] : null;
(async () => {
  const b = await chromium.launch();
  const errs = [];
  const mk = async () => { const c = await b.newContext({ viewport: { width: 1600, height: 900 }, locale: 'ko-KR' }); const p = await c.newPage();
    p.on('pageerror', e => errs.push('pageerror: ' + e.message)); return [c, p]; };
  const [c1, p] = await mk();
  await p.goto(SITE + 'try/#/inbox', { waitUntil: 'networkidle', timeout: 60000 });
  await p.waitForSelector('tr.row', { timeout: 30000 });
  const hidden = await p.$eval('#banner', e => e.hidden);
  const api = await p.evaluate(() => window.nabiApiBase);
  console.log('인스턴스:', api || '없음(읽기 전용)', '| 배너:', hidden ? '없음' : '있음', '| 행:', await p.$$eval('tr.row', r => r.length));
  let ok = hidden;
  if (EXPECT && api !== EXPECT) { console.log('기대한 인스턴스가 아니다:', EXPECT); ok = false; }
  if (ok && ACT) {
    await p.goto(SITE + 'try/#/queue'); await p.waitForSelector('#approve-all'); await p.click('#approve-all'); await p.waitForTimeout(1500);
    await p.reload({ waitUntil: 'networkidle' }); await p.goto(SITE + 'try/#/inbox'); await p.waitForSelector('tr.row');
    const kept = /이동완료\s*7/.test(await p.textContent('body')); console.log('전부 승인 → 새로고침 뒤 유지(이동완료 7):', kept); ok = ok && kept;
    const [c2, q] = await mk(); await q.goto(SITE + 'try/#/inbox', { waitUntil: 'networkidle' }); await q.waitForSelector('tr.row');
    const iso = /이동완료\s*0/.test(await q.textContent('body')); console.log('다른 방문자 격리(이동완료 0):', iso); ok = ok && iso; await c2.close();
    await p.click('tr.row:has-text("스캔001.pdf")'); await p.waitForSelector('body.drawer-open'); await p.click('.drawer button:has-text("다시 재기")');
    await p.waitForFunction(() => /다시 쟀습니다|다시 재지 못했습니다|연결이 끊겼습니다/.test((document.querySelector('#progress') || {}).textContent || ''), null, { timeout: 120000 });
    const res = (await p.textContent('#progress')).trim(); console.log('다시 재기:', res); ok = ok && /다시 쟀습니다/.test(res);
  }
  console.log('페이지 오류:', errs.length ? errs : '없음'); ok = ok && !errs.length;
  await b.close();
  console.log(ok ? '통과' : '실패'); process.exit(ok ? 0 : 1);
})().catch(e => { console.error('실패', e.message); process.exit(1); });
