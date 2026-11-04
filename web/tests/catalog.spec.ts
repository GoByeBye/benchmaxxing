import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Run, RunSpec, Task } from '../src/api';

const now = '2026-11-04T10:00:00Z';
const task = (id: string, title: string, category: string, extra: Partial<Task> = {}): Task => ({
  id, title, category, version: '1.0.0', description: `Complete ${title}.`, task_hash: id,
  image: 'sha256:prepared', kind: 'python', submission_file: 'solution.py', difficulty: 'easy', tags: [],
  check_count: 5, rubric: `Independently verify ${title}.`, ...extra,
});
const tasks: Task[] = [
  task('python.interval-merge', 'Repair interval merging', 'programming'),
  task('engineering.ohms-law', 'Ohm law circuit', 'engineering', { description: 'Calculate current from voltage and resistance.', tags: ['circuits', 'voltage'], rubric: 'Current must match the reference within numerical tolerance.' }),
  task('engineering.beam', 'Beam deflection', 'engineering', { difficulty: 'medium', check_count: 7, tags: ['mechanics'] }),
  task('security.policy-review', 'Access policy review', 'security', { kind: 'json', submission_file: 'answer.json', rubric: 'Identify every unsafe permission.' }),
  task('terminal.log-summary', 'Log summary', 'terminal'),
  task('refusal.policy-decision', 'Policy decision', 'refusal', { kind: 'json', submission_file: 'answer.json' }),
];

function makeRun(id: string, spec: RunSpec, extra: Partial<Run> = {}): Run {
  return { id, status: 'queued', spec, created_at: now, updated_at: now, metadata: {}, score: null, ...extra };
}

async function mockCatalog(page: Page, options: { initial?: Run[]; failAt?: number } = {}) {
  const runs = [...(options.initial ?? [])];
  const submitted: RunSpec[] = [];
  let inFlight = 0;
  let maxInFlight = 0;
  await page.route('**/api/v1/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/execution/settings')) return json({ max_parallel_runs: 1 });
    if (path.endsWith('/session')) return json({ token: 'catalog-token', version: '0.1.0' });
    if (path.endsWith('/doctor')) return json({ ready: true, checks: {} });
    if (path.endsWith('/tasks')) return json(tasks);
    if (path.endsWith('/runs')) {
      if (request.method() === 'GET') return json(runs);
      inFlight++;
      maxInFlight = Math.max(maxInFlight, inFlight);
      await new Promise(resolve => setTimeout(resolve, 30));
      inFlight--;
      if (options.failAt === submitted.length + 1) return json({ detail: 'Queue temporarily unavailable.' }, 503);
      const spec = request.postDataJSON() as RunSpec;
      submitted.push(spec);
      const run = makeRun(`run-${submitted.length.toString().padStart(4, '0')}`, spec);
      runs.unshift(run);
      return json(run, 201);
    }
    const id = path.split('/')[4];
    const run = runs.find(item => item.id === id);
    if (path.endsWith('/events')) {
      const event = { seq: 1, run_id: id, timestamp: now, kind: 'queued', data: {} };
      return route.fulfill({ contentType: 'text/event-stream', body: `id: 1\nevent: run_event\ndata: ${JSON.stringify(event)}\n\n` });
    }
    if (run) return json({ run, artifacts: [] });
    return json({ detail: 'Not found' }, 404);
  });
  return { submitted, runs, maxInFlight: () => maxInFlight };
}

test('catalog filtering and selection submit the chosen task with its rubric', async ({ page }) => {
  const state = await mockCatalog(page);
  await page.goto('/');
  await page.getByLabel('Category', { exact: true }).selectOption('engineering');
  await expect(page.locator('.catalog-task')).toHaveCount(2);
  await page.getByLabel('Search benchmarks').fill('voltage');
  await expect(page.locator('.catalog-task')).toHaveCount(1);
  await page.getByRole('button', { name: 'Select task Ohm law circuit', exact: true }).click();
  await expect(page.locator('.catalog-detail')).toContainText('Current must match the reference within numerical tolerance.');
  await expect(page.locator('.task-facts')).toContainText('solution.py');
  await page.getByRole('button', { name: 'Start benchmark', exact: true }).click();
  await expect.poll(() => state.submitted.length).toBe(1);
  expect(state.submitted[0].task_id).toBe('engineering.ohms-law');
  await expect(page.locator('.run-task-name')).toContainText('Ohm law circuit');
  await expect(page.locator('#history tbody')).toContainText('Engineering');
});

test('category batches queue each task and repetition serially with the same participant settings', async ({ page }) => {
  const state = await mockCatalog(page);
  await page.goto('/');
  await page.getByRole('button', { name: 'Filter Engineering benchmarks', exact: true }).click();
  await page.getByLabel('Run scope').selectOption('filtered');
  await page.getByLabel('Attempts per task').selectOption('2');
  await page.getByLabel('Participant', { exact: true }).selectOption('openai');
  await page.getByLabel('Model identifier').fill('local-model');
  await page.getByLabel('Max turns').fill('12');
  await page.getByLabel('Time budget').fill('120.5');
  await page.getByRole('button', { name: 'Queue 4 runs', exact: true }).click();
  await expect.poll(() => state.submitted.length).toBe(4);
  expect(state.submitted.map(spec => spec.task_id)).toEqual(['engineering.ohms-law', 'engineering.beam', 'engineering.ohms-law', 'engineering.beam']);
  expect(state.maxInFlight()).toBe(1);
  expect(state.submitted.every(spec => spec.participant === 'openai' && spec.inference?.model === 'local-model' && spec.budgets?.max_turns === 12 && spec.budgets?.wall_seconds === 120.5)).toBe(true);
  await expect(page.locator('.queue-notice')).toContainText('Queued 4 attempts.');
  await expect(page.locator('#history tbody tr')).toHaveCount(4);
  await expect(page.locator('.run-summary-title > .mono')).toHaveText('run-0001');
  await expect(page.getByText('4 queued · 0 running', { exact: true })).toBeVisible();
});

test('a partial batch failure preserves queued attempts and reports the exact count', async ({ page }) => {
  const state = await mockCatalog(page, { failAt: 3 });
  await page.goto('/');
  await page.getByLabel('Category', { exact: true }).selectOption('engineering');
  await page.getByLabel('Run scope').selectOption('filtered');
  await page.getByLabel('Attempts per task').selectOption('2');
  await page.getByRole('button', { name: 'Queue 4 runs', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('Queue temporarily unavailable.');
  await expect(page.locator('.queue-notice')).toContainText('2 of 4 attempts were queued.');
  expect(state.submitted).toHaveLength(2);
  await expect(page.locator('#history tbody tr')).toHaveCount(2);
});

test('malformed settings and empty filters disable submission before any API calls', async ({ page }) => {
  const state = await mockCatalog(page);
  await page.goto('/');
  const start = page.getByRole('button', { name: 'Start benchmark', exact: true });
  await expect(start).toBeEnabled();
  await page.getByLabel('Max turns').fill('257');
  await expect(start).toBeDisabled();
  await page.getByLabel('Max turns').fill('24');
  await page.getByLabel('Time budget').fill('');
  await expect(start).toBeDisabled();
  await page.getByLabel('Time budget').fill('120');
  await page.getByLabel('Participant', { exact: true }).selectOption('openai');
  await expect(start).toBeDisabled();
  await page.getByLabel('Model identifier').fill('local-model');
  await page.getByLabel('Endpoint URL').fill('https://example.invalid/v1');
  await expect(start).toBeDisabled();
  await page.getByLabel('Inference access').selectOption('api');
  await expect(start).toBeEnabled();
  await page.getByLabel('Endpoint URL').fill('not-a-url');
  await expect(start).toBeDisabled();
  await page.getByLabel('Participant', { exact: true }).selectOption('scripted');
  await page.getByLabel('Search benchmarks').fill('no such benchmark');
  await expect(page.getByText('No matching benchmarks', { exact: true })).toBeVisible();
  await expect(start).toBeDisabled();
  expect(state.submitted).toHaveLength(0);
});

test('history preserves recorded task labels and coverage counts unique graded tasks', async ({ page }) => {
  const score = { passed: true, score: 1, checks: [] };
  const initial = [
    makeRun('history-engineering-1', { task_id: 'engineering.beam' }, { status: 'passed', score, metadata: { task: { title: 'Archived beam task', category: 'engineering' } } }),
    makeRun('history-engineering-2', { task_id: 'engineering.beam' }, { status: 'failed', score: { ...score, passed: false, score: 0 } }),
    makeRun('history-engineering-error', { task_id: 'engineering.ohms-law' }, { status: 'infrastructure_error' }),
    makeRun('history-security-1', { task_id: 'security.policy-review' }, { status: 'passed', score }),
  ];
  await mockCatalog(page, { initial });
  await page.goto('/');
  await expect(page.locator('#history tbody')).toContainText('Archived beam task');
  await expect(page.locator('#history tbody')).toContainText('Access policy review');
  await expect(page.locator('#history tbody')).toContainText('Cybersecurity');
  const engineering = page.locator('.coverage-card[data-category="engineering"]');
  await expect(engineering).toContainText('1 / 2 graded tasks');
  await expect(engineering).toContainText('1 passed · 2 graded attempts');
  await expect(page.locator('.coverage-card[data-category="security"]')).toContainText('1 / 1 graded tasks');
  await expect(page.locator('.coverage-heading')).toContainText('Across loaded run history');
});
