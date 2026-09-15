#!/usr/bin/env node
// 심사용 스크린샷 5장(1600×900, 16:9) — 체험 인스턴스에 새 방문자로 붙어 상태를 만든 뒤 찍는다.
//
//   python3 -m web --demo                      # 8098 에 떠 있어야 한다 (모델 서버는 없어도 된다)
//   NODE_PATH=<playwright 가 있는 node_modules> node tools/site_shots.js [출력폴더] [BASE]
//   예) NODE_PATH=~/workspace/03-agents/naver-agent/node_modules node tools/site_shots.js \
//         ~/projects/02-hackathon/2026-원티드-AI-챔피언십/hackathon/2026-09-11-사이트
//
// Playwright(크로미엄)만 쓴다 — 저장소에 의존성을 두지 않으려고 NODE_PATH 로 빌린다(`npm i playwright` 해도 된다).
// 방문자별 샌드박스라 여기서 승인한 것은 다른 방문자에게 보이지 않는다.
const { chromium } = require('playwright');
const fs = require('fs');

const OUT = process.argv[2] || 'shots';
const BASE = process.argv[3] || 'http://127.0.0.1:8098';
fs.mkdirSync(OUT, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 1, locale: 'ko-KR' });
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
  const shot = async (name) => { await page.waitForTimeout(350); await page.screenshot({ path: `${OUT}/${name}.png` }); console.log('저장', `${OUT}/${name}.png`); };
  const noToast = () => page.evaluate(() => document.querySelectorAll('.toast').forEach(t => t.remove()));

  // 1) 랜딩
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  await shot('01-landing');

  // 2) 받은 서류함 + 상세 드로어 (보류 건)
  await page.goto(BASE + '/try/#/inbox', { waitUntil: 'networkidle' });
  await page.waitForSelector('tr.row');
  await page.click('tr.row:has-text("가상은행_거래내역.xlsx")');
  await page.waitForSelector('body.drawer-open');
  await shot('02-inbox-drawer');

  // 상태 만들기 — 확정 전부 승인 + 보류 하나를 고쳐서 승인
  await page.goto(BASE + '/try/#/queue');
  await page.waitForSelector('#approve-all');
  await page.click('#approve-all');
  await page.waitForTimeout(1500);
  await page.goto(BASE + '/try/#/inbox');
  await page.waitForSelector('tr.row');
  await page.click('tr.row:has-text("임대차계약서(수정본).docx")');
  await page.waitForSelector('body.drawer-open');
  await page.click('.drawer [data-act="fix"]');
  await page.fill('.drawer .fix input[name="period"]', 'PERMANENT');
  await page.click('.drawer .fix button[type="submit"]');
  await page.waitForTimeout(800);
  await page.keyboard.press('Escape');

  // 3) 확인 큐 — 인라인 수정 폼이 열린 상태
  await page.goto(BASE + '/try/#/queue');
  await page.waitForSelector('.qcard');
  await noToast();
  await page.click('.qcard:has-text("스캔002.pdf") [data-act="fix"]');
  await page.waitForSelector('.qcard .fix.open');
  await shot('03-queue');

  // 4) 정리된 서류
  await page.goto(BASE + '/try/#/organized');
  await page.waitForSelector('.tree .file');
  await noToast();
  await shot('04-organized');

  // 5) 대시보드 (활동 있음)
  await page.goto(BASE + '/try/#/dashboard');
  await page.waitForSelector('.feed li');
  await noToast();
  await shot('05-dashboard');

  console.log('콘솔 오류:', errors.length ? errors : '없음');
  await browser.close();
})().catch(e => { console.error('실패', e); process.exit(1); });
