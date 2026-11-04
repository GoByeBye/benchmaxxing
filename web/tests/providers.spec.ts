import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Artifact, PreparationJob, Run, RunSpec, SuiteStatus, Task } from '../src/api';

const now = '2026-11-04T10:00:00Z';
const original: Task = { id: 'python.interval-merge', title: 'Repair interval merging', version: '1.0.0', category: 'programming', description: 'Repair intervals.', task_hash: 'original', image: 'sha256:original', kind: 'python' };
const native = (id: string, prepared = true): Task => ({ id, title: `Native task ${id}`, version: '1', category: 'frontend', description: 'Implement the specified frontend interface.', task_hash: id, image: 'sha256:frontend', kind: 'native', suite_id: 'frontend-design', upstream_id: id, upstream_revision: 'pinned-source', variant: 'offline-30m', prepared, submission_file: 'index.html', difficulty: 'hard', rubric: 'Independent functional checks; aesthetic review remains separate.' });
const suite = (extra: Partial<SuiteStatus> = {}): SuiteStatus => ({ suite: { id: 'frontend-design', name: 'Frontend Design', category: 'frontend', upstream_url: 'https://github.com/GoByeBye/benchmaxxing', version: '1', published_size: '6 original frontend briefs', description: 'Original frontend design tasks with browser grading.', license: 'Upstream terms', offline: 'supported-path', requirements: ['Prepared browser runtime and private functional checks'] }, state: 'partial', imported_tasks: 3, prepared_tasks: 2, source_bytes: 1200000, revisions: ['pinned-source'], blockers: ['One task is missing its prepared browser runtime.'], ...extra });
const completed = (id: string, review: boolean): Run => ({ id, created_at: now, updated_at: now, status: 'completed', spec: { task_id: 'native-1', participant: 'openai', inference: { model: 'local-model' } }, score: review ? null : { score: .72, passed: null, grader_version: 'native-iou/1', checks: [] }, metadata: review ? { assessment_status: 'needs_review' } : {} });

async function install(page: Page, options: { tasks?: Task[]; runs?: Run[]; finishJob?: boolean; suiteData?: SuiteStatus | SuiteStatus[]; artifacts?: Artifact[] } = {}) {
  let tasks = options.tasks ?? [original, native('native-1'), native('native-2', false), native('native-3')];
  const runs = options.runs ?? [];
  let jobs: PreparationJob[] = [];
  let polls = 0;
  const posted: { path: string; body: Record<string, unknown>; token: string | undefined }[] = [];
  await page.route('**/api/v1/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    if (path.endsWith('/execution/settings')) return json({ max_parallel_runs: 1 });
    if (path.endsWith('/session')) return json({ token: 'provider-token', version: '0.1' });
    if (path.endsWith('/doctor')) return json({ ready: true, checks: {} });
    if (path.endsWith('/run-progress')) return json({ schema_version: 1, server_time: now, runs: [] });
    if (path.endsWith('/tasks')) return json(tasks);
    if (path.endsWith('/providers')) return json(Array.isArray(options.suiteData) ? options.suiteData : [options.suiteData ?? suite({ imported_tasks: tasks.filter(task => task.suite_id).length })]);
    if (path.endsWith('/providers/settings')) {
      if (request.method() === 'PUT') { posted.push({ path, body: request.postDataJSON(), token: request.headers()['x-benchmaxxing-token'] }); return json(request.postDataJSON()); }
      return json({ schema_version: 1, storage_limit_bytes: 250e9, reserve_free_bytes: 10e9, cache_dir: 'D:\\BenchmaxxingCache' });
    }
    if (path.endsWith('/providers/jobs')) {
      if (jobs.length && options.finishJob && ++polls >= 2 && jobs[0].status === 'queued') {
        jobs = [{ ...jobs[0], status: 'completed', result: jobs[0].operation === 'prepare' ? { prepared_tasks: 1, task_ids: ['native-1'], errors: [{ task_id: 'native-2', error: 'Missing browser dependency' }] } : { imported_tasks: 4 } }];
        if (jobs[0].operation !== 'prepare') tasks = [...tasks, native('native-imported', false)];
      }
      return json(jobs);
    }
    if (path.endsWith('/providers/import') || path.endsWith('/providers/prepare') || path.endsWith('/providers/fetch')) {
      posted.push({ path, body: request.postDataJSON(), token: request.headers()['x-benchmaxxing-token'] });
      const job: PreparationJob = { id: 'job-1', suite_id: 'frontend-design', operation: path.endsWith('/fetch') ? 'fetch' : path.endsWith('/import') ? 'import' : 'prepare', status: 'queued', created_at: now, updated_at: now, result: path.endsWith('/prepare') ? { generated_records: 3, total_records: 17900, failed_records: 1, current_record: 'sample-part' } : null };
      jobs = [job]; return json(job, 202);
    }
    if (path.endsWith('/runs')) {
      if (request.method() === 'POST') {
        const spec = request.postDataJSON() as RunSpec; posted.push({ path, body: spec, token: request.headers()['x-benchmaxxing-token'] });
        const run: Run = { id: `new-${runs.length}`, status: 'queued', created_at: now, updated_at: now, spec, metadata: {} }; runs.unshift(run); return json(run, 201);
      }
      return json(runs);
    }
    if (path.endsWith('/events')) return route.fulfill({ contentType: 'text/event-stream', body: 'event: complete\ndata: {}\n\n' });
    const run = runs.find(item => path.endsWith(`/runs/${item.id}`));
    if (run) return json({ run, artifacts: options.artifacts ?? [] });
    return json({ detail: 'Not found' }, 404);
  });
  return { posted };
}

test('suite counts stay distinct and browse configures only prepared native tasks', async ({ page }) => {
  const state = await install(page); await page.goto('/');
  const library = page.locator('#providers');
  await expect(library.locator('.provider-facts')).toContainText('6 original frontend briefs');
  await expect(library.locator('.provider-facts')).toContainText('Installed locally3 tasks');
  await expect(library.locator('.provider-facts')).toContainText('Prepared to run2 tasks');
  await expect(library).toContainText('One task is missing its prepared browser runtime.');
  await library.getByRole('button', { name: /Browse 3 imported tasks/ }).click();
  await expect(page.getByLabel('Task suite')).toHaveValue('frontend-design');
  await expect(page.locator('.catalog-task')).toHaveCount(3);
  await expect(page.getByLabel('Participant', { exact: true })).toHaveValue('openai');
  await page.getByLabel('Model identifier').fill('local-frontend-model');
  await page.getByRole('button', { name: 'Select task Native task native-2', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Start benchmark', exact: true })).toBeDisabled();
  await expect(page.locator('.readiness-hint')).toContainText('Prepare the imported suite');
  await page.getByRole('button', { name: 'Select task Native task native-1', exact: true }).click();
  await page.getByRole('button', { name: 'Start benchmark', exact: true }).click();
  await expect.poll(() => state.posted.length).toBe(1);
  expect(state.posted[0]).toMatchObject({ token: 'provider-token', body: { task_id: 'native-1', participant: 'openai', budgets: { wall_seconds: 1800, memory_mb: 2048, pids: 256, command_seconds: 120, artifact_bytes: 67108864 } } });
});

test('local imports poll recorded jobs and refresh installed tasks after completion', async ({ page }) => {
  const state = await install(page, { finishJob: true }); await page.goto('/');
  await page.getByText('Import local source or prepare a runtime', { exact: true }).click();
  await page.getByLabel('Local source path').fill('D:\\BenchmaxxingCache\\sources\\frontend-design');
  await page.getByLabel('Source revision', { exact: true }).fill('source-commit');
  await page.getByLabel('Variant identity').fill('local-offline-v1');
  await page.getByRole('button', { name: 'Import snapshot', exact: true }).click();
  await expect(page.locator('.provider-job')).toContainText('In progress');
  await expect(page.locator('.provider-job')).toContainText('4 tasks imported.');
  await expect(page.getByRole('button', { name: /Browse 4 imported tasks/ })).toBeEnabled();
  expect(state.posted[0]).toMatchObject({ token: 'provider-token', body: { suite_id: 'frontend-design', source_path: 'D:\\BenchmaxxingCache\\sources\\frontend-design', revision: 'source-commit', variant: 'local-offline-v1' } });
});

test('explicit preparation keeps pulls off and exposes partial job errors', async ({ page }) => {
  const state = await install(page, { finishJob: true }); await page.goto('/');
  await page.getByText('Import local source or prepare a runtime', { exact: true }).click();
  await page.getByLabel('Runtime image', { exact: true }).fill('benchmaxxing/frontend:1');
  await page.getByRole('button', { name: 'Prepare suite', exact: true }).click();
  await expect(page.locator('.provider-job')).toContainText('3 / 17,900 canonical models generated · 1 failed.');
  await expect(page.locator('.provider-job')).toContainText('Current model: sample-part');
  await expect(page.locator('.provider-job')).toContainText('Review errors');
  await expect(page.locator('.provider-job')).toContainText('1 tasks prepared.');
  await page.getByText('View job evidence', { exact: true }).click();
  await expect(page.locator('.provider-job pre')).toContainText('Missing browser dependency');
  expect(state.posted[0]).toMatchObject({ token: 'provider-token', body: { suite_id: 'frontend-design', image: 'benchmaxxing/frontend:1', pull: false } });
});

test('storage settings cannot exceed the authorized total and preserve reserve', async ({ page }) => {
  const state = await install(page); await page.goto('/');
  await page.getByText('Preparation storage · 250 GB total ceiling', { exact: true }).click();
  await expect(page.getByLabel('Preparation cache directory', { exact: true })).toBeDisabled();
  await page.getByLabel('Total storage ceiling (GB)', { exact: true }).fill('251');
  await expect(page.getByRole('button', { name: 'Save storage settings' })).toBeDisabled();
  await page.getByLabel('Total storage ceiling (GB)', { exact: true }).fill('200');
  await page.getByRole('button', { name: 'Save storage settings' }).click();
  await expect(page.getByText('Preparation storage settings saved.', { exact: true })).toBeVisible();
  expect(state.posted[0]).toMatchObject({ token: 'provider-token', body: { storage_limit_bytes: 200e9, reserve_free_bytes: 10e9 } });
});

test('continuous scores and review-required responses never receive a false pass or fail', async ({ page }) => {
  await install(page, { runs: [completed('score-run', false), completed('review-run', true)] }); await page.goto('/');
  await page.getByRole('tab', { name: 'Grading', exact: true }).click();
  await expect(page.locator('.score-summary .status')).toHaveText('scored');
  await expect(page.locator('.score-summary')).toContainText('72%');
  await expect(page.locator('.score-summary .status-failed')).toHaveCount(0);
  await page.getByLabel('Inspect run review-run', { exact: true }).click();
  await page.getByRole('tab', { name: 'Grading', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Assessment needs review' })).toBeVisible();
  await expect(page.locator('#history tbody')).toContainText('Needs review');
  await expect(page.locator('#history .status-failed, #history .status-passed')).toHaveCount(0);
});

test('large installed catalogs bound rendered rows and search across the full inventory', async ({ page }) => {
  await install(page, { tasks: Array.from({ length: 105 }, (_, index) => native(`record-${index}`)) }); await page.goto('/');
  await expect(page.locator('.catalog-task')).toHaveCount(40);
  await page.getByLabel('Next task page', { exact: true }).click();
  await expect(page.locator('.catalog-pagination')).toContainText('41–80 of 105');
  await page.getByLabel('Search benchmarks').fill('record-104');
  await expect(page.locator('.catalog-task')).toHaveCount(1);
  await expect(page.locator('.catalog-detail h3')).toHaveText('Native task record-104');
});


test('explicit full-source preparation imports the pinned release through a recorded job', async ({ page }) => {
  const state = await install(page, { finishJob: true }); await page.goto('/');
  await page.getByRole('button', { name: 'Fetch full source', exact: true }).click();
  await expect(page.locator('.provider-job')).toContainText('In progress');
  await expect(page.locator('.provider-job')).toContainText('4 tasks imported.');
  await expect(page.getByRole('button', { name: /Browse 4 imported tasks/ })).toBeEnabled();
  expect(state.posted[0]).toMatchObject({ path: '/api/v1/providers/fetch', token: 'provider-token', body: { suite_id: 'frontend-design' } });
});


test('blank runtime image explicitly requests the reviewed preparation recipe', async ({ page }) => {
  const state = await install(page, { finishJob: true }); await page.goto('/');
  await page.getByText('Import local source or prepare a runtime', { exact: true }).click();
  await expect(page.getByLabel('Runtime image', { exact: true })).toHaveValue('');
  await page.getByRole('button', { name: 'Prepare suite', exact: true }).click();
  await expect.poll(() => state.posted.length).toBe(1);
  expect(state.posted[0]).toMatchObject({ token: 'provider-token', body: { suite_id: 'frontend-design', image: '', pull: false } });
});

test('removed provider cards stay hidden with old controller responses while run evidence remains accessible', async ({ page }) => {
  const legacy: Task = { ...native('legacy-task'), suite_id: 'benchcad', category: 'cad', title: 'Historical task' };
  const descriptor = { ...suite().suite, id: 'benchcad', name: 'Older provider', category: 'cad', active: false };
  const run: Run = { ...completed('legacy-run', false), spec: { task_id: legacy.id, participant: 'openai' }, metadata: { task: { title: legacy.title, category: legacy.category } } };
  await install(page, { tasks: [original, legacy], runs: [run], suiteData: [suite(), suite({ suite: descriptor })] });
  await page.goto('/');
  await expect(page.locator('.catalog-task')).toHaveCount(1);
  await expect(page.locator('.coverage-card[data-category="cad"]')).toHaveCount(0);
  await page.getByLabel('Show inactive suites and tasks').check();
  await expect(page.locator('.catalog-task')).toHaveCount(1);
  await expect(page.locator('.provider-option')).toHaveCount(1);
  await expect(page.locator('.provider-library')).not.toContainText('Older provider');
  await expect(page.locator('#history')).toContainText('Historical task');
  await page.getByLabel('Inspect run legacy-run', { exact: true }).click();
  await expect(page.locator('#history')).toContainText('Historical task');
});

test('knowledge and retrieval profiles identify tool-free inputs', async ({ page }) => {
  const knowledge: Task = { ...original, id: 'knowledge.geography', title: 'Stable geography facts', category: 'knowledge', kind: 'answer', execution_profile: 'closed-book' };
  const retrieval: Task = { ...original, id: 'retrieval.middle', title: 'Retrieve the middle needle', category: 'retrieval', kind: 'answer', execution_profile: 'context-retrieval' };
  await install(page, { tasks: [knowledge, retrieval] }); await page.goto('/');
  await expect(page.locator('.catalog-detail')).toContainText('Closed-book · no tools');
  await expect(page.locator('.catalog-detail')).toContainText('JSON response');
  await page.getByRole('button', { name: 'Select task Retrieve the middle needle', exact: true }).click();
  await expect(page.locator('.catalog-detail')).toContainText('Supplied context · no tools');
  await expect(page.locator('.catalog-detail')).toContainText('Direct structured answer');
});

test('recorded PNG evidence previews through its artifact ID with separate visual review', async ({ page }) => {
  const run = completed('frontend-run', false);
  const image: Artifact = { id: 'recorded-png', run_id: run.id, name: 'frontend-390.png', media_type: 'image/png', size: 64, sha256: 'a'.repeat(64) };
  await install(page, { runs: [run], artifacts: [image] });
  await page.route('**/artifacts/recorded-png', route => route.fulfill({ contentType: 'image/png', body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aVRkAAAAASUVORK5CYII=', 'base64') }));
  await page.goto('/');
  await page.getByRole('tab', { name: /^Artifacts/ }).click();
  await page.getByRole('button', { name: /frontend-390.png/ }).click();
  const preview = page.getByRole('img', { name: 'Recorded benchmark screenshot: frontend-390.png', exact: true });
  await expect(preview).toBeVisible();
  await expect(preview).toHaveAttribute('src', '/api/v1/runs/frontend-run/artifacts/recorded-png');
  await expect(page.locator('.recorded-image')).toContainText('Visual design quality requires a separate review');
});

test('original frontend briefs install without an upstream dataset claim', async ({ page }) => {
  const descriptor = { ...suite().suite, origin: 'original' as const, license: 'AGPL-3.0-only' };
  const state = await install(page, { suiteData: suite({ suite: descriptor }) });
  await page.goto('/');
  await expect(page.locator('.provider-title')).toContainText('Benchmaxxing original');
  await expect(page.locator('.provider-facts')).toContainText('Original suite');
  await page.getByRole('button', { name: 'Install original briefs', exact: true }).click();
  await expect.poll(() => state.posted.length).toBe(1);
  expect(state.posted[0]).toMatchObject({ path: '/api/v1/providers/fetch', body: { suite_id: 'frontend-design' } });
});
