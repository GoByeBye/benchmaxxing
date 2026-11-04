import type { Run } from './api';

// The controller uses canonical UTC timestamps; the string tie-break preserves sub-millisecond precision.
const compareTime = (a: string, b: string) => Date.parse(a) - Date.parse(b) || a.localeCompare(b);

/** Keep newer local evidence when a previously requested history snapshot arrives late. */
export function mergeRunHistory(incoming: Run[], current: Run[]): Run[] {
  const merged = new Map(incoming.map(run => [run.id, run]));
  for (const run of current) {
    const other = merged.get(run.id);
    if (!other || compareTime(run.updated_at, other.updated_at) > 0) merged.set(run.id, run);
  }
  return [...merged.values()].sort((a, b) => compareTime(b.created_at, a.created_at)).slice(0, 1000);
}
