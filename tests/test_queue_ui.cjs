const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

test('backend batches render and respond to queue controls in the browser', async () => {
  const root = path.resolve(__dirname, '..');
  const fixtures = JSON.parse(execFileSync(process.env.TEST_PYTHON || 'python3', [path.join(__dirname, 'queue_fixture.py')], { encoding: 'utf8' }));
  const html = fs.readFileSync(path.join(root, 'web/templates/index.html'), 'utf8').replace(/\{\{[\s\S]*?\}\}/g, '');
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.TEST_BROWSER_CHANNEL ? { channel: process.env.TEST_BROWSER_CHANNEL } : {}),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    const actions = [];
    let queue = structuredClone(fixtures.running);
    page.on('pageerror', (error) => errors.push(error.message));
    await page.route('**/*', async (route) => {
      const url = new URL(route.request().url());
      let data;
      if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: html });
      if (url.pathname === '/api/status') {
        data = { authorized: true, state: 'running', settings: {}, logs: '', missing: [], update: {} };
      } else if (url.pathname.startsWith('/api/downloads')) {
        if (route.request().method() === 'POST') {
          actions.push(url.pathname);
          if (url.pathname.endsWith('/pause')) queue = structuredClone(fixtures.paused);
          else if (url.pathname.endsWith('/resume')) queue = structuredClone(fixtures.running);
          else if (url.pathname.endsWith('/stop')) {
            const id = decodeURIComponent(url.pathname.split('/')[4]);
            queue.items = queue.items.filter((item) => item.id !== id);
            queue.total = queue.items.length;
          }
        }
        data = queue;
      } else return route.fulfill({ status: 204, body: '' });
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
    });
    await page.goto('http://queue.test/');
    await page.waitForFunction(() => document.querySelectorAll('.queue-item').length === 2);
    assert.match(await page.locator('#queueHint').innerText(), /共 2 个任务/);
    assert.match(await page.locator('#queueList').innerText(), /完成 5\/20/);
    assert.match(await page.locator('#queueList').innerText(), /完成 0\/51/);
    assert.match(await page.locator('#queueList').innerText(), /当前传输.*40\.0%/);
    await page.click('#queuePauseBtn');
    await page.waitForFunction(() => document.getElementById('queueHint').textContent.includes('已暂停'));
    assert.equal(await page.locator('#queuePauseBtn').isDisabled(), true);
    assert.equal(await page.locator('#queueResumeBtn').isEnabled(), true);
    await page.click('#queueResumeBtn');
    await page.waitForFunction(() => !document.getElementById('queueHint').textContent.includes('已暂停'));
    await page.locator('.queue-item').filter({ hasText: 'comments-folder' }).locator('button').click();
    await page.waitForFunction(() => document.querySelectorAll('.queue-item').length === 1);
    assert.ok(actions.includes('/api/downloads/batch/comments%3Atest/stop'));
    assert.match(await page.locator('#queueList').innerText(), /range-folder/);

    queue = structuredClone(fixtures.building);
    await page.evaluate((data) => renderQueue(data), queue);
    assert.match(await page.locator('#queueList').innerText(), /正在加入队列.*完成 0\/51/);
    await page.setViewportSize({ width: 390, height: 844 });
    await page.click('#navQueue');
    assert.equal(await page.locator('#queueView').isVisible(), true);
    queue = { available: false, items: [] };
    await page.evaluate((data) => renderQueue(data), queue);
    assert.match(await page.locator('#queueList').innerText(), /启动机器人后/);
    assert.equal(await page.locator('#queuePauseBtn').isDisabled(), true);
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
});
