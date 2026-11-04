import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Run, RunEvent, RunSpec } from '../src/api';

const now = '2026-11-03T18:30:00Z';
const task = { schema_version: 1, id: 'python.interval-merge', title: 'Repair interval merging', version: '1.0.0', category: 'programming', description: 'Repair the interval merger.', task_hash: 'a'.repeat(64), image: 'sha256:prepared' };
const event = (seq: number, kind: string): RunEvent => ({ seq, run_id: 'run-test-001', kind, timestamp: now, data: { message: 'Recorded evidence' } });
const stream = (items: RunEvent[]) => items.map(item => `id: ${item.seq}\nevent: run_event\ndata: ${JSON.stringify(item)}\n\n`).join('');
const makeRun = (spec: RunSpec = {}): Run => ({
  id: 'run-test-001', status: 'passed', created_at: now, updated_at: now,
  spec: { participant: 'scripted', scripted_variant: 'good', task_id: 'python.interval-merge', ...spec },
  score: { passed: true, score: 1, grader_version: 'interval-blackbox/1', checks: [{ name: 'Overlapping intervals', passed: true, detail: 'Expected output matched.' }] },
  metadata: { harness_version: 'benchmaxxing-tools/1' },
});

async function mockWorkspace(page: Page, options: { ready?: boolean; initial?: Run; reconnect?: boolean; failStart?: boolean } = {}) {
  let run = options.initial;
  const requests: { started: RunSpec[]; tokens: string[]; after: string[]; cancelled: number } = { started: [], tokens: [], after: [], cancelled: 0 };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/session')) return json({ token: 'local-test-token', version: '0.1.0' });
    if (path.endsWith('/doctor')) return json({ ready: options.ready ?? true, checks: { docker: { ready: true }, prepared: { ready: options.ready ?? true } } });
    if (path.endsWith('/tasks')) return json([task]);
    if (path.endsWith('/runs')) {
      if (request.method() === 'POST') {
        if (options.failStart) return json({ detail: 'The task image is not prepared.' }, 409);
        const spec = request.postDataJSON() as RunSpec;
        requests.started.push(spec);
        requests.tokens.push(request.headers()['x-benchmaxxing-token']);
        run = makeRun(spec);
        if (options.reconnect) { run.status = 'running'; run.score = null; }
        return json(run, 201);
      }
      return json(run ? [run] : []);
    }
    if (path.endsWith('/cancel')) {
      requests.cancelled++;
      if (run) run = { ...run, status: 'cancelled', score: null };
      return json(run);
    }
    if (path.endsWith('/events')) {
      const after = url.searchParams.get('after') ?? '0';
      requests.after.push(after);
      if (options.reconnect && after === '1' && run) { run = { ...run, status: 'passed', score: makeRun().score }; }
      const items = options.reconnect ? [event(after === '0' ? 1 : 2, after === '0' ? 'run.started' : 'run.completed')] : [event(1, 'run.started'), event(2, 'run.completed')];
      return route.fulfill({ status: 200, contentType: 'text/event-stream', body: stream(items) + (run && !['queued', 'running'].includes(run.status) ? 'event: complete\ndata: {}\n\n' : '') });
    }
    if (path.includes('/artifacts/')) return route.fulfill({ contentType: 'text/plain', body: '<script>window.injected = true</script>\ndef merge_intervals(items):\n    return []\n' });
    if (path === '/api/v1/runs/run-test-001') return json({ run, artifacts: [{ id: 'artifact-001', run_id: 'run-test-001', name: 'solution.py', media_type: 'text/plain', size: 42, sha256: 'b'.repeat(64) }] });
    return json({ detail: 'Not found' }, 404);
  });
  return requests;
}

test('starts a run, inspects grading and safely renders artifact evidence', async ({ page }) => {
  const requests = await mockWorkspace(page);
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Every run, inspectable.' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start benchmark' })).toBeEnabled();
  await page.getByRole('button', { name: 'Start benchmark' }).click();
  await expect(page.getByRole('heading', { name: 'Evidence, in context' })).toBeVisible();
  await expect.poll(() => requests.started.length).toBe(1);
  expect(requests.started[0]).toMatchObject({ participant: 'scripted', scripted_variant: 'good', budgets: { max_turns: 24, wall_seconds: 1800 } });
  expect(requests.tokens).toEqual(['local-test-token']);
  await expect(page.getByText('run / completed', { exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Grading' }).click();
  await expect(page.getByText('Overlapping intervals', { exact: true })).toBeVisible();
  await expect(page.getByText('Expected output matched.', { exact: true })).toBeVisible();
  await page.getByRole('tab', { name: 'Artifacts' }).click();
  await page.getByRole('button', { name: /solution.py/ }).click();
  await expect(page.locator('.artifact-preview pre')).toContainText('<script>window.injected = true</script>');
  expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  await expect(page.getByRole('link', { name: 'Export', exact: true })).toHaveAttribute('href', '/api/v1/runs/run-test-001/export');
});

test('unprepared environments show an actionable preparation command and disable runs', async ({ page }) => {
  await mockWorkspace(page, { ready: false });
  await page.goto('/');
  await expect(page.getByText('Prepare your task environment', { exact: true })).toBeVisible();
  await expect(page.getByText('benchmaxxing prepare', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start benchmark' })).toBeDisabled();
  await page.getByText('View environment checks').click();
  await expect(page.locator('.diagnostics pre')).toContainText('prepared');
});

test('model profiles submit endpoint settings and key variable names', async ({ page }) => {
  const requests = await mockWorkspace(page);
  await page.goto('/');
  await page.getByLabel('Participant', { exact: true }).selectOption('openai');
  await page.getByLabel('Inference access').selectOption('api');
  await page.getByLabel('Endpoint URL').fill('https://example.invalid/v1');
  await page.getByLabel('Model identifier').fill('test-model');
  await page.getByLabel('API key environment variable').fill('TEST_MODEL_KEY');
  await page.getByLabel('Max turns').fill('10');
  await page.getByRole('button', { name: 'Start benchmark' }).click();
  await expect.poll(() => requests.started.length).toBe(1);
  expect(requests.started[0]).toMatchObject({ participant: 'openai', inference: { mode: 'api', base_url: 'https://example.invalid/v1', model: 'test-model', api_key_env: 'TEST_MODEL_KEY' }, budgets: { max_turns: 10 } });
  await expect(page.locator('.run-summary p')).toContainText('API inference');
});

test('cancelled runs remain inspectable', async ({ page }) => {
  const requests = await mockWorkspace(page, { initial: { ...makeRun(), status: 'running', score: null } });
  await page.goto('/');
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  await expect.poll(() => requests.cancelled).toBe(1);
  await expect(page.locator('.run-summary .status')).toHaveText('cancelled');
  await expect(page.getByRole('button', { name: 'Cancel', exact: true })).toHaveCount(0);
  await page.getByRole('tab', { name: 'Grading' }).click();
  await expect(page.getByText('This run ended without a scorecard. Inspect the trace for details.')).toBeVisible();
});

test('SSE reconnect resumes after the last sequence without duplicating events', async ({ page }) => {
  const requests = await mockWorkspace(page, { reconnect: true });
  await page.goto('/');
  await page.getByRole('button', { name: 'Start benchmark' }).click();
  await expect.poll(() => requests.after.includes('1'), { timeout: 10_000 }).toBe(true);
  await expect(page.locator('.event')).toHaveCount(2);
  await expect(page.locator('.run-summary .status')).toHaveText('passed');
});

test('API errors are actionable and keep the run form usable', async ({ page }) => {
  await mockWorkspace(page, { failStart: true });
  await page.goto('/');
  await page.getByRole('button', { name: 'Start benchmark' }).click();
  await expect(page.getByRole('alert')).toContainText('The task image is not prepared.');
  await expect(page.getByRole('button', { name: 'Start benchmark' })).toBeEnabled();
});

test('workspace remains usable on a narrow viewport', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockWorkspace(page, { initial: makeRun() });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Start a run' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Start benchmark' })).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});
