import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { mutate, request, statusLabel } from './api';
import type { ImportSettings, PreparationJob, Session, SuiteStatus } from './api';
import { categoryLabel, removedCategories } from './catalog';

const pending = (job: PreparationJob) => job.status === 'queued' || job.status === 'running';
const message = (error: unknown) => error instanceof Error ? error.message : 'Unable to reach the provider library.';
const bytes = (count: number) => count >= 1e9 ? `${(count / 1e9).toFixed(2)} GB` : `${(count / 1e6).toFixed(1)} MB`;
const stateNames: Record<string, string> = { not_installed: 'Not imported', imported: 'Imported', prepared: 'Prepared', partial: 'Partly prepared', blocked: 'Blocked' };

export default function ProviderCatalog({ session, onBrowse, onChanged, includeArchived, onArchived }: {
  session: Session | null; onBrowse: (suiteId: string) => void; onChanged: () => void;
  includeArchived: boolean; onArchived: (include: boolean) => void;
}) {
  const [providers, setProviders] = useState<SuiteStatus[]>([]);
  const [settings, setSettings] = useState<ImportSettings | null>(null);
  const [jobs, setJobs] = useState<PreparationJob[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [action, setAction] = useState(false);
  const [source, setSource] = useState('');
  const [revision, setRevision] = useState('');
  const [variant, setVariant] = useState('');
  const [image, setImage] = useState('');
  const [pull, setPull] = useState(false);
  const [cache, setCache] = useState('');
  const [cap, setCap] = useState('250');
  const [notice, setNotice] = useState('');
  const jobsRef = useRef(jobs); jobsRef.current = jobs;
  const onChangedRef = useRef(onChanged); onChangedRef.current = onChanged;
  const available = providers.filter(item => !removedCategories.has(item.suite.category) && (includeArchived || item.suite.active !== false));
  const selected = available.find(item => item.suite.id === selectedId) ?? available[0];
  const archived = selected?.suite.active === false;
  const original = selected?.suite.origin === 'original';
  const visible = available.filter(item => `${item.suite.name} ${item.suite.category} ${item.suite.description}`.toLowerCase().includes(search.trim().toLowerCase()));
  const visibleJobs = jobs.filter(job => available.some(item => item.suite.id === job.suite_id));
  const busy = visibleJobs.some(job => job.suite_id === selected?.suite.id && pending(job));
  const hasPending = visibleJobs.some(pending);
  const cacheLocked = providers.some(item => (item.imported_tasks ?? 0) > 0);

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const results = await Promise.allSettled([
      request<SuiteStatus[]>(includeArchived ? '/providers?include_archived=true' : '/providers', { signal }),
      request<ImportSettings>('/providers/settings', { signal }),
      request<PreparationJob[]>('/providers/jobs', { signal }),
    ]);
    if (signal?.aborted) return;
    if (results[0].status === 'fulfilled') { const providers = results[0].value; setProviders(providers); setSelectedId(current => current ?? providers.find(item => !removedCategories.has(item.suite.category))?.suite.id ?? null); }
    if (results[1].status === 'fulfilled') {
      const value = results[1].value; setSettings(value); setCache(value.cache_dir ?? ''); setCap(String((value.storage_limit_bytes ?? 250e9) / 1e9));
    }
    if (results[2].status === 'fulfilled') setJobs(results[2].value);
    const failure = results.find(result => result.status === 'rejected');
    setError(failure?.status === 'rejected' ? message(failure.reason) : '');
    setLoading(false);
  }, [includeArchived]);

  useEffect(() => { const controller = new AbortController(); void refresh(controller.signal); return () => controller.abort(); }, [refresh]);
  useEffect(() => { setSource(''); setRevision(''); setVariant(''); setImage(''); setPull(false); setNotice(''); }, [selectedId]);
  useEffect(() => {
    if (!hasPending) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function poll() {
      try {
        const next = await request<PreparationJob[]>('/providers/jobs', { signal: controller.signal });
        if (controller.signal.aborted) return;
        const finished = next.some(job => !pending(job) && jobsRef.current.some(old => old.id === job.id && pending(old)));
        if (finished) { onChangedRef.current(); await refresh(controller.signal); }
        else setJobs(next);
      } catch (problem) { if (!controller.signal.aborted) setError(message(problem)); }
      finally { if (!controller.signal.aborted) timer = setTimeout(() => void poll(), 1200); }
    }
    timer = setTimeout(() => void poll(), 600);
    return () => { controller.abort(); if (timer) clearTimeout(timer); };
  }, [hasPending, refresh]);

  async function submitJob(event: FormEvent, operation: 'import' | 'prepare' | 'fetch') {
    event.preventDefault();
    if (!session || !selected || action || busy || archived) return;
    setAction(true); setError(''); setNotice('');
    try {
      const body = operation === 'fetch' ? { suite_id: selected.suite.id } : operation === 'import'
        ? { suite_id: selected.suite.id, source_path: source.trim(), revision: revision.trim(), variant: variant.trim() || null }
        : { suite_id: selected.suite.id, image: image.trim(), pull };
      const job = await mutate<PreparationJob>(`/providers/${operation}`, session.token, body);
      setJobs(current => [job, ...current.filter(item => item.id !== job.id)]);
      setNotice(`${operation === 'fetch' ? 'Source preparation' : operation === 'import' ? 'Import' : 'Preparation'} ${statusLabel(job.status)}. Progress is recorded below.`);
      if (!pending(job)) { await refresh(); onChangedRef.current(); }
    } catch (problem) { setError(message(problem)); }
    finally { setAction(false); }
  }

  async function saveSettings(event: FormEvent) {
    event.preventDefault();
    if (!session || !settings || !Number.isFinite(Number(cap)) || Number(cap) < 1 || Number(cap) > 250) return;
    setAction(true); setError('');
    try {
      const result = await request<ImportSettings>('/providers/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Benchmaxxing-Token': session.token },
        body: JSON.stringify({ ...settings, cache_dir: cache.trim() || null, storage_limit_bytes: Math.round(Number(cap) * 1e9) }),
      });
      setSettings(result); setNotice('Preparation storage settings saved.');
    } catch (problem) { setError(message(problem)); }
    finally { setAction(false); }
  }

  return <section className="panel provider-library" id="providers" aria-labelledby="providers-heading">
    <div className="panel-heading"><div><span className="eyebrow">BENCHMARK LIBRARY</span><h2 id="providers-heading">Benchmark suites</h2></div><span className="history-count mono">{available.length} suites</span></div>
    <div className="provider-intro"><p>Software, cybersecurity, frontend design and browser benchmarks alongside mathematical engineering, offline knowledge and context retrieval.</p><button className="button button-quiet" onClick={() => void refresh()} disabled={action}>Refresh suites</button></div>
    <label className="provider-checkbox archive-toggle"><input type="checkbox" checked={includeArchived} onChange={event => onArchived(event.target.checked)} />Show inactive suites and tasks</label>
    {error && <div className="provider-error" role="status"><strong>Provider library needs attention</strong><p>{error}</p></div>}
    {loading ? <p className="provider-placeholder">Loading the local suite registry…</p> : available.length ? <>
      <div className="provider-browser"><div className="provider-sidebar"><label htmlFor="provider-search">Find a suite</label><input id="provider-search" type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Name or category" />
        <div className="provider-list" aria-label="Benchmark suites">{visible.map(item => <button key={item.suite.id} className={`provider-option ${selected?.suite.id === item.suite.id ? 'selected' : ''}`} aria-pressed={selected?.suite.id === item.suite.id} onClick={() => setSelectedId(item.suite.id)}>
          <span><strong>{item.suite.name}</strong><small>{categoryLabel(item.suite.category)}</small></span><span className={`provider-state provider-${item.state}`}>{stateNames[item.state]}</span><span className="provider-counts"><b>{item.imported_tasks ?? 0}</b> imported<span> · {item.prepared_tasks ?? 0} prepared</span></span>
        </button>)}{!visible.length && <p className="provider-placeholder">No matching suites.</p>}</div>
      </div><div className="provider-detail">{selected && <>
        <div className="provider-title"><div><span className="tag">{original ? 'Benchmaxxing original · ' : ''}{categoryLabel(selected.suite.category)}</span><h3>{selected.suite.name}</h3></div><a href={selected.suite.upstream_url} target="_blank" rel="noreferrer">{original ? 'Project source' : 'Upstream'} ↗</a></div>
        {archived && <p className="provider-blockers">Archived from active scope. Existing evidence is preserved; preparation and new runs are disabled.</p>}
        <p className="provider-description">{selected.suite.description}</p>
        <dl className="provider-facts"><div><dt>{original ? 'Original suite' : 'Upstream release'}</dt><dd>{selected.suite.published_size}</dd></div><div><dt>Installed locally</dt><dd>{(selected.imported_tasks ?? 0).toLocaleString()} tasks</dd></div><div><dt>Prepared to run</dt><dd>{(selected.prepared_tasks ?? 0).toLocaleString()} tasks</dd></div><div><dt>Imported source size</dt><dd>{selected.source_bytes ? bytes(selected.source_bytes) : 'Not recorded'}</dd></div><div><dt>Version / license</dt><dd>{selected.suite.version} · {selected.suite.license}</dd></div><div><dt>Offline profile</dt><dd>{selected.suite.offline.replaceAll('-', ' ')}</dd></div></dl>
        {!!selected.blockers?.length && <div className="provider-blockers"><strong>Preparation requirements not met</strong><ul>{selected.blockers.map((value, index) => <li key={index}>{value}</li>)}</ul></div>}
        {!!selected.suite.requirements?.length && <details className="provider-requirements"><summary>Runtime and grading requirements</summary><ul>{selected.suite.requirements.map((value, index) => <li key={index}>{value}</li>)}</ul></details>}
        {!!selected.revisions?.length && <details className="provider-requirements"><summary>Imported revisions ({selected.revisions.length})</summary>{selected.revisions.map(value => <code key={value}>{value}</code>)}</details>}
        <button className="button button-primary provider-browse" disabled={!selected.imported_tasks} onClick={() => { onBrowse(selected.suite.id); document.getElementById('catalog')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }}>Browse {selected.imported_tasks ?? 0} imported tasks <span aria-hidden="true">↓</span></button>
        <form className="provider-fetch" onSubmit={event => void submitJob(event, 'fetch')}><button className="button button-quiet" type="submit" disabled={archived || !session || action || busy || selected.suite.offline === 'unavailable'}>{original ? 'Install original briefs' : 'Fetch full source'}</button><p>{original ? 'Install bundled original task definitions without internet access. Prepare the browser runtime separately.' : 'Explicitly download and import the pinned public source within the storage ceiling. Runtime preparation remains a separate step.'}</p></form>
        <details className="provider-actions"><summary>Import local source or prepare a runtime</summary>
          <form onSubmit={event => void submitJob(event, 'import')}><h4>Import a local snapshot</h4><p>Use an existing source directory or dataset file with its exact revision. Import does not download dependencies.</p><label htmlFor="provider-source">Local source path</label><input id="provider-source" value={source} onChange={event => setSource(event.target.value)} required spellCheck={false} placeholder="Absolute path to the prepared upstream snapshot" /><label htmlFor="provider-revision">Source revision</label><input id="provider-revision" value={revision} onChange={event => setRevision(event.target.value)} required spellCheck={false} placeholder="Pinned commit or dataset revision" /><label htmlFor="provider-variant">Variant identity <span className="optional">optional</span></label><input id="provider-variant" value={variant} onChange={event => setVariant(event.target.value)} spellCheck={false} placeholder="Uses the recorded offline variant by default" /><button className="button button-quiet" type="submit" disabled={archived || !session || action || busy || !source.trim() || !revision.trim()}>Import snapshot</button></form>
          <form onSubmit={event => void submitJob(event, 'prepare')}><h4>Prepare execution</h4><p>Pin an image and verify the imported tasks against their runtime requirements.</p><label htmlFor="provider-image">Runtime image</label><input id="provider-image" value={image} onChange={event => setImage(event.target.value)} spellCheck={false} placeholder="Leave blank to build the reviewed runtime" /><label className="provider-checkbox"><input type="checkbox" checked={pull} onChange={event => setPull(event.target.checked)} />Explicitly allow pulling this image</label><button className="button button-quiet" type="submit" disabled={archived || !session || action || busy || !selected.imported_tasks}>Prepare suite</button></form>
        </details>
      </>}</div></div>
      <details className="provider-settings"><summary>Preparation storage · {settings ? `${((settings.storage_limit_bytes ?? 250e9) / 1e9).toLocaleString()} GB total ceiling` : 'Settings unavailable'}</summary><form onSubmit={event => void saveSettings(event)}><div><label htmlFor="provider-cache">Preparation cache directory</label><input id="provider-cache" disabled={cacheLocked} value={cache} onChange={event => setCache(event.target.value)} placeholder="Default local data directory" spellCheck={false} /></div><div><label htmlFor="provider-cap">Total storage ceiling (GB)</label><input id="provider-cap" type="number" min="1" max="250" step="0.1" value={cap} onChange={event => setCap(event.target.value)} /></div><button type="submit" className="button button-quiet" disabled={!session || !settings || action || !Number.isFinite(Number(cap)) || Number(cap) < 1 || Number(cap) > 250}>Save storage settings</button></form><p>The ceiling applies to preparation across suites. Source bytes above do not include runtime images or build caches.{cacheLocked && ' The cache path is fixed after import; migrate the workspace before changing its storage location.'}</p></details>
    </> : <p className="provider-placeholder">No provider registry is available from this service.</p>}
    {notice && <p className="provider-notice" role="status">{notice}</p>}
    {!!visibleJobs.length && <div className="provider-jobs" aria-label="Preparation jobs"><h3>Import and preparation jobs</h3>{visibleJobs.slice(0, 8).map(job => {
      const problem = job.result?.errors;
      const errors = Array.isArray(problem) ? problem : problem && typeof problem === 'object' ? Object.entries(problem) : [];
      const prepared = job.result?.prepared_tasks;
      const imported = job.result?.imported_tasks;
      const generated = job.result?.generated_records;
      const total = job.result?.total_records;
      const failed = job.result?.failed_records;
      const current = job.result?.current_record;
      return <div className="provider-job" key={job.id}><div><strong>{providers.find(item => item.suite.id === job.suite_id)?.suite.name ?? job.suite_id}</strong><span>{job.operation} · {statusLabel(job.status)}{errors.length ? ` · ${errors.length} ${errors.length === 1 ? 'error' : 'errors'}` : ''}</span>{typeof prepared === 'number' && <p>{prepared} tasks prepared.</p>}{typeof imported === 'number' && <p>{imported} tasks imported.</p>}{typeof generated === 'number' && typeof total === 'number' && <p>{generated.toLocaleString()} / {total.toLocaleString()} canonical models generated{typeof failed === 'number' && failed > 0 ? ` · ${failed.toLocaleString()} failed` : ''}.</p>}{pending(job) && typeof current === 'string' && <p>Current model: {current}</p>}{job.error && <p className="provider-job-error">{job.error}</p>}</div><span className={`provider-state ${pending(job) ? 'provider-working' : job.status === 'failed' || errors.length ? 'provider-blocked' : 'provider-imported'}`}>{pending(job) ? 'In progress' : errors.length ? 'Review errors' : statusLabel(job.status)}</span>{job.result && <details><summary>View job evidence</summary><pre>{JSON.stringify(job.result, null, 2)}</pre></details>}</div>;
    })}</div>}
  </section>;
}
