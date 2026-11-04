import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Run, RunEvent, RunSpec, Task } from '../src/api';

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

async function mockWorkspace(page: Page, options: { ready?: boolean; initial?: Run; reconnect?: boolean; failStart?: boolean; tasks?: Task[] } = {}) {
  let run = options.initial;
  const requests: { started: RunSpec[]; tokens: string[]; after: string[]; cancelled: number } = { started: [], tokens: [], after: [], cancelled: 0 };
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/session')) return json({ token: 'local-test-token', version: '0.1.0' });
    if (path.endsWith('/doctor')) return json({ ready: options.ready ?? true, checks: { docker: { ready: true }, prepared: { ready: options.ready ?? true } } });
    if (path.endsWith('/tasks')) return json(options.tasks ?? [task]);
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
  await expect(page.getByLabel('Max output tokens')).toHaveValue('16384');
  await page.getByLabel('Max output tokens').fill('0');
  await expect(page.getByRole('button', { name: 'Start benchmark' })).toBeDisabled();
  await page.getByLabel('Max output tokens').fill('8192');
  await page.getByRole('button', { name: 'Start benchmark' }).click();
  await expect.poll(() => requests.started.length).toBe(1);
  expect(requests.started[0]).toMatchObject({ participant: 'openai', inference: { mode: 'api', base_url: 'https://example.invalid/v1', model: 'test-model', api_key_env: 'TEST_MODEL_KEY' }, generation: { max_tokens: 8192 }, budgets: { max_turns: 10 } });
  await expect(page.locator('.run-summary p')).toContainText('API inference');
});

test('recent model runs prefill the next endpoint without fixing a machine-specific default', async ({ page }) => {
  const previous = makeRun({
    participant: 'openai',
    inference: { mode: 'offline', base_url: 'http://127.0.0.1:8081/v1', model: 'qwen3.8-27b', api_key_env: null },
  });
  const requests = await mockWorkspace(page, { initial: previous });
  await page.goto('/');
  await expect(page.getByLabel('Participant', { exact: true })).toHaveValue('openai');
  await expect(page.getByLabel('Endpoint URL')).toHaveValue('http://127.0.0.1:8081/v1');
  await expect(page.getByLabel('Model identifier')).toHaveValue('qwen3.8-27b');
  await page.getByRole('button', { name: 'Start benchmark' }).click();
  await expect.poll(() => requests.started.length).toBe(1);
  expect(requests.started[0].inference).toMatchObject({
    mode: 'offline', base_url: 'http://127.0.0.1:8081/v1', model: 'qwen3.8-27b',
  });
  expect(requests.started[0].generation?.max_tokens).toBe(16384);
});

test('saves an endpoint, discovers models, and queues only checked models', async ({ page }) => {
  const requests = await mockWorkspace(page);
  let saved: Array<{ id: string; name: string; mode: string; base_url: string; api_key_env: string | null; api_style: string; model_ids: string[] }> = [];
  const mutations: string[] = [];
  await page.route('**/api/v1/endpoints**', async route => {
    const req = route.request();
    const url = new URL(req.url());
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (url.pathname === '/api/v1/endpoints' && req.method() === 'GET') return json(saved);
    if (url.pathname === '/api/v1/endpoints' && req.method() === 'POST') {
      mutations.push(req.headers()['x-benchmaxxing-token']);
      const value = { ...req.postDataJSON(), id: 'saved-1' };
      saved = [value];
      return json(value, 201);
    }
    if (url.pathname.endsWith('/models')) {
      mutations.push(req.headers()['x-benchmaxxing-token']);
      return json({ endpoint_id: 'saved-1', model_ids: ['qwen-a', 'qwen-b'] });
    }
    if (url.pathname.endsWith('/saved-1') && req.method() === 'PUT') {
      mutations.push(req.headers()['x-benchmaxxing-token']);
      const value = { ...req.postDataJSON(), id: 'saved-1' };
      saved = [value];
      return json(value);
    }
    return json({ detail: 'Not found' }, 404);
  });
  await page.goto('/');
  await page.getByLabel('Participant', { exact: true }).selectOption('openai');
  await page.getByLabel('Model source').selectOption('saved');
  await page.getByRole('button', { name: 'Add endpoint' }).click();
  await page.getByLabel('Endpoint name').fill('Local Qwen');
  await page.getByLabel('Server URL').fill('http://127.0.0.1:8081/v1');
  await page.getByLabel('API style').selectOption('responses');
  await page.getByRole('button', { name: 'Save and discover models' }).click();
  await expect(page.getByRole('group', { name: 'Local Qwen models' }).getByRole('checkbox')).toHaveCount(2);
  await page.getByRole('group', { name: 'Local Qwen models' }).getByRole('checkbox', { name: 'qwen-b' }).uncheck();
  await expect(page.getByText('1 attempt · 1 task · 1 models')).toBeVisible();
  await page.getByRole('button', { name: 'Start benchmark' }).click();
  await expect.poll(() => requests.started.length).toBe(1);
  expect(requests.started[0].inference).toMatchObject({
    base_url: 'http://127.0.0.1:8081/v1', model: 'qwen-a', api_style: 'responses',
  });
  expect(mutations).toEqual(['local-test-token', 'local-test-token', 'local-test-token', 'local-test-token']);
});

test('selected saved endpoints queue a run for every checked model', async ({ page }) => {
  const requests = await mockWorkspace(page);
  const endpoints = [
    { id: 'one', name: 'First server', mode: 'offline', base_url: 'http://127.0.0.1:8081/v1', api_key_env: null, api_style: 'chat_completions', model_ids: ['model-a', 'model-b'] },
    { id: 'two', name: 'Second server', mode: 'api', base_url: 'https://example.invalid/v1', api_key_env: 'TEST_KEY', api_style: 'responses', model_ids: ['model-c'] },
  ];
  await page.route('**/api/v1/endpoints', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify(endpoints) }));
  await page.goto('/');
  await page.getByLabel('Participant', { exact: true }).selectOption('openai');
  await expect(page.getByLabel('Model source')).toHaveValue('saved');
  await page.getByRole('checkbox', { name: /Second server/ }).check();
  await expect(page.getByText('3 attempts · 1 task · 3 models')).toBeVisible();
  await page.getByRole('button', { name: 'Queue 3 runs' }).click();
  await expect.poll(() => requests.started.length).toBe(3);
  expect(requests.started.map(item => item.inference?.model)).toEqual(['model-a', 'model-b', 'model-c']);
  expect(requests.started[2].inference).toMatchObject({ mode: 'api', api_style: 'responses', api_key_env: 'TEST_KEY' });
});

test('a task selection spans catalog filters and combines with checked models', async ({ page }) => {
  const other: Task = { ...task, id: 'terminal.log-demo', title: 'Inspect logs', category: 'terminal', task_hash: 'b'.repeat(64) };
  const requests = await mockWorkspace(page, { tasks: [task, other] });
  const endpoint = { id: 'one', name: 'Two models', mode: 'offline', base_url: 'http://127.0.0.1:8081/v1', api_key_env: null, api_style: 'chat_completions', model_ids: ['model-a', 'model-b'] };
  await page.route('**/api/v1/endpoints', route => route.fulfill({ contentType: 'application/json', body: JSON.stringify([endpoint]) }));
  await page.goto('/');
  await page.getByLabel('Participant', { exact: true }).selectOption('openai');
  await expect(page.getByLabel('Model source')).toHaveValue('saved');
  await page.getByRole('button', { name: 'Add selected task' }).click();
  await page.locator('#task-category').selectOption('terminal');
  await expect(page.locator('.catalog-detail h3')).toHaveText('Inspect logs');
  await page.getByRole('button', { name: 'Add selected task' }).click();
  await expect(page.getByText('4 attempts · 2 tasks · 2 models')).toBeVisible();
  await page.getByRole('button', { name: 'Queue 4 runs' }).click();
  await expect.poll(() => requests.started.length).toBe(4);
  expect(requests.started.map(item => `${item.task_id}:${item.inference?.model}`)).toEqual([
    'python.interval-merge:model-a', 'python.interval-merge:model-b',
    'terminal.log-demo:model-a', 'terminal.log-demo:model-b',
  ]);
});

test('completed runs show recorded time without treating an unknown duration as zero', async ({ page }) => {
  const timed = makeRun(); timed.metadata = { ...timed.metadata, elapsed_seconds: 22.5 };
  await mockWorkspace(page, { initial: timed });
  await page.goto('/');
  await expect(page.locator('.run-summary')).toContainText('Time to completion 22.5s');
  await expect(page.locator('.history tbody tr').first()).toContainText('22.5s');
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
