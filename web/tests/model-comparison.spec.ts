import { expect, test } from '@playwright/test';
import type { Run } from '../src/api';
import { compareModels, modelCategoryOverview, modelKey } from '../src/comparisonMath';

function run(id: string, model: string, passed = true): Run {
  return { id, status: passed ? 'passed' : 'failed', created_at: id, updated_at: id,
    spec: { participant: 'openai', task_id: 'task', inference: { model, base_url: 'http://localhost/v1', mode: 'offline' }, budgets: { max_turns: 24 }, generation: {} },
    metadata: { harness_version: 'benchmaxxing-tools/3', task: { id: 'task', version: '1', task_hash: 'hash', category: 'programming' } },
    score: { passed, score: passed ? 1 : 0, grader_version: 'grader/1', checks: [] },
  };
}

test('matches strict historical conditions and balances latest attempts', () => {
  const a = run('1', 'A'), latest = run('3', 'A', false), b = run('2', 'B');
  const result = compareModels([a, latest, b], modelKey(a)!, modelKey(b)!);
  expect(result.rows[0].attempts).toEqual([{ count: 1, passed: 0 }, { count: 1, passed: 1 }]);
  expect(result.summary[0].unmatched).toBe(1);
});

test('hash, harness, budget, network, generation, API style and grader differences do not match', () => {
  const a = run('1', 'A');
  const variants = [
    (b: Run) => { (b.metadata!.task as Record<string, unknown>).task_hash = 'other'; },
    (b: Run) => { b.metadata!.harness_version = 'other'; },
    (b: Run) => { b.spec.budgets = { max_turns: 12 }; },
    (b: Run) => { b.metadata!.environment = { external_task_network: true }; },
    (b: Run) => { b.spec.generation = { temperature: 0.8 }; },
    (b: Run) => { b.spec.inference!.api_style = 'responses'; },
    (b: Run) => { b.spec.inference!.responses_reasoning = true; },
    (b: Run) => { b.spec.inference!.vllm_priority = -1; },
    (b: Run) => { b.metadata!.execution = { max_parallel_runs: 2 }; },
    (b: Run) => { b.score!.grader_version = 'grader/2'; },
  ];
  for (const change of variants) {
    const b = run('2', 'B'); change(b);
    const result = compareModels([a, b], modelKey(a)!, modelKey(b)!);
    expect(result.rows.every(row => row.shared === 0)).toBe(true);
  }
});

test('historical serial results match explicit serial execution and unset priority', () => {
  const a = run('1', 'A'), b = run('2', 'B');
  b.metadata!.execution = { max_parallel_runs: 1 };
  b.spec.inference!.vllm_priority = null;
  expect(compareModels([a, b], modelKey(a)!, modelKey(b)!).rows[0].shared).toBe(1);
});

test('keeps ungraded, cancelled, active and missing provenance outside pass rates', () => {
  const a = run('1', 'A'), b = run('2', 'B');
  const ungraded = { ...run('3', 'A'), score: null };
  const cancelled: Run = { ...run('4', 'A'), status: 'cancelled' };
  const review = run('5', 'A'); review.metadata!.assessment_status = 'needs_review';
  const missing = run('6', 'A'); missing.metadata = {};
  const active: Run = { ...run('7', 'A'), status: 'running' };
  const result = compareModels([a, b, ungraded, cancelled, review, missing, active], modelKey(a)!, modelKey(b)!);
  expect(result.rows[0].attempts[0]).toEqual({ passed: 1, count: 1 });
  expect(result.summary[0]).toMatchObject({ ungraded: 2, cancelled: 1, missing: 1, active: 1, unmatched: 0 });
});

test('category overview shows each model score with its own task coverage', () => {
  const a = run('1', 'A'), aFail = run('2', 'A', false), b = run('3', 'B');
  b.spec.task_id = 'another-task';
  (b.metadata!.task as Record<string, unknown>).id = 'another-task';
  const overview = modelCategoryOverview([a, aFail, b]);
  expect(overview).toHaveLength(1);
  expect(overview[0].models).toEqual([
    { key: modelKey(a), passed: 1, graded: 2, tasks: 1, medianSeconds: null, timedPassed: 0 },
    { key: modelKey(b), passed: 1, graded: 1, tasks: 1, medianSeconds: null, timedPassed: 0 },
  ]);
});

test('time to successful completion uses stored elapsed time and excludes failed attempts', () => {
  const a = run('1', 'A'), aFail = run('2', 'A', false), b = run('3', 'B');
  a.metadata!.elapsed_seconds = 10;
  aFail.metadata!.elapsed_seconds = 1;
  b.metadata!.elapsed_seconds = 20;
  const result = compareModels([a, aFail, b], modelKey(a)!, modelKey(b)!);
  expect(result.rows[0].timings).toEqual([{ medianSeconds: null, timedPassed: 0 }, { medianSeconds: 20, timedPassed: 1 }]);
  const overview = modelCategoryOverview([a, aFail, b]);
  expect(overview[0].models[0]).toMatchObject({ passed: 1, graded: 2, medianSeconds: 10, timedPassed: 1 });
});
