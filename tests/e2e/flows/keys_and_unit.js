/**
 * E2E 剧本：密钥保存/测试/清除 + 单元七态（INIT→PICK→LEARN→PRODUCE→VERIFY→JUDGE→INTEGRATE→CLOSED）。
 *
 * 参数：argv[2]=base url，argv[3]=本次项目名。
 * 输出契约同 project_lifecycle.js：PASS/FAIL/SUMMARY，退出码 0 = 全通过。
 */
const { chromium } = require('playwright');

const BASE = process.argv[2];
const NAME = process.argv[3] || 'E2E七态项目';
const fails = [];

function log(ok, label, detail) {
  const line = (ok ? 'PASS' : 'FAIL') + ' | ' + label + (detail ? ' | ' + detail : '');
  console.log(line);
  if (!ok) fails.push(label + ' :: ' + (detail || ''));
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await ctx.newPage();
  const consoleErrors = [];
  page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
  page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + e.message));
  page.on('dialog', (d) => d.accept());

  async function bodyHas(text, timeout = 8000) {
    await page.waitForFunction(
      (t) => document.body.innerText.includes(t), text, { timeout }
    );
  }

  async function move(value, expect) {
    try {
      await page.click(`button[name="nxt"][value="${value}"]`);
      if (expect) { await bodyHas(expect); }
      await page.waitForTimeout(250);
    } catch (e) {
      const body = await page.locator('body').innerText().catch(() => '');
      console.log('DEBUG move=' + value + ' url=' + page.url() + ' body=' + body.replace(/\s+/g, ' ').slice(0, 300));
      throw e;
    }
  }

  // 1. 建项目（成功后自动切到 /unit）
  await page.goto(BASE + '/projects/new', { waitUntil: 'networkidle' });
  await page.fill('input[name="name"]', NAME);
  await page.fill('textarea[name="goal"]', 'E2E：密钥与单元七态');
  await page.click('#create-form button[type="submit"]');
  await page.waitForTimeout(1800);
  log(!page.url().includes('/projects/new'), '创建项目后进入 /unit', page.url());

  // 2. 建单元
  await page.fill('input[name="title"]', 'E2E七态单元');
  await page.click('form[hx-post="/api/v1/units"] button[type="submit"]');
  await page.waitForTimeout(1200);
  await page.locator('#unit-list a', { hasText: 'E2E七态单元' }).first().click();
  await page.waitForLoadState('networkidle');
  console.log('DEBUG detail url=' + page.url());
  console.log('DEBUG panel=' + (await page.locator('#wf-panel').innerText().catch(() => 'NO PANEL')).replace(/\s+/g, ' ').slice(0, 200));

  // 3. 七态推进
  await move('PICK', '选题中');
  await move('LEARN', '学习中');
  await move('PRODUCE', '产出中');

  await page.fill('textarea[name="production"]', 'E2E 产出：跑通一个最小脚本并记录结果。');
  await page.click('form[hx-post$="/production"] button[type="submit"]');
  await bodyHas('产出已保存');

  await move('VERIFY', '验证中');
  await move('JUDGE', '评判中');

  await page.fill('#close-panel input[name="confirmed_score"]', '0.9');
  await page.click('#close-panel button[type="submit"]');
  await bodyHas('整合中');
  await bodyHas('该单元已闭环');
  log(true, '七态推进', 'INIT→PICK→LEARN→PRODUCE→VERIFY→JUDGE→INTEGRATE');

  // 4. 终态 CLOSED（INTEGRATE → CLOSED）
  const closedBtn = page.locator('button[name="nxt"][value="CLOSED"]');
  if (await closedBtn.count()) {
    await closedBtn.first().click();
    await bodyHas('已闭环');
  }
  log(true, '进入闭环终态');

  // 5. 密钥：保存 → 测试 → 清除（失败/成功都只断言中文结论）
  await page.goto(BASE + '/keys', { waitUntil: 'networkidle' });
  await page.fill('#key-deepseek', 'sk-e2e-fake-key-1234567890');
  await page.click('[data-action="save-key"][data-provider="deepseek"]');
  await bodyHas('已保存');
  const badge = await page.locator('.key-row[data-provider="deepseek"] [data-role="badge"]').innerText();
  log(badge.includes('已配置'), '密钥保存', 'badge=' + badge);

  await page.click('[data-action="test-key"][data-provider="deepseek"]');
  await page.waitForFunction(() => {
    const el = document.querySelector('.key-row[data-provider="deepseek"] [data-role="result"]');
    return el && el.innerText && !el.innerText.includes('测试中');
  }, null, { timeout: 20000 });
  const testText = await page.locator('.key-row[data-provider="deepseek"] [data-role="result"]').innerText();
  log(testText.includes('连接') && !testText.includes('Traceback'), '密钥测试', testText.slice(0, 80));

  await page.click('[data-action="delete-key"][data-provider="deepseek"]');
  await page.waitForTimeout(1200);
  const badge2 = await page.locator('.key-row[data-provider="deepseek"] [data-role="badge"]').innerText();
  log(badge2.includes('未配置'), '密钥清除', 'badge=' + badge2);

  // 7. 清理本次项目（项目生命周期剧本会断言「首页无项目」，不能留下脏数据）
  const csrf = (await ctx.cookies()).find((c) => c.name === 'hpsl_csrf');
  const cleanup = await page.evaluate(async ({ name, token }) => {
    const list = await (await fetch('/api/v1/projects')).json();
    const p = (list.data || []).find((x) => x.name === name);
    if (!p) { return 'notfound'; }
    const r = await fetch('/api/v1/projects/' + p.id + '/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': token },
      body: JSON.stringify({ confirm_name: name })
    });
    return (await r.json()).code;
  }, { name: NAME, token: csrf ? csrf.value : '' });
  log(cleanup === '0', '收尾删除项目', 'code=' + cleanup);

  // 6. 控制台零错误 + 窄屏无横向溢出
  log(consoleErrors.length === 0, '零 console error', consoleErrors.slice(0, 2).join(' | '));
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto(BASE + '/unit', { waitUntil: 'networkidle' });
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  log(!overflow, '375px 视口无横向溢出');

  await browser.close();
  console.log('SUMMARY | ' + (fails.length ? fails.length + ' fail' : 'all pass'));
  process.exit(fails.length ? 1 : 0);
})().catch((e) => {
  console.error('FATAL | ' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
