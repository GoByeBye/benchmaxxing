import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';
import type { Run, RunEvent } from '../src/api';
import { appendSessionEvents, emptySession, SESSION_ITEMS, SESSION_TEXT_LIMIT } from '../src/sessionState';

const now = new Date().toISOString();
const event = (seq: number, kind: string, data: Record<string, unknown>): RunEvent => ({ seq, kind, data, run_id: 'streamed-run', timestamp: now });
const delta = (seq: number, type: string, data: Record<string, unknown>, turn = 1) => event(seq, 'model.delta', { turn, type, ...data });
const response = (seq: number, turn: number, content: string, tool_calls: unknown[] = []) => event(seq, 'model.response', { turn, message: { role: 'assistant', content, tool_calls } });
const toolCall = (id: string, name: string, args: object) => ({ id, type: 'function', function: { name, arguments: JSON.stringify(args) } });

test('provider filtering is visible without labeling ordinary text as a refusal', () => {
  const state = appendSessionEvents(emptySession(), [
    response(1, 1, 'I cannot help with that.'),
    event(2, 'model.filtered', { turn: 1, reason: 'content_filter' }),
  ]);
  expect(state.items[0].refusal).toBe('');
  expect(state.items[1]).toMatchObject({ kind: 'notice', status: 'error', content: 'Provider filtered this response (content_filter).' });
});

async function sessionFixture(page: Page, batches: RunEvent[][]) {
  const releases: (() => void)[] = [];
  const gates = batches.map((_, index) => new Promise<void>(resolve => { releases[index] = resolve; }));
  const requests = { progress: 0, legacy: 0, after: [] as number[] };
  const run: Run = { id: 'streamed-run', status: 'running', spec: { task_id: 'python.stream', participant: 'openai', inference: { model: 'streaming-model', base_url: 'http://localhost:8080/v1' }, budgets: { wall_seconds: 1800 } }, created_at: now, updated_at: now, metadata: { task: { title: 'Streaming code repair', category: 'programming' } } };
  await page.route('**/api/v1/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
    if (path.endsWith('/activity')) { requests.legacy++; return route.abort('blockedbyclient'); }
    if (path.endsWith('/run-progress')) {
      requests.progress++;
      return json({ schema_version: 1, server_time: now, runs: run.status === 'running' ? [{ id: run.id, status: 'running', task_id: run.spec.task_id, task_title: 'Streaming code repair', category: 'programming', participant: 'openai', model: 'streaming-model', created_at: now, updated_at: now, started_at: now, phase: 'inference', turn: 1, max_turns: 24, wall_seconds: 1800, elapsed_seconds: 10, last_event_at: now }] : [] });
    }
    if (path.endsWith('/execution/settings')) return json({ max_parallel_runs: 1 });
    if (path.endsWith('/session')) return json({ token: 'stream-token', version: '0.1.0' });
    if (path.endsWith('/doctor')) return json({ ready: true, checks: {} });
    if (path.endsWith('/tasks')) return json([{ id: 'python.stream', title: 'Streaming code repair', category: 'programming', description: 'Repair the provided implementation.', version: '1.0.0', task_hash: 'stream', image: 'prepared' }]);
    if (path.endsWith('/runs')) return json([run]);
    if (path.endsWith('/events')) {
      const after = Number(url.searchParams.get('after') || 0); requests.after.push(after);
      const index = batches.findIndex(batch => batch.some(item => item.seq > after));
      if (index < 0) return route.fulfill({ contentType: 'text/event-stream', body: 'event: complete\ndata: {}\n\n' });
      if (index) await gates[index];
      const final = index === batches.length - 1;
      if (final) { run.status = 'passed'; run.updated_at = new Date(Date.parse(now) + 1000).toISOString(); }
      const body = batches[index].map(item => `id: ${item.seq}\nevent: run_event\ndata: ${JSON.stringify(item)}\n\n`).join('');
      return route.fulfill({ contentType: 'text/event-stream', body: body + (final ? 'event: complete\ndata: {}\n\n' : '') });
    }
    if (path.endsWith('/streamed-run')) return json({ run, artifacts: [] });
    return json({ detail: 'Not found' }, 404);
  });
  return { requests, release: (index: number) => releases[index]() };
}

test('session streams text, provider reasoning, partial tool arguments and output before final results without duplication', async ({ page }) => {
  const writeArgs = { path: 'solution.py', content: 'def solve(x):\n    return x' };
  const commandArgs = { command: 'python public_tests.py' };
  const batches = [
    [event(1, 'model.request', { turn: 1, messages: ['REQUEST_PROMPT_MUST_NOT_BE_DUPLICATED'] }), delta(2, 'mode', { mode: 'sse' }), delta(3, 'content', { text: 'Inspecting ' }), delta(4, 'reasoning', { text: 'Checking the public input.' })],
    [delta(3, 'content', { text: 'Inspecting ' }), delta(4, 'reasoning', { text: 'Checking the public input.' }), delta(5, 'content', { text: 'the file.' }), delta(6, 'tool_call_delta', { index: 0, field: 'id', text: 'call-write' }), delta(7, 'tool_call_delta', { index: 0, field: 'name', text: 'write_file' }), delta(8, 'tool_call_delta', { index: 0, field: 'arguments', text: '{"path":"solution.py","content":"def solve' })],
    [delta(9, 'tool_call_delta', { index: 0, field: 'arguments', text: '(x):\\n    return x"}' }), response(10, 1, 'Inspecting the file.', [toolCall('call-write', 'write_file', writeArgs)]), event(11, 'tool.started', { turn: 1, tool_call_id: 'call-write', tool: 'write_file', arguments: writeArgs }), event(12, 'tool.result', { turn: 1, tool_call_id: 'call-write', tool: 'write_file', result: { written: 'solution.py' } }), event(13, 'model.request', { turn: 2 }), delta(14, 'mode', { mode: 'sse' }, 2), delta(15, 'content', { text: 'Running checks.' }, 2), response(16, 2, 'Running checks.', [toolCall('call-command', 'execute', commandArgs)]), event(17, 'tool.started', { turn: 2, tool_call_id: 'call-command', tool: 'execute', arguments: commandArgs }), event(18, 'tool.output', { turn: 2, tool_call_id: 'call-command', stream: 'stdout', text: 'first line\n' })],
    [event(19, 'tool.output', { turn: 2, tool_call_id: 'call-command', stream: 'stdout', text: 'second line\n' }), event(20, 'tool.result', { turn: 2, tool_call_id: 'call-command', tool: 'execute', result: { exit_code: 0, stdout: 'first line\nsecond line\n', stderr: '' } }), event(21, 'run.finished', { status: 'passed' })],
  ];
  const state = await sessionFixture(page, batches);
  await page.goto('/');
  const feed = page.locator('.session-feed');
  await expect(feed.locator('.assistant-text')).toHaveText('Inspecting');
  await expect(feed.locator('.provider-reasoning')).toContainText('Provider reasoning · returned by the model');
  await expect(feed).not.toContainText('REQUEST_PROMPT_MUST_NOT_BE_DUPLICATED');
  state.release(1);
  await expect(feed.locator('.assistant-text')).toHaveText('Inspecting the file.');
  await expect(feed.locator('.tool-card')).toContainText('Preparing arguments');
  await expect(feed.locator('.session-path')).toHaveText('solution.py');
  await expect(feed.locator('.session-file-content')).toHaveText('def solve');
  state.release(2);
  await expect(feed.locator('.tool-card')).toHaveCount(2);
  await expect(feed.locator('.session-command')).toContainText('python public_tests.py');
  await expect(feed.locator('.tool-card').last().locator('.session-output')).toContainText('first line');
  await expect(feed.locator('.tool-card').last().locator('.session-card-status')).toHaveText('Running');
  await expect(feed.locator('.assistant-text').first()).toHaveText('Inspecting the file.');
  state.release(3);
  await expect(feed.locator('.tool-card').last().locator('.session-card-status')).toHaveText('Completed');
  await expect(feed.locator('.tool-card').last().locator('.session-output pre')).toHaveText('first line\nsecond line');
  await expect(feed.locator('.session-tool-summary').last()).toHaveText('Exit code 0');
  await expect(feed.locator('.session-notice')).toHaveText('Run passed');
  for (const kind of ['cancelled', 'interrupted', 'infrastructure_error']) {
    expect(appendSessionEvents(emptySession(), [event(1, kind, {})]).items.at(-1)?.content).toBe(`Run ${kind.replaceAll('_', ' ')}`);
  }
  expect(state.requests.after).toContain(4);
  expect(state.requests.legacy).toBe(0);
  expect(state.requests.progress).toBeGreaterThan(0);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
});

test('new progress route works when the legacy activity URL is blocked and buffered generation is explicit', async ({ page }) => {
  const state = await sessionFixture(page, [[event(1, 'model.request', { turn: 1 }), delta(2, 'mode', { mode: 'json' })], [response(3, 1, 'Buffered answer.')]]);
  await page.goto('/');
  await expect(page.locator('.monitor-connection')).toHaveText('Live');
  await expect(page.locator('.session-feed')).toContainText('This endpoint returns a buffered response. Waiting for the full response');
  expect(state.requests.legacy).toBe(0);
  expect(state.requests.progress).toBeGreaterThan(0);
  state.release(1);
  await expect(page.locator('.assistant-text')).toHaveText('Buffered answer.');
});

test('long streamed turns retain their beginning after the raw event window rolls forward', async ({ page }) => {
  const chunks = [event(1, 'model.request', { turn: 1 }), ...Array.from({ length: 500 }, (_, index) => delta(index + 2, 'content', { text: index ? 'x' : 'BEGINNING ' }))];
  const state = await sessionFixture(page, [chunks, [response(502, 1, `BEGINNING ${'x'.repeat(499)}`)]]);
  await page.goto('/');
  await expect(page.locator('.event')).toHaveCount(300);
  await expect(page.locator('.assistant-text')).toHaveText(`BEGINNING ${'x'.repeat(499)}`);
  state.release(1);
  await expect(page.locator('.assistant-text')).toHaveText(`BEGINNING ${'x'.repeat(499)}`);
  const oversized = appendSessionEvents(emptySession(), Array.from({ length: 80 }, (_, index) => response(index + 1, index + 1, 'x'.repeat(20_000))));
  expect(oversized.items).toHaveLength(SESSION_ITEMS);
  expect(oversized.items.every(item => item.content.length === SESSION_TEXT_LIMIT && item.truncated)).toBe(true);
});

test('reading a session pauses its snapshot until jump to live, even when old cards are evicted', async ({ page }) => {
  const initial = Array.from({ length: 12 }, (_, index) => response(index + 1, index + 1, `Message ${index + 1}\n${'Readable evidence. '.repeat(12)}`));
  const next = Array.from({ length: 60 }, (_, index) => response(index + 13, index + 13, `New message ${index + 13}`));
  const state = await sessionFixture(page, [initial, next]);
  await page.goto('/');
  await expect(page.locator('.session-card')).toHaveCount(12);
  await page.locator('.session-items').evaluate(node => { node.scrollTop = 150; });
  await expect(page.locator('.session-reading-note')).toContainText('paused for reading');
  const before = await page.locator('.session-items').evaluate(node => node.scrollTop);
  state.release(1);
  await expect(page.locator('.session-jump')).toContainText('60 new updates');
  await expect(page.locator('.session-card')).toHaveCount(12);
  await expect(page.locator('.assistant-text').first()).toContainText('Message 1');
  expect(await page.locator('.session-items').evaluate(node => node.scrollTop)).toBe(before);
  await page.getByRole('button', { name: '60 new updates · Jump to live session ↓' }).click();
  await expect(page.locator('.session-card')).toHaveCount(SESSION_ITEMS);
  await expect(page.locator('.assistant-text').last()).toHaveText('New message 72');
  await expect(page.locator('.session-jump')).toHaveCount(0);
  expect(await page.locator('.session-items').evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight)).toBeLessThan(35);
});
