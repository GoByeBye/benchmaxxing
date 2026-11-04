import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Run, RunEvent } from '../src/api';
import type { ActivityReport, ActivityRun } from '../src/useActivity';

const now = new Date().toISOString();
const records: Run[] = [
  { id: 'historical-run', status: 'passed', spec: { task_id: 'python.old', participant: 'scripted' }, created_at: now, updated_at: now, metadata: { task: { title: 'Earlier experiment', category: 'programming' } } },
  ...['alpha', 'beta', 'gamma'].map((name, index): Run => ({ id: `run-${name}`, status: index ? 'queued' : 'running', spec: { task_id: `python.${name}`, participant: 'openai', inference: { model: 'local-model', base_url: 'http://localhost:8080/v1' } }, created_at: now, updated_at: now, metadata: { task: { title: `${name} task`, category: 'programming' } } })),
];
const activityRun = (name: string, status: ActivityRun['status'] = 'running', position: number | null = null): ActivityRun => ({
  id: `run-${name}`, status, task_id: `python.${name}`, task_title: `${name} task`, category: 'programming', participant: 'openai', model: 'local-model', created_at: now, started_at: status === 'running' ? now : null, updated_at: now, phase: 'inference', turn: 3, max_turns: 24, wall_seconds: 120, elapsed_seconds: 30, queue_position: position, last_event_at: now, last_event_seq: 1, cancellation_requested: false,
});
const event = (seq: number): RunEvent => ({ seq, run_id: 'run-alpha', kind: 'model.response', timestamp: now, data: { text: `Evidence ${seq}` } });
const stream = (events: RunEvent[]) => events.map(item => `id: ${item.seq}\nevent: run_event\ndata: ${JSON.stringify(item)}\n\n`).join('');

async function fixture(page: Page, options: { burst?: boolean; burstCount?: number; slowActivity?: boolean; holdDetail?: boolean; terminalReplay?: boolean } = {}) {
  const runs = [...records];
  let report: ActivityReport = { schema_version: 1, server_time: now, runs: [activityRun('alpha'), activityRun('beta', 'queued', 1), activityRun('gamma', 'queued', 2)] };
  let failActivity = false;
  let activityRequests = 0;
  let inflight = 0;
  let maximumInflight = 0;
  let releaseBurst: () => void = () => {};
  const burstGate = new Promise<void>(resolve => { releaseBurst = resolve; });
  let releaseDetail: () => void = () => {};
  const detailGate = new Promise<void>(resolve => { releaseDetail = resolve; });
  let releasedDetail = false;
  let releaseActivity: () => void = () => {};
  const activityGate = new Promise<void>(resolve => { releaseActivity = resolve; });
  let holdHistory = false;
  let historyHeld = false;
  let releaseHistory: () => void = () => {};
  const historyGate = new Promise<void>(resolve => { releaseHistory = resolve; });
  const calls = { doctor: 0, history: 0, detail: 0, detailIds: [] as string[], after: [] as string[] };
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/run-progress')) {
      activityRequests++;
      // Development StrictMode aborts its first mount; measure overlap after that initialization.
      const measure = activityRequests > 2;
      if (measure) { inflight++; maximumInflight = Math.max(maximumInflight, inflight); }
      if (options.slowActivity) {
        if (!measure) await activityGate;
        else await new Promise(resolve => setTimeout(resolve, 1400));
      }
      if (measure) inflight--;
      return failActivity ? json({ detail: 'Connection interrupted' }, 503) : json(report);
    }
    if (path.endsWith('/execution/settings')) return json({ max_parallel_runs: 1 });
    if (path.endsWith('/session')) return json({ token: 'monitor-token', version: '0.1.0' });
    if (path.endsWith('/doctor')) { calls.doctor++; return json({ ready: true, checks: {} }); }
    if (path.endsWith('/tasks')) return json([{ id: 'python.alpha', title: 'alpha task', category: 'programming', description: 'Solve alpha.', version: '1.0.0', task_hash: 'alpha', image: 'prepared' }]);
    if (path.endsWith('/runs')) {
      if (route.request().method() === 'POST') {
        const created: Run = { id: 'created-run', status: 'passed', spec: route.request().postDataJSON() as Run['spec'], created_at: new Date(Date.parse(now) + 2000).toISOString(), updated_at: new Date(Date.parse(now) + 3000).toISOString(), metadata: {} };
        runs.unshift(created);
        return json(created, 201);
      }
      calls.history++;
      const snapshot = [...runs];
      if (holdHistory) { holdHistory = false; historyHeld = true; await historyGate; }
      return json(snapshot);
    }
    const id = path.split('/')[4];
    if (path.endsWith('/events')) {
      const after = url.searchParams.get('after') ?? '0'; calls.after.push(after);
      if (id === 'run-alpha' && options.terminalReplay) return route.fulfill({ contentType: 'text/event-stream', body: after === '0' ? stream([event(1)]) : stream([event(1), event(2)]) + 'event: complete\ndata: {}\n\n' });
      if (id === 'run-alpha' && options.burst) {
        if (after === '0') return route.fulfill({ contentType: 'text/event-stream', body: stream(Array.from({ length: 500 }, (_, index) => event(index + 1))) });
        await burstGate;
        return route.fulfill({ contentType: 'text/event-stream', body: stream([event(500), ...Array.from({ length: options.burstCount ?? 10 }, (_, index) => event(index + 501))]) });
      }
      return route.fulfill({ contentType: 'text/event-stream', body: '' });
    }
    calls.detail++;
    calls.detailIds.push(id);
    if (id === 'run-alpha' && options.holdDetail) { await detailGate; releasedDetail = true; }
    const run = runs.find(item => item.id === id);
    return run ? json({ run: options.terminalReplay && id === 'run-alpha' ? { ...run, status: 'passed', updated_at: new Date(Date.parse(now) + 1000).toISOString() } : run, artifacts: [] }) : json({ detail: 'Not found' }, 404);
  });
  return { calls, setReport: (value: ActivityReport) => { report = value; }, fail: () => { failActivity = true; }, releaseBurst, releaseDetail, releaseActivity, releaseHistory, holdHistory: () => { holdHistory = true; }, historyHeld: () => historyHeld, detailReleased: () => releasedDetail, maximumInflight: () => maximumInflight };
}

test('live activity reports phase, budgets, queue and cancellation without invented progress', async ({ page }) => {
  const state = await fixture(page);
  await page.goto('/');
  const monitor = page.locator('.live-monitor');
  await expect(monitor).toContainText('alpha task');
  await expect(monitor).toContainText('Waiting for model');
  await expect(monitor).toContainText('3 / 24');
  await expect(monitor).toContainText('/ 2m 0s');
  await expect(monitor).toContainText('2 queued');
  await expect(monitor).toContainText('Next: beta task');
  await expect(monitor).toContainText('Last activity');
  const initialDoctorCalls = state.calls.doctor;
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.queue-more-mobile')).toHaveText('+1 more');
  expect(await monitor.innerText()).not.toContain('%');
  const updated = { ...activityRun('alpha'), turn: 4, phase: 'grade', checks_done: 3, checks_total: 8 };
  state.setReport({ schema_version: 1, server_time: now, runs: [updated, activityRun('gamma', 'queued', 1)] });
  await expect(monitor).toContainText('Grading');
  await expect(monitor).toContainText('3 / 8 checks');
  await expect(monitor).toContainText('4 / 24');
  await expect(monitor).toContainText('1 queued');
  await expect(monitor).toContainText('Next: gamma task');
  state.setReport({ schema_version: 1, server_time: now, runs: [{ ...updated, phase: 'tool', last_tool: 'run_command', cancellation_requested: true }] });
  await expect(monitor).toContainText('Cancellation requested');
  await expect(monitor).toContainText('run_command');
  expect(state.calls.doctor).toBe(initialDoctorCalls);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
});

test('follow active advances with the queue and manual history selection stays pinned', async ({ page }) => {
  const state = await fixture(page);
  await page.goto('/');
  await expect(page.getByLabel('Follow active run')).toBeChecked();
  await expect(page.locator('.run-summary-title > .mono')).toHaveText('run-alpha');
  state.setReport({ schema_version: 1, server_time: now, runs: [activityRun('beta')] });
  await expect(page.locator('.run-summary-title > .mono')).toHaveText('run-beta');
  await page.getByRole('button', { name: 'Inspect run historical-run', exact: true }).click();
  await expect(page.getByLabel('Follow active run')).not.toBeChecked();
  await expect(page.locator('.run-summary-title > .mono')).toHaveText('historical-r');
  state.setReport({ schema_version: 1, server_time: now, runs: [activityRun('gamma')] });
  await expect(page.locator('.monitor-task')).toContainText('gamma task');
  await expect(page.locator('.run-summary-title > .mono')).toHaveText('historical-r');
  await expect(page.locator('.monitor-note')).toContainText('Inspection pinned');
  await page.getByLabel('Follow active run').check();
  await expect(page.locator('.run-summary-title > .mono')).toHaveText('run-gamma');
});

test('parallel attempts each expose their phase and inspect their own evidence', async ({ page }) => {
  const state = await fixture(page);
  state.setReport({ schema_version: 1, server_time: now, runs: [activityRun('alpha'), { ...activityRun('beta'), model: 'second-model', phase: 'grade', checks_done: 2, checks_total: 5 }] });
  await page.goto('/');
  const monitor = page.locator('.live-monitor');
  await expect(monitor).toContainText('2 running');
  await expect(monitor.locator('.monitor-current')).toHaveCount(2);
  await expect(monitor.locator('.monitor-current').nth(1)).toContainText('second-model');
  await expect(monitor.locator('.monitor-current').nth(1)).toContainText('2 / 5 checks');
  await page.getByRole('button', { name: 'Inspect active run run-beta', exact: true }).click();
  await expect(page.locator('.run-summary-title > .mono')).toHaveText('run-beta');
  await expect(page.getByLabel('Follow active run')).not.toBeChecked();
});

test('burst traces stay bounded, resume by sequence, preserve reading position and expanded evidence', async ({ page }) => {
  const startedAt = Date.now();
  const state = await fixture(page, { burst: true });
  await page.goto('/');
  await expect(page.locator('.event')).toHaveCount(300);
  await expect(page.locator('.trace-window-note')).toContainText('latest 300 of 500');
  const initialDoctorCalls = state.calls.doctor;
  const row = page.locator('.event').filter({ has: page.locator('.event-seq', { hasText: '#350' }) });
  await row.locator('summary').click();
  await expect(row).toHaveAttribute('open', '');
  await expect(row.locator('pre')).toContainText('Evidence 350');
  await page.locator('.event-list').evaluate(node => { node.scrollTop = 140 * 44; });
  const before = await row.evaluate(node => node.getBoundingClientRect().top);
  state.releaseBurst();
  await expect.poll(() => state.calls.after.includes('500')).toBe(true);
  await expect(page.locator('.trace-window-note')).toContainText('Reading a paused window');
  await expect(page.locator('.trace-window-note')).toContainText('510 received in total');
  await expect(page.locator('.event')).toHaveCount(300);
  await expect(row).toHaveAttribute('open', '');
  const after = await row.evaluate(node => node.getBoundingClientRect().top);
  expect(Math.abs(after - before)).toBeLessThan(5);
  await expect(page.getByRole('button', { name: '10 new events · Jump to latest ↓' })).toBeVisible();
  await page.getByRole('button', { name: '10 new events · Jump to latest ↓' }).click();
  await expect(page.locator('.new-events')).toHaveCount(0);
  expect(await page.locator('.event-list').evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThan(35);
  expect(state.calls.doctor).toBe(initialDoctorCalls);
  expect(state.calls.history).toBeLessThanOrEqual(2 + Math.floor((Date.now() - startedAt) / 5000));
});

test('a paused trace keeps expanded older evidence through more than a full window of new events', async ({ page }) => {
  const state = await fixture(page, { burst: true, burstCount: 400 });
  await page.goto('/');
  await expect(page.locator('.event')).toHaveCount(300);
  const row = page.locator('.event').filter({ has: page.locator('.event-seq', { hasText: /^#350$/ }) });
  await row.locator('summary').click();
  await expect(row.locator('pre')).toContainText('Evidence 350');
  await page.locator('.event-list').evaluate(node => { node.scrollTop = 140 * 44; });
  const before = await page.locator('.event-list').evaluate(node => node.scrollTop);
  const rowBefore = await row.evaluate(node => node.getBoundingClientRect().top);
  state.releaseBurst();
  await expect(page.locator('.trace-window-note')).toContainText('900 received in total');
  await expect(page.locator('.trace-window-note')).not.toContainText('latest');
  await expect(page.locator('.event')).toHaveCount(300);
  await expect(page.locator('.event-seq').first()).toHaveText('#201');
  await expect(page.locator('.event-seq').last()).toHaveText('#500');
  await expect(row).toHaveAttribute('open', '');
  await expect(row.locator('pre')).toContainText('Evidence 350');
  expect(await page.locator('.event-list').evaluate(node => node.scrollTop)).toBe(before);
  expect(Math.abs(await row.evaluate(node => node.getBoundingClientRect().top) - rowBefore)).toBeLessThan(5);
  await page.getByRole('button', { name: '400 new events · Jump to latest ↓' }).click();
  await expect(page.locator('.trace-window-note')).toContainText('latest 300 of 900');
  await expect(page.locator('.event')).toHaveCount(300);
  await expect(row).toHaveCount(0);
  await expect(page.locator('.event-seq').first()).toHaveText('#601');
  await expect(page.locator('.event-seq').last()).toHaveText('#900');
  expect(await page.locator('.event-list').evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThan(35);
  await expect(page.locator('.new-events')).toHaveCount(0);
});

test('a delayed response from a previous selection cannot replace a pinned run', async ({ page }) => {
  const state = await fixture(page, { holdDetail: true });
  await page.goto('/');
  await expect.poll(() => state.calls.detailIds.includes('run-alpha')).toBe(true);
  await page.getByRole('button', { name: 'Inspect run historical-run', exact: true }).click();
  await expect(page.locator('.run-summary-title > .mono')).toHaveText('historical-r');
  state.releaseDetail();
  await expect.poll(() => state.detailReleased()).toBe(true);
  await expect(page.locator('.run-summary-title > .mono')).toHaveText('historical-r');
  await expect(page.locator('.run-task-name')).toContainText('Earlier experiment');
  await expect(page.getByLabel('Follow active run')).not.toBeChecked();
});

test('a finished run resumes an interrupted trace until the server confirms the end', async ({ page }) => {
  const state = await fixture(page, { terminalReplay: true });
  await page.goto('/');
  await expect(page.locator('.run-summary .status')).toHaveText('passed');
  await expect.poll(() => state.calls.after.includes('1')).toBe(true);
  await expect(page.locator('.event')).toHaveCount(2);
  await expect(page.locator('.inspector .stream-state')).toHaveText('Recorded');
  await expect(page.locator('.event-seq')).toHaveText(['#1', '#2']);
});

test('a delayed history snapshot preserves a newer completed run and a newly queued attempt', async ({ page }) => {
  const state = await fixture(page, { terminalReplay: true });
  await page.goto('/');
  await expect(page.locator('.run-summary .status')).toHaveText('passed');
  state.holdHistory();
  await page.getByRole('button', { name: 'Refresh workspace', exact: true }).click();
  await expect.poll(() => state.historyHeld()).toBe(true);
  await page.getByRole('button', { name: 'Start benchmark', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Inspect run created-run', exact: true })).toBeVisible();
  state.releaseHistory();
  await expect(page.getByRole('button', { name: 'Refresh workspace', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: 'Inspect run created-run', exact: true })).toBeVisible();
  const completed = page.locator('#history tbody tr').filter({ has: page.getByRole('button', { name: 'Inspect run run-alpha', exact: true }) });
  await expect(completed.locator('.status')).toHaveText('passed');
});

test('activity polling never overlaps and stale failures retain the last known run', async ({ page }) => {
  const state = await fixture(page, { slowActivity: true });
  await page.goto('/');
  await expect(page.locator('.monitor-connection')).toHaveText('Connecting');
  state.releaseActivity();
  await expect(page.locator('.monitor-connection')).toHaveText('Live');
  const initialDoctorCalls = state.calls.doctor;
  state.fail();
  await expect(page.locator('.monitor-connection')).toHaveText('Reconnecting');
  await expect(page.locator('.monitor-connection')).toHaveText('Stale · retrying', { timeout: 10_000 });
  await expect(page.locator('.monitor-task')).toContainText('alpha task');
  await expect(page.locator('.monitor-note')).toContainText('last known controller state');
  expect(state.maximumInflight()).toBe(1);
  expect(state.calls.doctor).toBe(initialDoctorCalls);
});
