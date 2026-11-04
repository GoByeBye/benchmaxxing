import type { RunEvent } from './api';

export const SESSION_ITEMS = 48;
export const SESSION_TEXT_LIMIT = 16_000;
export type SessionItem = {
  id: string; kind: 'assistant' | 'tool' | 'notice'; turn: number; seq: number; timestamp: string;
  status: 'generating' | 'assembling' | 'ready' | 'running' | 'complete' | 'error';
  content: string; reasoning: string; refusal: string; mode?: 'sse' | 'json';
  toolId: string; toolIndex: number; name: string; arguments: string; stdout: string; stderr: string;
  summary: string; truncated: boolean;
};
export type SessionState = { items: SessionItem[]; lastSeq: number; updates: number; omitted: number };
export const emptySession = (): SessionState => ({ items: [], lastSeq: 0, updates: 0, omitted: 0 });
const object = (value: unknown): Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value) ? value as Record<string, unknown> : {};
const string = (value: unknown) => typeof value === 'string' ? value : '';
const serialized = (value: unknown) => typeof value === 'string' ? value : value === undefined ? '' : JSON.stringify(value, null, 2);
const number = (value: unknown, fallback = 0) => typeof value === 'number' && Number.isFinite(value) ? value : fallback;
function newItem(event: RunEvent, id: string, kind: SessionItem['kind'], turn: number): SessionItem {
  return { id, kind, turn, seq: event.seq, timestamp: event.timestamp, status: kind === 'assistant' ? 'generating' : 'assembling', content: '', reasoning: '', refusal: '', toolId: '', toolIndex: -1, name: '', arguments: '', stdout: '', stderr: '', summary: '', truncated: false };
}
type TextField = 'content' | 'reasoning' | 'refusal' | 'arguments' | 'stdout' | 'stderr' | 'summary' | 'name' | 'toolId';
function put(item: SessionItem, field: TextField, value: string, append = false) {
  const combined = append ? item[field] + value : value;
  if (combined.length > SESSION_TEXT_LIMIT) item.truncated = true;
  item[field] = combined.slice(0, SESSION_TEXT_LIMIT);
}

/** Process accepted events before raw-trace eviction. Request prompts/tools are deliberately not duplicated. */
export function appendSessionEvents(previous: SessionState, events: RunEvent[]): SessionState {
  let state = previous;
  for (const event of events) {
    if (event.seq <= state.lastSeq) continue;
    const data = object(event.data);
    const turn = number(data.turn);
    const items = [...state.items];
    let changed = false;
    function edit(id: string, kind: SessionItem['kind']) {
      const index = items.findIndex(item => item.id === id);
      const item = index < 0 ? newItem(event, id, kind, turn) : { ...items[index] };
      if (index < 0) items.push(item); else items[index] = item;
      changed = true;
      return item;
    }
    function assistant() { return edit(`assistant:${turn}`, 'assistant'); }
    function tool(id: string, index = -1) {
      const found = items.find(item => item.kind === 'tool' && item.turn === turn && ((id && item.toolId === id) || (index >= 0 && item.toolIndex === index)));
      const item = edit(found?.id ?? `tool:${turn}:${index >= 0 ? index : id || event.seq}`, 'tool');
      if (id) put(item, 'toolId', id);
      if (index >= 0) item.toolIndex = index;
      return item;
    }
    if (event.kind === 'model.request') {
      assistant().status = 'generating';
    } else if (event.kind === 'model.delta') {
      const type = string(data.type);
      if (type === 'content' || type === 'reasoning' || type === 'refusal') {
        const item = assistant(); item.status = 'generating'; put(item, type, string(data.text), true);
      } else if (type === 'mode' && (data.mode === 'sse' || data.mode === 'json')) {
        assistant().mode = data.mode;
      } else if (type === 'tool_call_delta') {
        const item = tool('', number(data.index));
        const field = data.field === 'id' ? 'toolId' : data.field === 'name' ? 'name' : data.field === 'arguments' ? 'arguments' : null;
        if (field) put(item, field, string(data.text), true);
      } else if (type === 'finish') assistant().status = 'complete';
    } else if (event.kind === 'model.response') {
      const message = object(data.message);
      const item = assistant(); item.status = 'complete';
      put(item, 'content', string(message.content));
      if ('refusal' in message) put(item, 'refusal', string(message.refusal));
      if ('reasoning_content' in message || 'reasoning' in message) put(item, 'reasoning', string(message.reasoning_content ?? message.reasoning));
      if (Array.isArray(message.tool_calls)) message.tool_calls.forEach((value, index) => {
        const call = object(value); const fn = object(call.function);
        const action = tool(string(call.id), index);
        put(action, 'name', string(fn.name)); put(action, 'arguments', serialized(fn.arguments));
        if (action.status === 'assembling') action.status = 'ready';
      });
    } else if (event.kind === 'tool.started') {
      const item = tool(string(data.tool_call_id));
      item.status = 'running'; put(item, 'name', string(data.tool));
      if ('arguments' in data) put(item, 'arguments', serialized(data.arguments));
    } else if (event.kind === 'tool.output') {
      const item = tool(string(data.tool_call_id));
      item.status = 'running'; put(item, data.stream === 'stderr' ? 'stderr' : 'stdout', string(data.text), true);
    } else if (event.kind === 'tool.result') {
      const item = tool(string(data.tool_call_id));
      const result = object(data.result);
      put(item, 'name', string(data.tool) || item.name);
      item.status = result.error || (typeof result.exit_code === 'number' && result.exit_code !== 0) ? 'error' : 'complete';
      if ('stdout' in result) put(item, 'stdout', string(result.stdout));
      if ('stderr' in result) put(item, 'stderr', string(result.stderr));
      if ('content' in result) put(item, 'stdout', string(result.content));
      put(item, 'summary', result.error ? string(result.error) : typeof result.exit_code === 'number' ? `Exit code ${result.exit_code}` : result.written ? `Wrote ${string(result.written)}` : result.submitted ? 'Submission received by the harness' : item.name === 'read_file' ? 'File read' : 'Tool completed');
    } else if (['run.finished', 'run.completed', 'cancelled', 'interrupted', 'infrastructure_error'].includes(event.kind)) {
      const item = edit(`finish:${event.seq}`, 'notice');
      const outcome = string(data.status) || (event.kind.startsWith('run.') ? 'finished' : event.kind);
      item.status = outcome === 'passed' ? 'complete' : outcome === 'failed' || outcome === 'infrastructure_error' || data.error ? 'error' : 'ready';
      const explanation = string(data.error) || string(data.reason).replaceAll('_', ' ');
      put(item, 'content', `Run ${outcome.replaceAll('_', ' ')}${explanation ? `: ${explanation}` : ''}`);
    }
    const removed = Math.max(0, items.length - SESSION_ITEMS);
    state = { items: changed ? items.slice(-SESSION_ITEMS) : state.items, lastSeq: event.seq, updates: state.updates + (changed ? 1 : 0), omitted: state.omitted + removed };
  }
  return state;
}

/** Read a useful string field even while streamed JSON arguments are incomplete. */
export function argumentText(raw: string, field: 'path' | 'command' | 'content'): string {
  try { const value = object(JSON.parse(raw))[field]; return string(value); } catch { /* The provider may still be producing the JSON string. */ }
  const match = raw.match(new RegExp(`"${field}"\\s*:\\s*"((?:\\\\.|[^"\\\\])*)`));
  if (!match) return '';
  try { return JSON.parse(`"${match[1]}"`) as string; } catch { return match[1].replaceAll('\\n', '\n').replaceAll('\\t', '\t').replaceAll('\\"', '"').replaceAll('\\\\', '\\'); }
}
