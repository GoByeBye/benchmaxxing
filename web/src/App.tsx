import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { formatDate, formatTime, isActive, mutate, participantLabel, request, statusLabel } from './api';
import type { Artifact, Doctor, Run, RunDetail, RunEvent, RunSpec, Session, Task } from './api';

type IconName = 'grid' | 'play' | 'arrow' | 'clock' | 'check' | 'terminal' | 'download' | 'refresh' | 'stop' | 'file' | 'shield' | 'close';

function Icon({ name, size = 18 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, ReactNode> = {
    grid: <><rect x="3" y="3" width="6" height="6" rx="1" /><rect x="15" y="3" width="6" height="6" rx="1" /><rect x="3" y="15" width="6" height="6" rx="1" /><rect x="15" y="15" width="6" height="6" rx="1" /></>,
    play: <path d="m8 4 12 8-12 8Z" />,
    arrow: <><path d="M4 12h16m-6-6 6 6-6 6" /></>,
    clock: <><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    terminal: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="m7 8 4 4-4 4m7 0h3" /></>,
    download: <><path d="M12 3v12m-4-4 4 4 4-4M5 16v5h14v-5" /></>,
    refresh: <><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.2 7a7 7 0 0 1 11.6-1L20 9M4 15l2.2 3A7 7 0 0 0 18 17" /></>,
    stop: <rect x="6" y="6" width="12" height="12" rx="1" />,
    file: <><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8Zm0 0v5h5M8 12h8M8 16h6" /></>,
    shield: <><path d="m12 3 8 3v6c0 4-4 7-8 9-4-2-8-5-8-9V6Z" /><path d="m8 12 3 3 5-6" /></>,
    close: <path d="m6 6 12 12M6 18 18 6" />,
  };
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

function Badge({ status }: { status: string }) {
  return <span className={`status status-${status}`}><span className="status-dot" />{statusLabel(status)}</span>;
}

function errorMessage(error: unknown) { return error instanceof Error ? error.message : 'Something went wrong. Please try again.'; }

function RunComposer({ session, doctor, task, onCreated, onError }: {
  session: Session | null; doctor: Doctor | null; task: Task | undefined;
  onCreated: (run: Run) => void; onError: (message: string) => void;
}) {
  const [participant, setParticipant] = useState<'scripted' | 'openai'>('scripted');
  const [variant, setVariant] = useState<'good' | 'bad' | 'refuse'>('good');
  const [mode, setMode] = useState<'offline' | 'api'>('offline');
  const [endpoint, setEndpoint] = useState('http://127.0.0.1:8080/v1');
  const [model, setModel] = useState('');
  const [keyEnv, setKeyEnv] = useState('');
  const [turns, setTurns] = useState(24);
  const [seconds, setSeconds] = useState(120);
  const [submitting, setSubmitting] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!session || submitting) return;
    setSubmitting(true);
    const spec: RunSpec = {
      schema_version: 1,
      task_id: 'python.interval-merge',
      participant,
      scripted_variant: variant,
      budgets: { max_turns: turns, wall_seconds: seconds },
      ...(participant === 'openai' ? {
        inference: { mode, base_url: endpoint.trim(), model: model.trim(), api_key_env: keyEnv.trim() || null },
      } : {}),
    };
    try { onCreated(await mutate<Run>('/runs', session.token, spec)); }
    catch (error) { onError(errorMessage(error)); }
    finally { setSubmitting(false); }
  }

  return <section className="panel composer" id="new-run" aria-labelledby="new-run-heading">
    <div className="panel-heading"><div><span className="eyebrow">EXPERIMENT SETUP</span><h2 id="new-run-heading">Start a run</h2></div><span className="section-index">01</span></div>
    <div className="task-card">
      <span className="task-icon"><Icon name="terminal" size={23} /></span>
      <div><span className="tag">PROGRAMMING</span><h3>{task?.title ?? 'Repair interval merging'}</h3><p>One isolated task. Independently graded.</p></div>
    </div>
    <form onSubmit={submit}>
      <label htmlFor="participant">Participant</label>
      <select id="participant" value={participant} onChange={event => setParticipant(event.target.value as 'scripted' | 'openai')}>
        <option value="scripted">Scripted reference</option><option value="openai">Model endpoint</option>
      </select>
      {participant === 'scripted' ? <>
        <label htmlFor="variant">Reference behavior</label>
        <select id="variant" value={variant} onChange={event => setVariant(event.target.value as typeof variant)}>
          <option value="good">Correct solution</option><option value="bad">Incorrect solution</option><option value="refuse">Refusal</option>
        </select>
        <p className="field-hint">Validates the workflow without a model or GPU. This is a reference check, not a model benchmark.</p>
      </> : <>
        <label htmlFor="inference-mode">Inference access</label>
        <select id="inference-mode" value={mode} onChange={event => setMode(event.target.value as typeof mode)}>
          <option value="offline">Offline · local endpoint</option><option value="api">API · external endpoint</option>
        </select>
        <label htmlFor="endpoint">Endpoint URL</label>
        <input id="endpoint" type="url" required value={endpoint} onChange={event => setEndpoint(event.target.value)} spellCheck={false} />
        <label htmlFor="model">Model identifier</label>
        <input id="model" required value={model} onChange={event => setModel(event.target.value)} placeholder="As served by your endpoint" autoComplete="off" spellCheck={false} />
        <label htmlFor="key-env">API key environment variable <span className="optional">optional</span></label>
        <input id="key-env" value={keyEnv} onChange={event => setKeyEnv(event.target.value)} placeholder="BENCHMAXXING_API_KEY" autoComplete="off" spellCheck={false} />
        <p className="field-hint">Enter the variable name, not the secret. Model access is separate from the isolated task network.</p>
      </>}
      <div className="form-row">
        <div><label htmlFor="turns">Max turns</label><input id="turns" type="number" required min={1} max={256} value={turns} onChange={event => setTurns(Number(event.target.value))} /></div>
        <div><label htmlFor="seconds">Time budget <span className="optional">sec</span></label><input id="seconds" type="number" required min={1} max={86400} value={seconds} onChange={event => setSeconds(Number(event.target.value))} /></div>
      </div>
      <button className="button button-primary start-button" type="submit" disabled={!session || !doctor?.ready || submitting}>
        <Icon name="play" size={16} />{submitting ? 'Starting run…' : 'Start benchmark'}<span className="button-tail"><Icon name="arrow" size={17} /></span>
      </button>
      {!doctor?.ready && <p className="field-hint readiness-hint">Prepare the environment before starting a run.</p>}
    </form>
    <div className="composer-footer"><Icon name="shield" size={15} /><span>Fresh container · task internet disabled</span></div>
  </section>;
}

type DetailTab = 'trace' | 'checks' | 'artifacts' | 'configuration';

function RunInspector({ detail, events, selected, session, streamState, onCancel, onError }: {
  detail: RunDetail | null; events: RunEvent[]; selected: string | null;
  session: Session | null; streamState: 'connecting' | 'live' | 'complete' | 'reconnecting';
  onCancel: () => void; onError: (message: string) => void;
}) {
  const [tab, setTab] = useState<DetailTab>('trace');
  const [artifact, setArtifact] = useState<Artifact | null>(null);
  const [artifactText, setArtifactText] = useState<string | null>(null);
  const [loadingArtifact, setLoadingArtifact] = useState(false);
  const [exporting, setExporting] = useState(false);
  const artifactRequest = useRef(0);

  useEffect(() => { setTab('trace'); setArtifact(null); setArtifactText(null); artifactRequest.current++; }, [selected]);

  async function inspectArtifact(item: Artifact) {
    setArtifact(item); setArtifactText(null); setLoadingArtifact(true);
    const current = ++artifactRequest.current;
    try {
      const response = await fetch(`/api/v1/runs/${encodeURIComponent(item.run_id)}/artifacts/${encodeURIComponent(item.id)}`);
      if (!response.ok) throw new Error('Unable to load this artifact.');
      const text = await response.text();
      if (current === artifactRequest.current) setArtifactText(text);
    } catch (error) { onError(errorMessage(error)); }
    finally { if (current === artifactRequest.current) setLoadingArtifact(false); }
  }

  const run = detail?.run;
  async function downloadExport() {
    if (!run || isActive(run.status) || exporting) return;
    setExporting(true);
    try {
      const response = await fetch(`/api/v1/runs/${encodeURIComponent(run.id)}/export`);
      if (!response.ok) throw new Error('Unable to export this run. Refresh the workspace and try again.');
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url; link.download = `benchmaxxing-${run.id}.zip`; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { onError(errorMessage(error)); }
    finally { setExporting(false); }
  }
  return <section className="panel inspector" aria-labelledby="inspector-heading">
    <div className="panel-heading inspector-heading"><div><span className="eyebrow">RUN INSPECTOR</span><h2 id="inspector-heading">{run ? 'Evidence, in context' : 'Ready when you are'}</h2></div><span className="section-index">02</span></div>
    {!selected ? <div className="empty-inspector">
      <div className="empty-graphic" aria-hidden="true"><span /><span /><span /><Icon name="terminal" size={38} /></div>
      <h3>Your first run starts here.</h3><p>Choose a participant and start the reference task.<br />Its trace, submission, and grading evidence will appear here.</p>
      <div className="lifecycle"><span>Execute</span><Icon name="arrow" size={14} /><span>Grade</span><Icon name="arrow" size={14} /><span>Inspect</span></div>
    </div> : !run ? <div className="empty-inspector" aria-live="polite"><span className="loading-line" /><p>Loading run evidence…</p></div> : <>
      <div className="run-summary">
        <div><div className="run-summary-title"><Badge status={run.status} /><span className="mono muted">{run.id.slice(0, 12)}</span></div><h3>{participantLabel(run)}</h3><p>{formatDate(run.created_at)} <span className="separator">/</span> {run.spec.inference?.mode === 'api' ? 'API inference' : 'Offline profile'}</p></div>
        <div className="run-actions">
          {isActive(run.status) && <button className="button button-quiet" onClick={onCancel} disabled={!session}><Icon name="stop" size={14} />Cancel</button>}
          {isActive(run.status) ? <button className="button button-quiet" disabled title="Export is available when the run finishes"><Icon name="download" size={15} /><span>Export</span></button> : <a className="button button-quiet" href={`/api/v1/runs/${encodeURIComponent(run.id)}/export`} onClick={event => { event.preventDefault(); void downloadExport(); }} aria-disabled={exporting} title="Download this run's evidence bundle"><Icon name="download" size={15} /><span>{exporting ? 'Exporting…' : 'Export'}</span></a>}
        </div>
      </div>
      {run.error && <div className="run-error" role="status"><strong>{statusLabel(run.status)}</strong><span>{run.error}</span></div>}
      {run.refusal && <div className="refusal-note"><span className="tag">REFUSAL OBSERVATION</span><p>{run.refusal}</p></div>}
      <div className="tabs" role="tablist" aria-label="Run evidence">
        {(['trace', 'checks', 'artifacts', 'configuration'] as DetailTab[]).map(item => <button key={item} id={`tab-${item}`} role="tab" type="button" aria-selected={tab === item} aria-controls={`panel-${item}`} onClick={() => setTab(item)}>
          {item === 'trace' ? 'Trace' : item === 'checks' ? 'Grading' : item === 'artifacts' ? 'Artifacts' : 'Configuration'}
          {item === 'trace' && <span>{events.length}</span>}{item === 'artifacts' && <span>{detail.artifacts.length}</span>}
        </button>)}
      </div>
      <div className="tab-content" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === 'trace' && <>
          <div className="trace-toolbar"><span className="eyebrow">SEQUENCED EVENTS</span><span className={`stream-state ${streamState === 'live' ? 'stream-live' : ''}`}><span />{streamState === 'live' ? 'Live' : streamState === 'reconnecting' ? 'Reconnecting' : streamState === 'connecting' ? 'Connecting' : 'Recorded'}</span></div>
          {events.length === 0 ? <div className="tab-empty"><Icon name="clock" size={24} /><p>{isActive(run.status) ? 'Waiting for the first event…' : 'No events were recorded for this run.'}</p></div> : <div className="event-list">
            {events.map(event => <details className="event" key={event.seq}>
              <summary><span className="event-marker" /><span className="event-kind">{event.kind.replaceAll('_', ' ').replaceAll('.', ' / ')}</span><time className="mono" dateTime={event.timestamp}>{formatTime(event.timestamp)}</time><span className="event-seq mono">#{event.seq}</span></summary>
              <pre>{JSON.stringify(event.data, null, 2)}</pre>
            </details>)}
          </div>}
        </>}
        {tab === 'checks' && (run.score ? <>
          <div className="score-summary"><div><span className="eyebrow">OBJECTIVE CHECKS</span><div className="score-number">{Math.round(run.score.score * 100)}<span>%</span></div></div><div><Badge status={run.score.passed ? 'passed' : 'failed'} /><p className="mono muted">{run.score.grader_version}</p></div></div>
          <div className="checks-list">{run.score.checks?.map((check, index) => <div className="check-row" key={`${check.name}-${index}`}><span className={`check-icon ${check.passed ? 'pass' : 'fail'}`}><Icon name={check.passed ? 'check' : 'close'} size={16} /></span><div><strong>{check.name}</strong>{check.detail && <p>{check.detail}</p>}</div><span className="mono">{check.passed ? 'PASS' : 'FAIL'}</span></div>)}</div>
          <p className="panel-footnote">Checks evaluate the submitted artifact in a separate sandbox. This reference task validates the evaluation workflow.</p>
        </> : <div className="tab-empty"><Icon name="shield" size={26} /><h3>No grading result yet</h3><p>{isActive(run.status) ? 'The independent grader runs after submission.' : 'This run ended without a scorecard. Inspect the trace for details.'}</p></div>)}
        {tab === 'artifacts' && (detail.artifacts.length ? <>
          <div className="artifact-list">{detail.artifacts.map(item => <button className={`artifact-row ${artifact?.id === item.id ? 'selected' : ''}`} key={item.id} onClick={() => void inspectArtifact(item)}><Icon name="file" /><span><strong>{item.name}</strong><small className="mono">{item.size.toLocaleString()} bytes · {item.sha256.slice(0, 12)}</small></span><Icon name="arrow" size={16} /></button>)}</div>
          {artifact && <div className="artifact-preview"><div><span className="mono">{artifact.name}</span><a href={`/api/v1/runs/${encodeURIComponent(artifact.run_id)}/artifacts/${encodeURIComponent(artifact.id)}`} download={artifact.name}><Icon name="download" size={16} /><span className="sr-only">Download {artifact.name}</span></a></div><pre>{loadingArtifact ? 'Loading artifact…' : artifactText ?? 'Artifact could not be displayed.'}</pre></div>}
        </> : <div className="tab-empty"><Icon name="file" size={26} /><h3>No artifacts collected</h3><p>Collected submissions will appear here with their content hashes.</p></div>)}
        {tab === 'configuration' && <><span className="eyebrow">RESOLVED RUN SPECIFICATION</span><pre className="configuration-code">{JSON.stringify(run.spec, null, 2)}</pre><span className="eyebrow">PROVENANCE</span><pre className="configuration-code">{JSON.stringify(run.metadata, null, 2)}</pre></>}
      </div>
    </>}
  </section>;
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [doctor, setDoctor] = useState<Doctor | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [streamState, setStreamState] = useState<'connecting' | 'live' | 'complete' | 'reconnecting'>('connecting');
  const selectedRef = useRef(selected);
  const statusRef = useRef<string>('queued');
  selectedRef.current = selected;

  const loadRun = useCallback(async (id: string) => {
    const result = await request<RunDetail>(`/runs/${encodeURIComponent(id)}`);
    if (selectedRef.current === id) { setDetail(result); statusRef.current = result.run.status; }
    return result;
  }, []);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    const results = await Promise.allSettled([
      request<Session>('/session'), request<Doctor>('/doctor'), request<Task[]>('/tasks'), request<Run[]>('/runs'),
    ]);
    if (results[0].status === 'fulfilled') setSession(results[0].value);
    if (results[1].status === 'fulfilled') setDoctor(results[1].value);
    if (results[2].status === 'fulfilled') setTasks(results[2].value);
    if (results[3].status === 'fulfilled') { const loadedRuns = results[3].value; setRuns(loadedRuns); setSelected(current => current ?? loadedRuns[0]?.id ?? null); }
    const failure = results.find(result => result.status === 'rejected');
    if (failure?.status === 'rejected') setError(errorMessage(failure.reason));
    else setError(null);
    setRefreshing(false);
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    if (!selected) return;
    let disposed = false;
    let source: EventSource | null = null;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let latestSeq = 0;
    statusRef.current = 'queued';
    setDetail(null); setEvents([]); setStreamState('connecting');
    void loadRun(selected).catch(problem => { if (!disposed) setError(errorMessage(problem)); });

    async function refreshRun() {
      try {
        const result = await loadRun(selected!);
        const updatedRuns = await request<Run[]>('/runs');
        if (disposed) return;
        setRuns(updatedRuns);
        if (!isActive(result.run.status)) setStreamState('complete');
      } catch (problem) { if (!disposed) setError(errorMessage(problem)); }
    }

    function connect() {
      if (disposed) return;
      source = new EventSource(`/api/v1/runs/${encodeURIComponent(selected!)}/events?after=${latestSeq}`);
      source.onopen = () => { if (!disposed) setStreamState(isActive(statusRef.current) ? 'live' : 'complete'); };
      source.addEventListener('run_event', (message: MessageEvent<string>) => {
        if (disposed) return;
        try {
          const event = JSON.parse(message.data) as RunEvent;
          if (event.run_id !== selected || event.seq <= latestSeq) return;
          latestSeq = event.seq;
          setEvents(current => [...current, event]);
          clearTimeout(refreshTimer);
          refreshTimer = setTimeout(() => { void refreshRun(); }, 100);
        } catch { setError('An event could not be decoded. The stored run is still available.'); }
      });
      source.onerror = () => {
        source?.close();
        if (disposed) return;
        if (!isActive(statusRef.current)) { setStreamState('complete'); return; }
        setStreamState('reconnecting');
        reconnect = setTimeout(connect, 1500);
      };
    }
    connect();
    const poll = setInterval(() => { if (isActive(statusRef.current)) void refreshRun(); }, 1500);
    return () => { disposed = true; source?.close(); clearTimeout(reconnect); clearTimeout(refreshTimer); clearInterval(poll); };
  }, [selected, loadRun]);

  async function cancelRun() {
    if (!session || !selected) return;
    try { await mutate<Run>(`/runs/${encodeURIComponent(selected)}/cancel`, session.token); await loadRun(selected); }
    catch (problem) { setError(errorMessage(problem)); }
  }

  const finished = runs.filter(run => !isActive(run.status));
  const passed = runs.filter(run => run.status === 'passed').length;
  const active = runs.filter(run => isActive(run.status)).length;
  const recordedChecks = runs.reduce((total, run) => total + (run.score?.checks?.length ?? 0), 0);

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="wordmark" href="#workspace" aria-label="Benchmaxxing workspace"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span>benchmaxxing<span className="brand-period">.</span></span></a>
      <div className="workspace-switch"><span className="workspace-avatar">B</span><div><strong>Local workspace</strong><small>Foundation · v{session?.version ?? '0.1'}</small></div><span className="local-dot" /></div>
      <span className="sidebar-label">WORKSPACE</span>
      <nav aria-label="Main navigation"><a className="nav-item active" href="#workspace" aria-label="Overview"><Icon name="grid" /><span>Overview</span><span className="nav-arrow">↗</span></a><a className="nav-item" href="#history" aria-label="Run history"><Icon name="clock" /><span>Run history</span><span className="nav-count">{runs.length}</span></a><a className="nav-item" href="#new-run" aria-label="New run"><Icon name="play" /><span>New run</span></a></nav>
      <div className="sidebar-bottom"><div className="local-card"><Icon name="shield" size={20} /><strong>Your machine.<br />Your evidence.</strong><p>Runs and artifacts stay in your local workspace.</p><span><span className="status-dot" />LOCAL STORAGE</span></div><div className="sidebar-footer"><span>AGPL-3.0</span><span>01 / FOUNDATION</span></div></div>
    </aside>
    <main id="workspace">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><span>/</span><strong>Overview</strong></div><div className="topbar-right"><span className={`connection ${session ? 'connected' : ''}`}><span />{session ? 'Local service connected' : 'Connecting to service'}</span><button className={`icon-button ${refreshing ? 'refreshing' : ''}`} aria-label="Refresh workspace" onClick={() => void refresh()} disabled={refreshing}><Icon name="refresh" /></button></div></header>
      <div className="main-content">
        <section className="page-intro"><div><span className="eyebrow">LOCAL EVALUATION LAB</span><h1>Every run, <span>inspectable.</span></h1><p>Run controlled experiments. Follow the trace. Verify the result.</p></div><div className="intro-stamp"><span className="stamp-line" /><span>REPRODUCIBLE<br />BY DESIGN</span><span className="stamp-plus">+</span></div></section>
        {error && <div className="alert alert-error" role="alert"><div><strong>Something needs attention</strong><p>{error}</p></div><button className="icon-button" aria-label="Dismiss error" onClick={() => setError(null)}><Icon name="close" /></button></div>}
        {doctor && !doctor.ready && <div className="alert alert-prepare" role="status"><span className="alert-icon"><Icon name="terminal" size={22} /></span><div><strong>Prepare your task environment</strong><p>Run <code>benchmaxxing prepare</code> in your terminal, then refresh this workspace.</p><details className="diagnostics"><summary>View environment checks</summary><pre>{JSON.stringify(doctor.checks, null, 2)}</pre></details></div><button className="button button-quiet" onClick={() => void refresh()} disabled={refreshing}><Icon name="refresh" size={16} />Recheck</button></div>}
        <section className="metrics" aria-label="Workspace statistics"><div className="metric"><span>Total runs</span><strong>{runs.length.toString().padStart(2, '0')}</strong><small>Recorded in this workspace</small><Icon name="terminal" /></div><div className="metric"><span>Passed runs</span><strong>{passed.toString().padStart(2, '0')}<em className="metric-divider">/</em><em>{finished.length.toString().padStart(2, '0')}</em></strong><small>Of all finished attempts</small><Icon name="check" /></div><div className="metric"><span>Grading checks</span><strong>{recordedChecks.toString().padStart(2, '0')}</strong><small>Recorded with evidence</small><Icon name="shield" /></div><div className="metric"><span>Active runs</span><strong>{active.toString().padStart(2, '0')}<span className={`activity-dot ${active ? 'is-active' : ''}`} /></strong><small>{active ? 'Execution in progress' : 'Ready for the next experiment'}</small><Icon name="clock" /></div></section>
        <div className="workbench-grid">
          <RunComposer session={session} doctor={doctor} task={tasks[0]} onError={setError} onCreated={run => { setError(null); setRuns(current => [run, ...current.filter(item => item.id !== run.id)]); setSelected(run.id); }} />
          <RunInspector detail={detail} events={events} selected={selected} session={session} streamState={streamState} onCancel={() => void cancelRun()} onError={setError} />
        </div>
        <section className="panel history" id="history" aria-labelledby="history-heading"><div className="panel-heading"><div><span className="eyebrow">EXPERIMENT LOG</span><h2 id="history-heading">Run history</h2></div><span className="history-count mono">{runs.length} {runs.length === 1 ? 'run' : 'runs'}</span></div>
          {runs.length === 0 ? <div className="empty-history"><span className="empty-history-dash">—</span><p>No runs yet. Your experiments will be recorded here.</p></div> : <div className="table-scroll"><table><thead><tr><th>Run / participant</th><th>Task</th><th>Status</th><th>Score</th><th>Started</th><th><span className="sr-only">Inspect</span></th></tr></thead><tbody>{runs.map(run => <tr key={run.id} className={run.id === selected ? 'selected' : ''}><td><button className="run-link" onClick={() => setSelected(run.id)}><strong>{participantLabel(run)}</strong><span className="mono">{run.id.slice(0, 12)}</span></button></td><td><span className="task-cell">Interval merging</span><span className="table-subtext">Programming</span></td><td><Badge status={run.status} /></td><td><span className={`table-score ${run.score?.passed ? 'score-passed' : ''}`}>{run.score ? `${Math.round(run.score.score * 100)}%` : '—'}</span></td><td className="table-date">{formatDate(run.created_at)}</td><td><button className="icon-button" aria-label={`Inspect run ${run.id}`} onClick={() => setSelected(run.id)}><Icon name="arrow" size={16} /></button></td></tr>)}</tbody></table></div>}
        </section>
        <footer className="page-footer"><span><span className="footer-dot" />Local execution. Versioned evidence.</span><span>BENCHMAXXING <span className="muted">/</span> FOUNDATION</span></footer>
      </div>
    </main>
  </div>;
}
