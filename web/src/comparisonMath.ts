import type { Run } from './api';

const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const string = (value: unknown) => typeof value === 'string' && value.length ? value : null;
export function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${stable(item)}`).join(',')}}`;
  return JSON.stringify(value) ?? 'null';
}

export function modelKey(run: Run): string | null {
  const inference = run.spec.inference;
  return run.spec.participant === 'openai' && inference?.model && inference.base_url
    ? stable([inference.model, inference.base_url, inference.mode ?? 'offline', inference.api_style ?? 'chat_completions', inference.responses_reasoning ?? false]) : null;
}

export function comparisonIdentity(run: Run) {
  const metadata = run.metadata ?? {};
  const task = record(metadata.task);
  const environment = record(metadata.environment);
  const execution = record(metadata.execution);
  const harness = string(metadata.harness_version);
  // These versioned harnesses enforce task internet isolation in their implementation.
  // Unknown harnesses must supply explicit network evidence rather than inheriting it.
  const network = typeof environment.external_task_network === 'boolean' ? environment.external_task_network
    : ['benchmaxxing-tools/3', 'benchmaxxing-imports/1', 'benchmaxxing-answer/1'].includes(harness ?? '') ? false : null;
  if (!string(task.id) || !string(task.version) || !string(task.task_hash) || !string(task.category)
    || task.id !== run.spec.task_id || !harness || network === null || !run.spec.budgets
    || !Object.keys(run.spec.budgets).length || !run.spec.generation || !string(run.score?.grader_version)) return null;
  const category = task.category as string;
  return { category, taskId: task.id as string, key: stable({
    task: [task.id, task.version, task.task_hash, category], harness,
    framework: metadata.framework_version ?? null, image: environment.image_id ?? task.image ?? null,
    budgets: run.spec.budgets, generation: run.spec.generation, network,
    inference: [run.spec.inference?.mode ?? 'offline', run.spec.inference?.api_style ?? 'chat_completions', run.spec.inference?.responses_reasoning ?? false, run.spec.inference?.vllm_priority ?? null],
    maxParallelRuns: execution.max_parallel_runs ?? 1,
    grader: run.score?.grader_version,
    execution: metadata.execution_profile ?? task.execution_profile ?? null,
  }) };
}

export function outcome(run: Run) {
  if (run.status === 'cancelled') return 'cancelled';
  if (run.status === 'queued' || run.status === 'running') return 'active';
  if (['needs_review', 'ungraded'].includes(String(run.metadata?.assessment_status ?? ''))) return 'ungraded';
  return ['passed', 'failed', 'completed'].includes(run.status) && typeof run.score?.passed === 'boolean' ? 'graded' : 'ungraded';
}

export function elapsedSeconds(run: Run): number | null {
  const value = run.metadata?.elapsed_seconds;
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function median(values: number[]): number | null {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
}

export function modelCategoryOverview(runs: Run[]) {
  const categories = new Map<string, Map<string, { passed: number; graded: number; tasks: Set<string>; times: number[] }>>();
  for (const run of runs) {
    const key = modelKey(run);
    if (!key || outcome(run) !== 'graded') continue;
    const identity = comparisonIdentity(run);
    if (!identity) continue;
    const models = categories.get(identity.category) ?? new Map();
    const current = models.get(key) ?? { passed: 0, graded: 0, tasks: new Set<string>(), times: [] };
    current.graded++;
    if (run.score?.passed === true) {
      current.passed++;
      const seconds = elapsedSeconds(run);
      if (seconds !== null) current.times.push(seconds);
    }
    current.tasks.add(identity.taskId);
    models.set(key, current);
    categories.set(identity.category, models);
  }
  return [...categories].sort(([a], [b]) => a.localeCompare(b)).map(([category, models]) => ({
    category,
    models: [...models].sort(([a], [b]) => a.localeCompare(b)).map(([key, data]) => ({
      key, passed: data.passed, graded: data.graded, tasks: data.tasks.size,
      medianSeconds: median(data.times), timedPassed: data.times.length,
    })),
  }));
}

export function compareModels(runs: Run[], left: string, right: string) {
  const selected = [left, right].map(key => runs.filter(run => modelKey(run) === key));
  const groups = selected.map(items => {
    const result = new Map<string, { category: string; taskId: string; runs: Run[] }>();
    for (const run of items) {
      if (outcome(run) !== 'graded') continue;
      const identity = comparisonIdentity(run);
      if (!identity) continue;
      const group = result.get(identity.key) ?? { category: identity.category, taskId: identity.taskId, runs: [] };
      group.runs.push(run); result.set(identity.key, group);
    }
    return result;
  });
  const categories = [...new Set([...groups[0].values(), ...groups[1].values()].map(group => group.category))].sort();
  const rows = categories.map(category => {
    const union = new Set([...groups[0], ...groups[1]].filter(([, group]) => group.category === category).map(([key]) => key));
    const shared = [...union].filter(key => groups[0].has(key) && groups[1].has(key));
    const attempts: Run[][] = [[], []];
    for (const key of shared) {
      const count = Math.min(groups[0].get(key)!.runs.length, groups[1].get(key)!.runs.length);
      groups.forEach((group, index) => attempts[index].push(...group.get(key)!.runs.slice().sort((a, b) => b.created_at.localeCompare(a.created_at) || a.id.localeCompare(b.id)).slice(0, count)));
    }
    return {
      category, shared: shared.length, available: union.size,
      attempts: attempts.map(items => ({ count: items.length, passed: items.filter(run => run.score?.passed === true).length })),
      timings: attempts.map(items => {
        const values = items.filter(run => run.score?.passed === true).map(elapsedSeconds).filter((value): value is number => value !== null);
        return { medianSeconds: median(values), timedPassed: values.length };
      }),
    };
  });
  const summary = selected.map((items, index) => ({
    total: items.length,
    cancelled: items.filter(run => outcome(run) === 'cancelled').length,
    active: items.filter(run => outcome(run) === 'active').length,
    ungraded: items.filter(run => outcome(run) === 'ungraded').length,
    missing: items.filter(run => outcome(run) === 'graded' && !comparisonIdentity(run)).length,
    unmatched: items.filter(run => outcome(run) === 'graded' && comparisonIdentity(run)).length - rows.reduce((sum, row) => sum + row.attempts[index].count, 0),
  }));
  return { rows, summary };
}
