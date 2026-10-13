import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';

const png = readFileSync(new URL('./fixtures/product.png', import.meta.url));
const types = ['品牌导购主视觉','核心卖点信息图','生活方式海报','多场景展示图','场景化卖点图','使用体验图','SKU展示图','细节展示图'];
const options = { platform: ['TEMU', '淘宝'], ...Object.fromEntries(
  ['language','usage_scene','visual_style','selling_points'].map(k => [k, {
    choices: ['A','B','C'].map(code => ({ code, text: `${k} ${code}` })),
    allow_agent_design: true, allow_custom: true,
  }])) };
const oldTask = { task_id: 'old-task', status: 'succeeded', created_at: '2026-10-12T00:00:00Z',
  counts: { planned: 1, succeeded: 1, failed: 0, pending: 0, unresolved: 0 }, items: [{
    task_item_id: 'old-item', image_id: 'old-image', image_type: types[0], position: 1,
    status: 'succeeded', preview_ready: true,
  }] };

async function setup(page, state = {}) {
  Object.assign(state, { calls: [], owner: 'owner-1', sessionId: 'session-1', polls: 0, ...state });
  await page.route('**/api/v1/**', route => {
    const req = route.request(), path = new URL(req.url()).pathname.replace('/api/v1', '');
    state.calls.push({ path, method: req.method(), key: req.headers()['idempotency-key'],
      body: req.method() === 'POST' && !path.startsWith('/reference-sets') ? req.postDataJSON() : null });
    const json = (body, status = 200) => route.fulfill({ status, json: body });
    const detail = () => ({ business_session_id: state.sessionId, status: state.unknown ? 'reconciling' : state.ready ? 'recommendation_ready' : 'awaiting_recommendation',
      selection_ready: !!state.ready, can_end_before_start: !!state.unknown,
      safe_reason_code: state.unknown ? 'DIFY_REFERENCE_UPLOAD_OUTCOME_UNKNOWN' : null,
      task_id: state.activeTask ? 'active-task' : null,
      recommendation: state.ready ? { recommendation_id: 'rec-1', recommendation_revision: 1,
        public_options: options, image_types: types } : null });
    if (path === '/auth/guest/reset') { state.owner = 'owner-2'; state.sessionId = 'session-2'; return json({ user_id: state.owner, identity_kind: 'guest' }); }
    if (path === '/auth/guest/refresh') return route.fulfill({ status: 204 });
    if (path.startsWith('/auth/')) return json({ user_id: state.owner, identity_kind: 'guest' });
    if (['/quota', '/billing/balance'].includes(path)) return json({ enabled: false });
    const historical = state.oldFailed ? { ...oldTask, status: 'failed',
      counts: { ...oldTask.counts, succeeded: 0, failed: 1 },
      items: [{ ...oldTask.items[0], status: 'failed', preview_ready: false, image_id: null,
        error: { code: 'IMAGE_GENERATION_FAILED', message: '生成失败' } }] } : oldTask;
    if (path === '/tasks') return json({ items: [historical], next_cursor: null });
    if (path === '/tasks/old-task') return json(historical);
    if (path === '/tasks/old-task/retry') {
      state.retryCalls = (state.retryCalls || 0) + 1;
      if (state.retryCalls === 1) return json({ code: 'UNKNOWN', message: '历史重试结果未知' }, 503);
      state.oldFailed = false; return json({ task_id: 'old-task' });
    }
    if (path.endsWith('/selection')) { state.activeTask = true; return json({ task_id: 'active-task', status: 'queued', planned_count: 1 }, 202); }
    if (path === '/tasks/active-task') return json({ ...oldTask, task_id: 'active-task',
      status: state.generated ? 'succeeded' : 'generating',
      items: state.generated ? oldTask.items : [{ ...oldTask.items[0], status: 'running', image_id: null, preview_ready: false }],
      counts: { planned: 1, succeeded: state.generated ? 1 : 0, failed: 0, pending: state.generated ? 0 : 1, unresolved: 0 } });
    if (path.endsWith('/preview') || path.endsWith('/original')) return route.fulfill({ contentType: 'image/png', body: png });
    if (path.startsWith('/reference-sets')) return json({ reference_set_id: 'ref-1', product_category: '咖啡杯', status: 'ready' });
    if (path === '/business-sessions/readiness') return state.offline ? json({ code: 'CREATION_DEPENDENCY_UNAVAILABLE', message: 'Dify服务暂不可用，请运行一键启动' }, 503) : json({ status: 'ready' });
    if (path === '/business-sessions' && req.method() === 'POST') return json({ business_session_id: state.sessionId, status: 'starting' }, 202);
    if (path === '/business-sessions') return json({ items: [{ business_session_id: state.sessionId,
      reference_set_id: 'ref-1', product_category: '咖啡杯', created_at: '2026-10-12T00:00:00Z', status: detail().status }], next_cursor: null });
    if (path.endsWith('/end-unstarted')) { state.unknown = false; return json({ business_session_id: state.sessionId, status: 'failed', safe_reason_code: 'PRESTART_UPLOAD_ABANDONED' }); }
    if (path === `/business-sessions/${state.sessionId}`) { state.polls++; return json(detail()); }
    return json({ code: 'NOT_FOUND' }, 404);
  });
  await page.goto(`http://127.0.0.1:${process.env.WORKBENCH_TEST_PORT || 5174}/`);
  await expect(page.getByText('访客会话已连接')).toBeVisible();
  return state;
}
async function upload(page) {
  await page.locator('#category').fill('咖啡杯');
  await page.locator('#primary-image').setInputFiles({ name: 'product.png', mimeType: 'image/png', buffer: png });
  await page.getByRole('button', { name: '获取画面推荐' }).click();
}
const saved = page => page.evaluate(() => JSON.parse(sessionStorage.getItem('visual-studio:flow')));

test('repair: history viewing and download preserve analyzing creation, polling and reload', async ({ page }) => {
  const state = await setup(page); await upload(page);
  await expect(page.getByRole('heading', { name: '正在准备画面推荐' })).toBeVisible();
  const before = await saved(page), polls = state.polls;
  await page.getByRole('button', { name: /历史记录$/ }).click();
  await expect(page.getByRole('heading', { name: '推荐与创作流程' })).toBeVisible();
  await page.locator('button.history-row').click();
  await expect(page.getByRole('heading', { name: '历史任务详情' })).toBeVisible();
  await page.screenshot({ path: 'test-results/历史任务独立查看.png', fullPage: true });
  expect(await saved(page)).toEqual(before);
  const downloaded = page.waitForEvent('download');
  await page.getByRole('button', { name: '↓ 下载原图' }).click();
  expect((await downloaded).suggestedFilename()).toContain('old-task');
  state.ready = true;
  await expect.poll(() => state.polls).toBeGreaterThan(polls);
  await page.getByRole('button', { name: '返回当前创作', exact: true }).click();
  await expect(page.getByRole('heading', { name: '确认你的画面偏好' })).toBeVisible();
  await page.screenshot({ path: 'test-results/返回原创作.png', fullPage: true });
  expect((await saved(page)).sessionId).toBe('session-1');
  await page.reload();
  await expect(page.getByRole('heading', { name: '确认你的画面偏好' })).toBeVisible();
  expect(state.calls.filter(c => c.method === 'POST' && c.path === '/business-sessions')).toHaveLength(1);
  expect(state.calls.filter(c => /cancel|selection/.test(c.path))).toHaveLength(0);
});

test('repair: chosen preferences survive opening history task', async ({ page }) => {
  await setup(page, { ready: true }); await upload(page);
  await page.locator('#platform').selectOption('淘宝');
  await page.locator('input[name="language"][value="custom"]').check();
  await page.getByRole('textbox', { name: '自定义文案语言' }).fill('保留这段文案');
  await page.getByRole('button', { name: /历史记录$/ }).click();
  await page.locator('button.history-row').click();
  await page.getByRole('button', { name: '返回当前创作', exact: true }).click();
  await expect(page.locator('#platform')).toHaveValue('淘宝');
  await expect(page.getByRole('textbox', { name: '自定义文案语言' })).toHaveValue('保留这段文案');
});

test('repair: reset permits a new owner while old workflow is unfinished', async ({ page }) => {
  const state = await setup(page); await upload(page);
  await page.getByRole('button', { name: '重置访客身份', exact: true }).click();
  await page.getByRole('button', { name: '确认重置', exact: true }).click();
  state.ready = true; await upload(page);
  await expect(page.getByRole('heading', { name: '确认你的画面偏好' })).toBeVisible();
  expect((await saved(page)).owner).toBe('owner-2');
  expect((await saved(page)).sessionId).toBe('session-2');
  expect(state.calls.filter(c => c.path.endsWith('/cancel'))).toHaveLength(0);
});

test('repair: unknown prestart upload can be ended and retried explicitly', async ({ page }) => {
  const state = await setup(page, { unknown: true }); await upload(page);
  await expect(page.getByRole('button', { name: '结束未启动流程' })).toBeVisible();
  await page.getByRole('button', { name: '结束未启动流程' }).click();
  await expect(page.getByRole('button', { name: '获取新的推荐' })).toBeVisible();
  state.ready = true; await page.getByRole('button', { name: '获取新的推荐' }).click();
  await expect(page.getByRole('heading', { name: '确认你的画面偏好' })).toBeVisible();
});

test('repair: dependency outage stops before creating a session and preserves reference', async ({ page }) => {
  const state = await setup(page, { offline: true }); await upload(page);
  await expect(page.getByText('Dify服务暂不可用，请运行一键启动', { exact: true })).toBeVisible();
  expect((await saved(page)).referenceId).toBe('ref-1');
  expect(state.calls.filter(c => c.method === 'POST' && c.path === '/business-sessions')).toHaveLength(0);
  state.offline = false; state.ready = true;
  await page.getByRole('button', { name: '获取新的推荐' }).click();
  await expect(page.getByRole('heading', { name: '确认你的画面偏好' })).toBeVisible();
});

test('repair: active generation continues while uncertain historical retry keeps its own key', async ({ page }) => {
  const state = await setup(page, { ready: true, oldFailed: true }); await upload(page);
  await page.getByRole('button', { name: '确认并生成 1 张' }).click();
  await expect(page.getByRole('heading', { name: '生成中', exact: true })).toBeVisible();
  const before = await saved(page);
  await page.getByRole('button', { name: /历史记录$/ }).click();
  await page.locator('button.history-row').click();
  await page.getByRole('button', { name: '↻ 重试这张图片' }).click();
  await expect(page.getByText('历史任务重试结果尚未确认')).toBeVisible();
  expect((await saved(page)).taskId).toBe(before.taskId);
  expect((await saved(page)).pending).toBeNull();
  await page.getByRole('button', { name: '确认历史任务重试' }).click();
  await expect.poll(() => state.calls.filter(c => c.path === '/tasks/old-task/retry').length).toBe(2);
  await expect(page.getByText('历史任务重试结果尚未确认')).toHaveCount(0);
  const posts = state.calls.filter(c => c.path === '/tasks/old-task/retry');
  expect(posts).toHaveLength(2);
  expect(posts[0].key).toBe(posts[1].key); expect(posts[0].body).toEqual(posts[1].body);
  state.generated = true;
  await page.getByRole('button', { name: '返回当前创作', exact: true }).click();
  await expect(page.getByRole('heading', { name: '已完成', exact: true })).toBeVisible({ timeout: 10000 });
  expect((await saved(page)).taskId).toBe('active-task');
  expect(state.calls.filter(c => c.path.endsWith('/selection'))).toHaveLength(1);
});
