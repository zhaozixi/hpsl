/**
 * E2E 剧本：项目生命周期（新建 → 归档 → 取消归档 → 错误名删除 → 正确名删除）。
 *
 * 由 tests/e2e/test_project_lifecycle.py 用 node 调起，参数：
 *   argv[2] = base url（如 http://127.0.0.1:54321）
 *   argv[3] = 本次运行的项目名（带随机后缀，避免重跑撞名）
 *
 * 输出契约（pytest 解析）：
 *   PASS | 说明 [| 细节]
 *   FAIL | 说明 | 细节
 *   SUMMARY | n pass, m fail
 * 退出码 0 = 全通过。
 */
const { chromium } = require('playwright');

const BASE = process.argv[2];
const NAME = process.argv[3] || 'E2E归档测试';
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

  // 1. 首页可访问
  const resp = await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  log(resp && resp.status() === 200, '首页返回 200', resp ? String(resp.status()) : 'no response');

  // 2. 新建项目（成功后页面会自动切到 /unit）
  await page.goto(BASE + '/projects/new', { waitUntil: 'networkidle' });
  await page.fill('input[name="name"]', NAME);
  await page.fill('textarea[name="goal"]', 'E2E 目标：验证归档与删除');
  await page.click('#create-form button[type="submit"]');
  await page.waitForTimeout(2000);
  log(!page.url().includes('/projects/new'), '创建成功后离开新建页', page.url());

  // 2b. 密钥页（M38）：三家厂商 + 页面不出现完整密钥
  await page.goto(BASE + '/keys', { waitUntil: 'networkidle' });
  const keyRows = await page.locator('.key-row').count();
  log(keyRows === 3, '密钥页展示三家厂商', 'rows=' + keyRows);
  const keysHtml = await page.content();
  log(!/sk-[A-Za-z0-9._-]{20,}/.test(keysHtml), '密钥页不含完整密钥');

  // 2c. 单元创建（主链冒烟）：创建后就地出现在列表
  await page.goto(BASE + '/unit', { waitUntil: 'networkidle' });
  await page.fill('input[name="title"]', 'E2E单元');
  await page.click('form[hx-post="/api/v1/units"] button[type="submit"]');
  await page.waitForTimeout(1200);
  const unitList = await page.locator('#unit-list-section').innerText();
  log(unitList.includes('E2E单元'), '单元创建后出现在列表');

  // 3. 新项目出现在活跃列表
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  const activeRow = page.locator('#project-list li', { hasText: NAME });
  log((await activeRow.count()) === 1, '新项目出现在活跃列表');

  // 4. 归档
  await activeRow.locator('button', { hasText: '归档' }).click();
  await page.waitForTimeout(900);
  log((await page.locator('#project-list li', { hasText: NAME }).count()) === 0,
    '归档后离开活跃列表');
  log((await page.locator('#archived-box').count()) === 1, '出现「已归档」折叠区');
  const banner = await page.locator('#project-panel .banner').first().innerText().catch(() => '');
  log(banner.includes('已归档'), '顶部提示归档结果', banner.replace(/\s+/g, ' ').slice(0, 60));

  // 5. 取消归档
  await page.locator('#archived-box button', { hasText: '取消归档' }).first().click();
  await page.waitForTimeout(900);
  log((await page.locator('#project-list li', { hasText: NAME }).count()) === 1,
    '取消归档后回到活跃列表');

  // 6. 再归档，然后用错误名称尝试删除
  await page.locator('#project-list li', { hasText: NAME }).locator('button', { hasText: '归档' }).click();
  await page.waitForTimeout(900);
  await page.locator('#archived-box summary', { hasText: '删除这个项目' }).first().click();
  await page.fill('#archived-box input[name="confirm_name"]', '故意写错');
  await page.click('#archived-box button:has-text("移入回收目录")');
  await page.waitForTimeout(1000);
  log((await page.locator('#project-panel .banner.err').count()) === 1, '名称不符时给出错误提示');
  log((await page.locator('#archived-box').count()) === 1,
    '报错后归档区仍在（错误片段没把面板顶掉）');

  // 7. 正确名称删除
  await page.locator('#archived-box summary', { hasText: '删除这个项目' }).first().click();
  await page.fill('#archived-box input[name="confirm_name"]', NAME);
  await page.click('#archived-box button:has-text("移入回收目录")');
  await page.waitForTimeout(1200);
  // 注意：成功提示里就会出现项目名（「已删除『X』…」），所以只能按结构判断，
  // 不能用 innerText 里是否含名字——那会把提示文案算成"项目还在"。
  const activeLeft = await page.locator('#project-list li').count();
  const archivedLeft = await page.locator('#archived-box').count();
  log(activeLeft === 0 && archivedLeft === 0, '删除后项目从列表与归档区消失',
    'active=' + activeLeft + ' archived=' + archivedLeft);
  const panelText = await page.locator('#project-panel').innerText();
  log(panelText.includes('回收目录'), '提示已移入回收目录', panelText.replace(/\s+/g, ' ').slice(0, 80));

  // 8. 详情页：归档标记 + 归档跳转
  await page.goto(BASE + '/projects/new', { waitUntil: 'networkidle' });
  const second = NAME + '-详情';
  await page.fill('input[name="name"]', second);
  await page.fill('textarea[name="goal"]', '详情页操作');
  await page.click('#create-form button[type="submit"]');
  await page.waitForTimeout(2000);
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  await page.locator('#project-list li', { hasText: second }).locator('a').first().click();
  await page.waitForTimeout(600);
  const detailUrl = page.url();
  await page.click('button:has-text("归档此项目")');
  await page.waitForTimeout(1200);
  log(page.url().replace(/\/$/, '') === BASE.replace(/\/$/, ''),
    '详情页归档后自动回到列表页', page.url());
  await page.goto(detailUrl, { waitUntil: 'networkidle' });
  log((await page.locator('.badge.warn', { hasText: '已归档' }).count()) >= 1,
    '归档项目详情页显示「已归档」徽标');

  // 9. 收尾：删掉第二个项目，保持环境干净
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  await page.locator('#archived-box summary', { hasText: '删除这个项目' }).first().click();
  await page.fill('#archived-box input[name="confirm_name"]', second);
  await page.click('#archived-box button:has-text("移入回收目录")');
  await page.waitForTimeout(1000);
  log((await page.locator('#project-list li').count()) === 0
    && (await page.locator('#archived-box').count()) === 0,
    '收尾删除第二个项目');

  // 10. 控制台干净
  log(consoleErrors.length === 0, '零 console error', consoleErrors.join(' | ').slice(0, 300));

  // 11. 窄屏无横向溢出
  await page.setViewportSize({ width: 375, height: 800 });
  await page.goto(BASE + '/', { waitUntil: 'networkidle' });
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth + 1
  );
  log(!overflow, '375px 视口无横向溢出');

  await browser.close();
  console.log('SUMMARY | ' + (fails.length ? fails.length + ' fail' : 'all pass'));
  process.exit(fails.length ? 1 : 0);
})().catch((e) => {
  console.log('FAIL | 剧本异常 | ' + (e && e.message ? e.message.split('\n')[0] : e));
  console.log('SUMMARY | aborted');
  process.exit(2);
});
