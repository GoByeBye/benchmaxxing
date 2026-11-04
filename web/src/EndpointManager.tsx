import { useCallback, useEffect, useState } from 'react';
import type { Session } from './api';
import { mutate, request } from './api';
import './endpointManager.css';

export type EndpointInput = {
  name: string;
  mode: 'offline' | 'api';
  base_url: string;
  api_key_env: string | null;
  api_style: 'chat_completions' | 'responses';
  responses_reasoning: boolean;
  vllm_priority?: number | null;
  model_ids: string[];
};
export type SavedEndpoint = EndpointInput & { id: string };
export type ModelTarget = { endpoint: SavedEndpoint; model: string };

const empty: EndpointInput = {
  name: '', mode: 'offline', base_url: 'http://127.0.0.1:8080/v1',
  api_key_env: null, api_style: 'chat_completions', responses_reasoning: false, vllm_priority: null, model_ids: [],
};

function message(problem: unknown) {
  return problem instanceof Error ? problem.message : 'The endpoint request failed.';
}

export default function EndpointManager({ session, onChange }: {
  session: Session | null;
  onChange: (targets: ModelTarget[]) => void;
}) {
  const [endpoints, setEndpoints] = useState<SavedEndpoint[]>([]);
  const [activeIds, setActiveIds] = useState<string[]>([]);
  const [discovered, setDiscovered] = useState<Record<string, string[]>>({});
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<EndpointInput>(empty);
  const [formOpen, setFormOpen] = useState(false);
  const [manualModel, setManualModel] = useState<Record<string, string>>({});
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    request<SavedEndpoint[]>('/endpoints').then(items => {
      if (cancelled) return;
      setEndpoints(items);
      setActiveIds(current => current.length ? current : items[0] ? [items[0].id] : []);
    }).catch(problem => { if (!cancelled) setError(message(problem)); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    onChange(endpoints.filter(item => activeIds.includes(item.id))
      .flatMap(endpoint => endpoint.model_ids.map(model => ({ endpoint, model }))));
  }, [activeIds, endpoints, onChange]);

  const persist = useCallback(async (value: SavedEndpoint) => {
    if (!session) return;
    const { id, ...input } = value;
    const updated = await request<SavedEndpoint>(`/endpoints/${encodeURIComponent(id)}`, {
      method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'X-Benchmaxxing-Token': session.token },
      body: JSON.stringify(input),
    });
    setEndpoints(items => items.map(item => item.id === updated.id ? updated : item));
  }, [session]);

  async function discover(endpoint: SavedEndpoint, selectAll: boolean) {
    if (!session) return;
    setWorking(true); setError(null);
    try {
      const result = await mutate<{ endpoint_id: string; model_ids: string[] }>(`/endpoints/${encodeURIComponent(endpoint.id)}/models`, session.token);
      setDiscovered(current => ({ ...current, [endpoint.id]: result.model_ids }));
      if (selectAll && result.model_ids.length) await persist({ ...endpoint, model_ids: result.model_ids });
    } catch (problem) { setError(`${endpoint.name}: ${message(problem)}. You can add a model ID manually.`); }
    finally { setWorking(false); }
  }

  async function save() {
    if (!session) return;
    setWorking(true); setError(null);
    try {
      const name = draft.name.trim();
      if (!name) throw new Error('Enter an endpoint name.');
      if (draft.vllm_priority != null && (!Number.isInteger(draft.vllm_priority) || draft.vllm_priority < -2147483648 || draft.vllm_priority > 2147483647)) throw new Error('vLLM priority must be a whole number between -2,147,483,648 and 2,147,483,647.');
      const value: EndpointInput = { name, mode: draft.mode, base_url: draft.base_url.trim(), api_key_env: draft.api_key_env?.trim() || null, api_style: draft.api_style, responses_reasoning: draft.responses_reasoning, vllm_priority: draft.vllm_priority ?? null, model_ids: draft.model_ids };
      const endpoint = editingId
        ? await request<SavedEndpoint>(`/endpoints/${encodeURIComponent(editingId)}`, {
          method: 'PUT', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json', 'X-Benchmaxxing-Token': session.token },
          body: JSON.stringify(value),
        })
        : await mutate<SavedEndpoint>('/endpoints', session.token, value);
      setEndpoints(items => editingId ? items.map(item => item.id === endpoint.id ? endpoint : item) : [...items, endpoint]);
      setActiveIds(ids => ids.includes(endpoint.id) ? ids : [...ids, endpoint.id]);
      setFormOpen(false); setEditingId(null); setDraft(empty);
      await discover(endpoint, !editingId);
    } catch (problem) { setError(message(problem)); }
    finally { setWorking(false); }
  }

  async function remove(endpoint: SavedEndpoint) {
    if (!session) return;
    setWorking(true); setError(null);
    try {
      await request<unknown>(`/endpoints/${encodeURIComponent(endpoint.id)}`, {
        method: 'DELETE', credentials: 'same-origin', headers: { 'X-Benchmaxxing-Token': session.token },
      });
      setEndpoints(items => items.filter(item => item.id !== endpoint.id));
      setActiveIds(ids => ids.filter(id => id !== endpoint.id));
      setDiscovered(current => { const next = { ...current }; delete next[endpoint.id]; return next; });
    } catch (problem) { setError(message(problem)); }
    finally { setWorking(false); }
  }

  async function toggleModel(endpoint: SavedEndpoint, model: string) {
    setWorking(true); setError(null);
    const updated = { ...endpoint, model_ids: endpoint.model_ids.includes(model)
      ? endpoint.model_ids.filter(id => id !== model) : [...endpoint.model_ids, model] };
    setEndpoints(items => items.map(item => item.id === endpoint.id ? updated : item));
    try {
      await persist(updated);
    } catch (problem) { setEndpoints(items => items.map(item => item.id === endpoint.id ? endpoint : item)); setError(message(problem)); }
    finally { setWorking(false); }
  }

  async function addModel(endpoint: SavedEndpoint) {
    const id = (manualModel[endpoint.id] ?? '').trim();
    if (!id || id.length > 500) { setError('Enter a model ID of 1 to 500 characters.'); return; }
    setWorking(true); setError(null);
    try {
      if (!endpoint.model_ids.includes(id)) await persist({ ...endpoint, model_ids: [...endpoint.model_ids, id] });
      setManualModel(current => ({ ...current, [endpoint.id]: '' }));
    } catch (problem) { setError(message(problem)); }
    finally { setWorking(false); }
  }

  return <div className="endpoint-manager" aria-label="Saved model endpoints">
    <div className="endpoint-toolbar"><strong>Saved model endpoints</strong><button className="button button-quiet" type="button" disabled={!session || working} onClick={() => { setEditingId(null); setDraft(empty); setFormOpen(!formOpen); }}>Add endpoint</button></div>
    {endpoints.length === 0 && <p className="field-hint">Add an OpenAI-compatible endpoint, then discover its models. Checked models will be queued for each selected task.</p>}
    {endpoints.map(endpoint => {
      const models = Array.from(new Set([...(discovered[endpoint.id] ?? []), ...endpoint.model_ids])).sort();
      return <div className="endpoint-card" key={endpoint.id}>
        <div className="endpoint-card-top"><label className="endpoint-check"><input type="checkbox" checked={activeIds.includes(endpoint.id)} onChange={event => setActiveIds(ids => event.target.checked ? [...ids, endpoint.id] : ids.filter(id => id !== endpoint.id))} /><span><strong>{endpoint.name}</strong><small>{endpoint.base_url} · {endpoint.api_style === 'responses' ? 'Responses' : 'Chat Completions'}</small></span></label></div>
        <div className="endpoint-actions"><button type="button" disabled={working || !session} onClick={() => void discover(endpoint, false)}>Refresh models</button><button type="button" disabled={working || !session} onClick={() => { setEditingId(endpoint.id); setDraft(endpoint); setFormOpen(true); }}>Edit</button><button type="button" disabled={working || !session} onClick={() => void remove(endpoint)}>Remove</button></div>
        {models.length > 0 ? <div className="endpoint-models" role="group" aria-label={`${endpoint.name} models`}>{models.map(model => <label className="endpoint-model" key={model}><input type="checkbox" disabled={working || !session} checked={endpoint.model_ids.includes(model)} onChange={() => void toggleModel(endpoint, model)} /><span>{model}</span></label>)}</div> : <p className="field-hint">No models selected. Refresh discovery or add a model ID.</p>}
        <div className="endpoint-manual"><input aria-label={`Manual model ID for ${endpoint.name}`} value={manualModel[endpoint.id] ?? ''} onChange={event => setManualModel(current => ({ ...current, [endpoint.id]: event.target.value }))} placeholder="Manual model ID" /><button className="button button-quiet" type="button" disabled={!session || working} onClick={() => void addModel(endpoint)}>Add model</button></div>
      </div>;
    })}
    {formOpen && <div className="endpoint-editor" role="group" aria-label={editingId ? 'Edit endpoint' : 'Add endpoint'}>
      <label htmlFor="saved-endpoint-name">Endpoint name</label><input id="saved-endpoint-name" value={draft.name} onChange={event => setDraft(current => ({ ...current, name: event.target.value }))} placeholder="Local inference server" />
      <label htmlFor="saved-endpoint-url">Server URL</label><input id="saved-endpoint-url" type="url" value={draft.base_url} onChange={event => setDraft(current => ({ ...current, base_url: event.target.value }))} spellCheck={false} />
      <label htmlFor="saved-endpoint-mode">Inference access</label><select id="saved-endpoint-mode" value={draft.mode} onChange={event => setDraft(current => ({ ...current, mode: event.target.value as EndpointInput['mode'] }))}><option value="offline">Offline · local server</option><option value="api">API · external server</option></select>
      <label htmlFor="saved-endpoint-style">API style</label><select id="saved-endpoint-style" value={draft.api_style} onChange={event => setDraft(current => ({ ...current, api_style: event.target.value as EndpointInput['api_style'] }))}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></select>
      {draft.api_style === 'responses' && <label className="endpoint-model endpoint-reasoning"><input type="checkbox" checked={draft.responses_reasoning} onChange={event => setDraft(current => ({ ...current, responses_reasoning: event.target.checked }))} /><span>Request encrypted reasoning replay (supported servers only)</span></label>}
      <label htmlFor="saved-endpoint-key">API key environment variable <span className="optional">optional</span></label><input id="saved-endpoint-key" value={draft.api_key_env ?? ''} onChange={event => setDraft(current => ({ ...current, api_key_env: event.target.value }))} placeholder="BENCHMAXXING_API_KEY" spellCheck={false} />
      <label htmlFor="saved-endpoint-priority">vLLM priority <span className="optional">optional</span></label><input id="saved-endpoint-priority" type="number" min={-2147483648} max={2147483647} step={1} value={draft.vllm_priority ?? ''} onChange={event => setDraft(current => ({ ...current, vllm_priority: event.target.value === '' ? null : Number(event.target.value) }))} placeholder="Server default" />
      <p className="field-hint">Applies to this endpoint’s selected models. Lower numbers run first. Leave blank for other providers; nonzero values require vLLM’s priority scheduling policy.</p>
      <div className="endpoint-editor-actions"><button className="button button-primary" type="button" disabled={!session || working} onClick={() => void save()}>{working ? 'Saving…' : editingId ? 'Save changes' : 'Save and discover models'}</button><button className="button button-quiet" type="button" onClick={() => { setFormOpen(false); setEditingId(null); }}>Cancel</button></div>
      <p className="field-hint">Only the environment variable name is saved. The server address must include its API prefix, such as /v1.</p>
    </div>}
    {error && <p className="endpoint-error" role="alert">{error}</p>}
  </div>;
}
