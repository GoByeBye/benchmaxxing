import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { formatDate, formatDuration, isActive, mutate, needsReview, participantLabel, request, statusLabel } from './api';
import type { Artifact, Doctor, Run, RunDetail, RunEvent, RunSpec, Session, Task } from './api';
import TaskCatalog, { Coverage } from './TaskCatalog';
import { categoryLabel, filterTasks, isArchivedTask, removedCategories, taskIdentity } from './catalog';
import LiveActivity from './LiveActivity';
import { useActivity } from './useActivity';
import RunTrace from './RunTrace';
import { useRunEvidence } from './useRunEvidence';
import { mergeRunHistory } from './runHistory';
import SessionFeed from './SessionFeed';
import ProviderCatalog from './ProviderCatalog';
import EndpointManager from './EndpointManager';
import type { ModelTarget } from './EndpointManager';
import ModelComparison from './ModelComparison';
import { elapsedSeconds } from './comparisonMath';

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

function endpointProblem(endpoint: string, mode: 'offline' | 'api') {
  try {
    const url = new URL(endpoint);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || /[\s\\]/.test(endpoint)) return 'Use an HTTP(S) endpoint without credentials, query parameters, or fragments.';
    if (mode === 'offline' && !['localhost', '[::1]'].includes(url.hostname) && !/^127(?:\.\d{1,3}){3}$/.test(url.hostname)) return 'Offline inference requires localhost or a numeric loopback address.';
  } catch { return 'Enter a valid HTTP(S) endpoint URL.'; }
  return null;
}

function RunComposer({ session, doctor, task, filteredTasks, batchTasks, onAddSelected, onAddFiltered, onRemoveBatch, onClearBatch, recentInference, onCreated, onError }: {
  session: Session | null; doctor: Doctor | null; task: Task | undefined; filteredTasks: Task[]; batchTasks: Task[];
  onAddSelected: () => void; onAddFiltered: () => void; onRemoveBatch: (id: string) => void; onClearBatch: () => void;
  recentInference: RunSpec['inference'];
  onCreated: (run: Run, inspect: boolean) => void; onError: (message: string) => void;
}) {
  const [participant, setParticipant] = useState<'scripted' | 'openai'>('scripted');
  const [variant, setVariant] = useState<'good' | 'bad' | 'refuse'>('good');
  const [mode, setMode] = useState<'offline' | 'api'>('offline');
  const [endpoint, setEndpoint] = useState('http://127.0.0.1:8080/v1');
  const [model, setModel] = useState('');
  const [keyEnv, setKeyEnv] = useState('');
  const [apiStyle, setApiStyle] = useState<'chat_completions' | 'responses'>('chat_completions');
  const [responsesReasoning, setResponsesReasoning] = useState(false);
  const [endpointSource, setEndpointSource] = useState<'custom' | 'saved'>('custom');
  const [modelTargets, setModelTargets] = useState<ModelTarget[]>([]);
  const sourceTouched = useRef(false);
  const [turns, setTurns] = useState('24');
  const [maxTokens, setMaxTokens] = useState('16384');
  const [seconds, setSeconds] = useState('1800');
  const [scope, setScope] = useState<'selected' | 'filtered' | 'batch'>('selected');
  const [repeats, setRepeats] = useState(1);
  const [submitting, setSubmitting] = useState(false);
  const [queueNotice, setQueueNotice] = useState('');
  const [progress, setProgress] = useState({ queued: 0, total: 0 });
  const profileTouched = useRef(false);
  useEffect(() => {
    if (profileTouched.current || !recentInference) return;
    profileTouched.current = true;
    setParticipant('openai');
    setMode(recentInference.mode ?? 'offline');
    setEndpoint(recentInference.base_url ?? '');
    setModel(recentInference.model ?? '');
    setKeyEnv(recentInference.api_key_env ?? '');
    setApiStyle(recentInference.api_style ?? 'chat_completions');
    setResponsesReasoning(recentInference.responses_reasoning ?? false);
  }, [recentInference]);
  useEffect(() => {
    if (modelTargets.length && !sourceTouched.current) {
      setEndpointSource('saved');
      setParticipant('openai');
    }
  }, [modelTargets]);
  const targets = scope === 'filtered' ? filteredTasks : scope === 'batch' ? batchTasks : task ? [task] : [];
  const modelCount = participant === 'openai' && endpointSource === 'saved' ? modelTargets.length : 1;
  const queueCount = targets.length * repeats * modelCount;
  const validation = participant === 'openai' && (!maxTokens.trim() || !Number.isInteger(Number(maxTokens)) || Number(maxTokens) < 1 || Number(maxTokens) > 32768)
    ? 'Max output tokens must be a whole number between 1 and 32,768.'
    : !Number.isInteger(Number(turns)) || Number(turns) < 1 || Number(turns) > 256
      ? 'Max turns must be a whole number between 1 and 256.'
    : !seconds.trim() || !Number.isFinite(Number(seconds)) || Number(seconds) < 1 || Number(seconds) > 86400
      ? 'Time budget must be between 1 and 86,400 seconds.'
      : !Number.isInteger(repeats) || repeats < 1 || repeats > 5
        ? 'Choose between 1 and 5 attempts per task.'
        : participant === 'openai'
          ? endpointSource === 'saved'
            ? modelTargets.length === 0 ? 'Check at least one model on a saved endpoint.' : null
            : endpointProblem(endpoint.trim(), mode) || (!model.trim() ? 'Enter the model identifier served by your endpoint.' : null)
              || (keyEnv.trim() && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(keyEnv.trim()) ? 'Enter an environment variable name for the API key.' : null)
          : null;
  const hasNative = targets.some(item => item.kind === 'native');
  const unprepared = targets.filter(item => isArchivedTask(item) || (item.kind === 'native' ? item.prepared !== true : !doctor?.ready)).length;
  useEffect(() => { if (hasNative) setParticipant('openai'); }, [hasNative]);
  const canSubmit = Boolean(session && !unprepared && targets.length && !validation && !submitting && (!hasNative || participant === 'openai'));

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!session || !canSubmit) return;
    setSubmitting(true);
    setQueueNotice('');
    const taskIds = Array.from({ length: repeats }, () => targets.map(item => item.id!)).flat();
    const profiles = participant === 'openai'
      ? endpointSource === 'saved'
        ? modelTargets.map(({ endpoint: saved, model: selectedModel }) => ({ mode: saved.mode, base_url: saved.base_url, model: selectedModel, api_key_env: saved.api_key_env, api_style: saved.api_style, responses_reasoning: saved.responses_reasoning }))
        : [{ mode, base_url: endpoint.trim(), model: model.trim(), api_key_env: keyEnv.trim() || null, api_style: apiStyle, responses_reasoning: responsesReasoning }]
      : [null];
    const attempts = taskIds.flatMap(taskId => profiles.map(inference => ({ task_id: taskId, inference })));
    setProgress({ queued: 0, total: attempts.length });
    const configuration: Omit<RunSpec, 'task_id'> = {
      schema_version: 1,
      participant,
      scripted_variant: variant,
      ...(participant === 'openai' ? { generation: { max_tokens: Number(maxTokens) } } : {}),
      budgets: { max_turns: Number(turns), wall_seconds: Number(seconds), ...(hasNative ? { memory_mb: 2048, pids: 256, command_seconds: 120, artifact_bytes: 67108864 } : {}) },
    };
    let queued = 0;
    try {
      for (const attempt of attempts) {
        const run = await mutate<Run>('/runs', session.token, { ...configuration, task_id: attempt.task_id, ...(attempt.inference ? { inference: attempt.inference } : {}) });
        onCreated(run, queued === 0);
        queued++;
        setProgress({ queued, total: attempts.length });
      }
      setQueueNotice(`Queued ${queued} ${queued === 1 ? 'attempt' : 'attempts'}. Each run has its own trace and scorecard.`);
    }
    catch (error) {
      setQueueNotice(`${queued} of ${attempts.length} attempts were queued. Existing runs are preserved.`);
      onError(errorMessage(error));
    }
    finally { setSubmitting(false); }
  }

  return <section className="panel composer" id="new-run" aria-labelledby="new-run-heading">
    <div className="panel-heading"><div><span className="eyebrow">EXPERIMENT SETUP</span><h2 id="new-run-heading">Start a run</h2></div><span className="section-index">01</span></div>
    <div className="task-card">
      <span className="task-icon"><Icon name="terminal" size={23} /></span>
      <div><span className="tag">{scope === 'filtered' ? 'FILTERED SELECTION' : scope === 'batch' ? 'SAVED SELECTION' : categoryLabel(task?.category ?? 'uncategorized')}</span><h3>{scope === 'filtered' ? `${filteredTasks.length} benchmarks selected` : scope === 'batch' ? `${batchTasks.length} benchmarks selected` : task?.title ?? 'Choose a benchmark'}</h3><p>{scope === 'filtered' || scope === 'batch' ? 'Each task runs in a fresh environment.' : 'Isolated execution. Independent grading.'}</p></div>
    </div>
    <form onSubmit={submit}>
      <fieldset disabled={submitting}>
      <label htmlFor="run-scope">Run scope</label>
      <select id="run-scope" value={scope} onChange={event => setScope(event.target.value as typeof scope)}><option value="selected">Selected task</option><option value="filtered">Filtered tasks ({filteredTasks.length})</option><option value="batch">My task selection ({batchTasks.length})</option></select>
      <div className="batch-builder"><div className="batch-actions"><button type="button" className="button button-quiet" disabled={!task} onClick={() => { onAddSelected(); setScope('batch'); }}>Add selected task</button><button type="button" className="button button-quiet" disabled={!filteredTasks.length} onClick={() => { onAddFiltered(); setScope('batch'); }}>Add filtered tasks ({filteredTasks.length})</button></div>
        {scope === 'batch' && <><div className="batch-heading"><strong>{batchTasks.length} selected across filters</strong><button type="button" disabled={!batchTasks.length} onClick={onClearBatch}>Clear selection</button></div><div className="batch-list" aria-label="Selected benchmark tasks">{batchTasks.map(item => <div key={item.id}><span>{item.title ?? item.id}<small>{categoryLabel(item.category ?? 'uncategorized')}</small></span><button type="button" aria-label={`Remove ${item.title ?? item.id} from batch`} onClick={() => onRemoveBatch(item.id!)}>×</button></div>)}</div></>}
      </div>
      <label htmlFor="participant">Participant</label>
      <select id="participant" value={participant} onChange={event => { profileTouched.current = true; setParticipant(event.target.value as 'scripted' | 'openai'); }}>
        <option value="scripted" disabled={hasNative}>Scripted reference</option><option value="openai">Model endpoint</option>
      </select>
      {participant === 'scripted' ? <>
        <label htmlFor="variant">Reference behavior</label>
        <select id="variant" value={variant} onChange={event => setVariant(event.target.value as typeof variant)}>
          <option value="good">Correct solution</option><option value="bad">Incorrect solution</option><option value="refuse">Refusal</option>
        </select>
        <p className="field-hint">Calibrates the grader with a known outcome. Choose a model endpoint to benchmark a model.</p>
      </> : <>
        <label htmlFor="endpoint-source">Model source</label>
        <select id="endpoint-source" value={endpointSource} onChange={event => { sourceTouched.current = true; setEndpointSource(event.target.value as typeof endpointSource); }}><option value="custom">Custom endpoint</option><option value="saved">Saved endpoints · select models</option></select>
        <div hidden={endpointSource !== 'saved'}><EndpointManager session={session} onChange={setModelTargets} /></div>
        {endpointSource === 'custom' && <>
        <label htmlFor="inference-mode">Inference access</label>
        <select id="inference-mode" value={mode} onChange={event => { profileTouched.current = true; setMode(event.target.value as typeof mode); }}>
          <option value="offline">Offline · local endpoint</option><option value="api">API · external endpoint</option>
        </select>
        <label htmlFor="endpoint">Endpoint URL</label>
        <input id="endpoint" type="url" required value={endpoint} onChange={event => { profileTouched.current = true; setEndpoint(event.target.value); }} spellCheck={false} />
        <label htmlFor="model">Model identifier</label>
        <input id="model" required value={model} onChange={event => { profileTouched.current = true; setModel(event.target.value); }} placeholder="As served by your endpoint" autoComplete="off" spellCheck={false} />
        <label htmlFor="api-style">API style</label>
        <select id="api-style" value={apiStyle} onChange={event => setApiStyle(event.target.value as typeof apiStyle)}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></select>
        {apiStyle === 'responses' && <label className="endpoint-model endpoint-reasoning"><input type="checkbox" checked={responsesReasoning} onChange={event => setResponsesReasoning(event.target.checked)} /><span>Request encrypted reasoning replay (supported servers only)</span></label>}
        <label htmlFor="key-env">API key environment variable <span className="optional">optional</span></label>
        <input id="key-env" value={keyEnv} onChange={event => { profileTouched.current = true; setKeyEnv(event.target.value); }} placeholder="BENCHMAXXING_API_KEY" autoComplete="off" spellCheck={false} />
        <p className="field-hint">Enter the variable name, not the secret. Model access is separate from the isolated task network.</p>
        </>}
      </>}
      <div className="form-row">
        <div><label htmlFor="turns">Max turns</label><input id="turns" type="number" required min={1} max={256} value={turns} onChange={event => setTurns(event.target.value)} /></div>
        <div><label htmlFor="seconds">Time budget <span className="optional">sec / attempt</span></label><input id="seconds" type="number" required min={1} max={86400} step="any" value={seconds} onChange={event => setSeconds(event.target.value)} /></div>
      </div>
      {participant === 'openai' && <><label htmlFor="max-output-tokens">Max output tokens <span className="optional">per model turn</span></label><input id="max-output-tokens" type="number" required min={1} max={32768} step={1} value={maxTokens} onChange={event => setMaxTokens(event.target.value)} /><p className="field-hint">Caps each model response. This is separate from the task's 30-minute wall budget and max turns.</p></>}
      <label htmlFor="repeats">Attempts per task</label>
      <select id="repeats" value={repeats} onChange={event => setRepeats(Number(event.target.value))}>{[1, 2, 3, 4, 5].map(count => <option key={count} value={count}>{count} {count === 1 ? 'attempt' : 'attempts'}</option>)}</select>
      </fieldset>
      <div className="queue-summary"><strong>{queueCount} {queueCount === 1 ? 'attempt' : 'attempts'} · {targets.length} {targets.length === 1 ? 'task' : 'tasks'}{participant === 'openai' && endpointSource === 'saved' ? ` · ${modelCount} models` : ''}</strong><span>Runs execute sequentially. Budgets apply to each attempt.</span></div>
      {validation && <p className="field-hint validation-hint" role="status">{validation}</p>}
      <button className="button button-primary start-button" type="submit" disabled={!canSubmit}>
        <Icon name="play" size={16} />{submitting ? `Queueing ${progress.queued} / ${progress.total}…` : queueCount > 1 ? `Queue ${queueCount} runs` : 'Start benchmark'}<span className="button-tail"><Icon name="arrow" size={17} /></span>
      </button>
      {queueNotice && <p className="queue-notice" role="status">{queueNotice}</p>}
      {hasNative && <p className="field-hint">Native runtime budget: 2 GiB memory · 256 processes · 120 sec per command · 64 MiB artifacts. Uses this suite’s tools and grader.</p>}
      {!!unprepared && <p className="field-hint readiness-hint">{unprepared} selected {unprepared === 1 ? 'task needs' : 'tasks need'} preparation. {hasNative ? 'Prepare the imported suite in Benchmark suites.' : 'Prepare the environment before starting a run.'}</p>}
      {!targets.length && <p className="field-hint readiness-hint">Select a task or adjust the catalog filters.</p>}
    </form>
    <div className="composer-footer"><Icon name="shield" size={15} /><span>Fresh container · task internet disabled</span></div>
  </section>;
}

type DetailTab = 'trace' | 'checks' | 'artifacts' | 'configuration';

function RunInspector({ detail, events, eventCount, selected, session, tasks, streamState, onCancel, onError }: {
  detail: RunDetail | null; events: RunEvent[]; selected: string | null;
  tasks: Task[]; eventCount: number;
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
    if (item.media_type === 'image/png') { setLoadingArtifact(false); return; }
    if (item.media_type && !/^(text\/|application\/(json|xml|javascript))/.test(item.media_type)) { setArtifactText('Binary artifact. Use the download control to inspect it with its native application.'); setLoadingArtifact(false); return; }
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
      <h3>Your first run starts here.</h3><p>Choose a benchmark and participant, then start a run.<br />Its trace, submission, and grading evidence will appear here.</p>
      <div className="lifecycle"><span>Execute</span><Icon name="arrow" size={14} /><span>Grade</span><Icon name="arrow" size={14} /><span>Inspect</span></div>
    </div> : !run ? <div className="empty-inspector" aria-live="polite"><span className="loading-line" /><p>Loading run evidence…</p></div> : <>
      <div className="run-summary">
        <div><div className="run-summary-title"><Badge status={needsReview(run) ? 'needs_review' : run.status} /><span className="mono muted">{run.id.slice(0, 12)}</span></div><h3>{participantLabel(run)}</h3><div className="run-task-name">{taskIdentity(run, tasks).title}<span> / {categoryLabel(taskIdentity(run, tasks).category)}</span></div><p>{formatDate(run.created_at)} <span className="separator">/</span> {run.spec.inference?.mode === 'api' ? 'API inference' : 'Offline profile'}{!isActive(run.status) && elapsedSeconds(run) !== null ? <> <span className="separator">/</span> {run.status === 'passed' ? 'Time to completion' : 'Execution time'} {formatDuration(elapsedSeconds(run)!)}</> : null}</p></div>
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
          {item === 'trace' && <span>{eventCount}</span>}{item === 'artifacts' && <span>{detail.artifacts.length}</span>}
        </button>)}
      </div>
      <div className="tab-content" role="tabpanel" id={`panel-${tab}`} aria-labelledby={`tab-${tab}`}>
        {tab === 'trace' && <RunTrace key={run.id} events={events} count={eventCount} streamState={streamState} active={isActive(run.status)} />}
        {tab === 'checks' && (run.score ? <>
          <div className="score-summary"><div><span className="eyebrow">{run.score.passed === null ? 'RECORDED SCORE' : 'OBJECTIVE CHECKS'}</span><div className="score-number">{Math.round(run.score.score * 100)}<span>%</span></div></div><div><Badge status={run.score.passed === null ? 'scored' : run.score.passed ? 'passed' : 'failed'} /><p className="mono muted">{run.score.grader_version}</p></div></div>
          <div className="checks-list">{run.score.checks?.map((check, index) => <div className="check-row" key={`${check.name}-${index}`}><span className={`check-icon ${check.passed ? 'pass' : 'fail'}`}><Icon name={check.passed ? 'check' : 'close'} size={16} /></span><div><strong>{check.name}</strong>{check.detail && <p>{check.detail}</p>}</div><span className="mono">{check.passed ? 'PASS' : 'FAIL'}</span></div>)}</div>
          <p className="panel-footnote">Results belong to this task and rubric. Compare runs with matching task and harness versions.</p>
        </> : <div className="tab-empty"><Icon name="shield" size={26} /><h3>{needsReview(run) ? 'Assessment needs review' : 'No grading result yet'}</h3><p>{needsReview(run) ? 'The response is preserved. A configured local judge or manual assessment is required; no pass/fail result has been assigned.' : isActive(run.status) ? 'The independent grader runs after submission.' : 'This run ended without a scorecard. Inspect the trace for details.'}</p></div>)}
        {tab === 'artifacts' && (detail.artifacts.length ? <>
          <div className="artifact-list">{detail.artifacts.map(item => <button className={`artifact-row ${artifact?.id === item.id ? 'selected' : ''}`} key={item.id} onClick={() => void inspectArtifact(item)}><Icon name="file" /><span><strong>{item.name}</strong><small className="mono">{item.size.toLocaleString()} bytes · {item.sha256.slice(0, 12)}</small></span><Icon name="arrow" size={16} /></button>)}</div>
          {artifact && <div className="artifact-preview"><div><span className="mono">{artifact.name}</span><a href={`/api/v1/runs/${encodeURIComponent(artifact.run_id)}/artifacts/${encodeURIComponent(artifact.id)}`} download={artifact.name}><Icon name="download" size={16} /><span className="sr-only">Download {artifact.name}</span></a></div>{artifact.media_type === 'image/png' ? <figure className="recorded-image"><img src={`/api/v1/runs/${encodeURIComponent(artifact.run_id)}/artifacts/${encodeURIComponent(artifact.id)}`} alt={`Recorded benchmark screenshot: ${artifact.name}`} /><figcaption>Recorded browser evidence. Visual design quality requires a separate review.</figcaption></figure> : <pre>{loadingArtifact ? 'Loading artifact…' : artifactText ?? 'Artifact could not be displayed.'}</pre>}</div>}
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
  const [taskCategory, setTaskCategory] = useState('all');
  const [taskSearch, setTaskSearch] = useState('');
  const [taskSuite, setTaskSuite] = useState('all');
  const [includeArchived, setIncludeArchived] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>('python.interval-merge');
  const [batchTaskIds, setBatchTaskIds] = useState<string[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [followActive, setFollowActive] = useState(true);
  const activity = useActivity();
  const updateRun = useCallback((run: Run) => setRuns(current => mergeRunHistory([run], current)), []);
  const { detail, events, eventCount, streamState, session: sessionFeed, reload } = useRunEvidence(selected, updateRun, setError);
  const historyController = useRef<AbortController | null>(null);
  const workspaceRequest = useRef(0);
  const activitySignature = activity.report?.runs.map(run => `${run.id}:${run.status}`).join('|');
  const previousActivity = useRef<string | undefined>(undefined);
  const activeId = activity.report?.runs.find(run => run.status === 'running')?.id ?? activity.report?.runs[0]?.id;
  const catalogTasks = useMemo(() => tasks.filter(task => !removedCategories.has(task.category ?? '') && (includeArchived || !isArchivedTask(task))), [tasks, includeArchived]);
  const filteredTasks = useMemo(() => filterTasks(catalogTasks.filter(task => taskSuite === 'all' || (taskSuite === 'original' ? !task.suite_id || task.tags?.includes('original') : task.suite_id === taskSuite)), taskCategory, taskSearch), [catalogTasks, taskCategory, taskSearch, taskSuite]);
  const selectedTask = filteredTasks.find(task => task.id === selectedTaskId);
  const batchTasks = useMemo(() => catalogTasks.filter(task => batchTaskIds.includes(task.id!)), [catalogTasks, batchTaskIds]);

  useEffect(() => {
    setSelectedTaskId(current => filteredTasks.some(task => task.id === current) ? current : filteredTasks.find(task => task.id === 'python.interval-merge')?.id ?? filteredTasks[0]?.id ?? null);
  }, [filteredTasks]);

  useEffect(() => { if (followActive && activeId && activeId !== selected) setSelected(activeId); }, [activeId, followActive, selected]);

  const refreshHistory = useCallback(async () => {
    if (historyController.current) return;
    const controller = new AbortController();
    historyController.current = controller;
    const timeout = setTimeout(() => controller.abort(), 10000);
    try {
      const result = await request<Run[]>('/runs?limit=1000', { signal: controller.signal });
      if (!controller.signal.aborted) setRuns(current => mergeRunHistory(result, current));
    } catch { /* Keep the last known history; the activity monitor reports connectivity. */ }
    finally { clearTimeout(timeout); if (historyController.current === controller) historyController.current = null; }
  }, []);

  const refresh = useCallback(async () => {
    const current = ++workspaceRequest.current;
    setRefreshing(true);
    const results = await Promise.allSettled([
      request<Session>('/session'), request<Doctor>('/doctor'), request<Task[]>(includeArchived ? '/tasks?include_archived=true' : '/tasks'), request<Run[]>('/runs?limit=1000'),
    ]);
    if (current !== workspaceRequest.current) return;
    if (results[0].status === 'fulfilled') setSession(results[0].value);
    if (results[1].status === 'fulfilled') setDoctor(results[1].value);
    if (results[2].status === 'fulfilled') setTasks(results[2].value);
    if (results[3].status === 'fulfilled') { const loadedRuns = results[3].value; setRuns(current => mergeRunHistory(loadedRuns, current)); setSelected(current => current ?? loadedRuns[0]?.id ?? null); }
    const failure = results.find(result => result.status === 'rejected');
    if (failure?.status === 'rejected') setError(errorMessage(failure.reason));
    else setError(null);
    setRefreshing(false);
  }, [includeArchived]);

  useEffect(() => { void refresh(); }, [refresh]);

  useEffect(() => {
    const poll = setInterval(() => void refreshHistory(), 5000);
    return () => { clearInterval(poll); historyController.current?.abort(); };
  }, [refreshHistory]);

  useEffect(() => {
    if (activitySignature === undefined) return;
    if (previousActivity.current !== undefined && previousActivity.current !== activitySignature) void refreshHistory();
    previousActivity.current = activitySignature;
  }, [activitySignature, refreshHistory]);

  function inspectRun(id: string) { setFollowActive(false); setSelected(id); }

  async function cancelRun() {
    if (!session || !selected) return;
    try { const run = await mutate<Run>(`/runs/${encodeURIComponent(selected)}/cancel`, session.token); updateRun(run); reload(); }
    catch (problem) { setError(errorMessage(problem)); }
  }

  const finished = runs.filter(run => run.score?.passed === true || run.score?.passed === false);
  const passed = runs.filter(run => run.status === 'passed').length;
  const currentRuns = activity.report?.runs ?? runs;
  const active = currentRuns.filter(run => isActive(run.status)).length;
  const queued = currentRuns.filter(run => run.status === 'queued').length;
  const running = currentRuns.filter(run => run.status === 'running').length;
  const recordedChecks = runs.reduce((total, run) => total + (run.score?.checks?.length ?? 0), 0);

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="wordmark" href="#workspace" aria-label="Benchmaxxing workspace"><span className="brand-mark" aria-hidden="true"><i /><i /><i /></span><span>benchmaxxing<span className="brand-period">.</span></span></a>
      <div className="workspace-switch"><span className="workspace-avatar">B</span><div><strong>Local workspace</strong><small>Foundation · v{session?.version ?? '0.1'}</small></div><span className="local-dot" /></div>
      <span className="sidebar-label">WORKSPACE</span>
      <nav aria-label="Main navigation"><a className="nav-item active" href="#workspace" aria-label="Overview"><Icon name="grid" /><span>Overview</span><span className="nav-arrow">↗</span></a><a className="nav-item" href="#history" aria-label="Run history"><Icon name="clock" /><span>Run history</span><span className="nav-count">{runs.length}</span></a><a className="nav-item" href="#providers" aria-label="Benchmark suites"><Icon name="grid" /><span>Benchmark suites</span></a><a className="nav-item" href="#new-run" aria-label="New run"><Icon name="play" /><span>New run</span></a></nav>
      <div className="sidebar-bottom"><div className="local-card"><Icon name="shield" size={20} /><strong>Your machine.<br />Your evidence.</strong><p>Runs and artifacts stay in your local workspace.</p><span><span className="status-dot" />LOCAL STORAGE</span></div><div className="sidebar-footer"><span>AGPL-3.0</span><span>01 / FOUNDATION</span></div></div>
    </aside>
    <main id="workspace">
      <header className="topbar"><div className="breadcrumb"><span>Workspace</span><span>/</span><strong>Overview</strong></div><div className="topbar-right"><span className={`connection ${activity.connection === 'live' ? 'connected' : ''}`}><span />{activity.connection === 'live' ? 'Local service connected' : activity.connection === 'stale' ? 'Activity updates delayed' : activity.connection === 'reconnecting' ? 'Reconnecting to service' : 'Connecting to service'}</span><button className={`icon-button ${refreshing ? 'refreshing' : ''}`} aria-label="Refresh workspace" onClick={() => void refresh()} disabled={refreshing}><Icon name="refresh" /></button></div></header>
      <div className="main-content">
        <section className="page-intro"><div><span className="eyebrow">LOCAL EVALUATION LAB</span><h1>Every run, <span>inspectable.</span></h1><p>Run controlled experiments. Follow the trace. Verify the result.</p></div><div className="intro-stamp"><span className="stamp-line" /><span>REPRODUCIBLE<br />BY DESIGN</span><span className="stamp-plus">+</span></div></section>
        {error && <div className="alert alert-error" role="alert"><div><strong>Something needs attention</strong><p>{error}</p></div><button className="icon-button" aria-label="Dismiss error" onClick={() => setError(null)}><Icon name="close" /></button></div>}
        {doctor && !doctor.ready && <div className="alert alert-prepare" role="status"><span className="alert-icon"><Icon name="terminal" size={22} /></span><div><strong>Prepare your task environment</strong><p>Run <code>benchmaxxing prepare</code> in your terminal, then refresh this workspace.</p><details className="diagnostics"><summary>View environment checks</summary><pre>{JSON.stringify(doctor.checks, null, 2)}</pre></details></div><button className="button button-quiet" onClick={() => void refresh()} disabled={refreshing}><Icon name="refresh" size={16} />Recheck</button></div>}
        <LiveActivity {...activity} following={followActive} selected={selected} onFollow={setFollowActive} onInspect={inspectRun} />
        <SessionFeed key={selected} run={detail?.run ?? null} tasks={tasks} session={sessionFeed} streamState={streamState} />
        <section className="metrics" aria-label="Workspace statistics"><div className="metric"><span>Loaded attempts</span><strong>{runs.length.toString().padStart(2, '0')}</strong><small>{runs.length >= 1000 ? 'Latest 1,000 runs' : 'Across loaded run history'}</small><Icon name="terminal" /></div><div className="metric"><span>Passed attempts</span><strong>{passed.toString().padStart(2, '0')}<em className="metric-divider">/</em><em>{finished.length.toString().padStart(2, '0')}</em></strong><small>Of attempts with a pass/fail grade</small><Icon name="check" /></div><div className="metric"><span>Grading checks</span><strong>{recordedChecks.toString().padStart(2, '0')}</strong><small>Recorded with evidence</small><Icon name="shield" /></div><div className="metric"><span>Run queue</span><strong>{queued.toString().padStart(2, '0')}<span className={`activity-dot ${active ? 'is-active' : ''}`} /></strong><small>{queued} queued · {running} running</small><Icon name="clock" /></div></section>
        <ProviderCatalog includeArchived={includeArchived} onArchived={setIncludeArchived} session={session} onChanged={() => void refresh()} onBrowse={suiteId => { setTaskSuite(suiteId); setTaskCategory('all'); setTaskSearch(''); }} />
        <Coverage tasks={catalogTasks} runs={runs} category={taskCategory} onCategory={setTaskCategory} />
        <ModelComparison runs={runs} />
        <TaskCatalog tasks={catalogTasks} filtered={filteredTasks} selected={selectedTask} category={taskCategory} search={taskSearch} suite={taskSuite} onSuite={setTaskSuite} onCategory={setTaskCategory} onSearch={setTaskSearch} onSelect={setSelectedTaskId} />
        <div className="workbench-grid">
          <RunComposer session={session} doctor={doctor} task={selectedTask} filteredTasks={filteredTasks} batchTasks={batchTasks} onAddSelected={() => { if (selectedTask?.id) setBatchTaskIds(ids => [...new Set([...ids, selectedTask.id!])]); }} onAddFiltered={() => setBatchTaskIds(ids => [...new Set([...ids, ...filteredTasks.map(item => item.id!)])])} onRemoveBatch={id => setBatchTaskIds(ids => ids.filter(item => item !== id))} onClearBatch={() => setBatchTaskIds([])} recentInference={runs.find(run => run.spec.participant === 'openai' && run.spec.inference)?.spec.inference} onError={setError} onCreated={(run, inspect) => { setError(null); updateRun(run); if (inspect) { setFollowActive(true); setSelected(run.id); } }} />
          <RunInspector detail={detail} events={events} eventCount={eventCount} selected={selected} session={session} tasks={tasks} streamState={streamState} onCancel={() => void cancelRun()} onError={setError} />
        </div>
        <section className="panel history" id="history" aria-labelledby="history-heading"><div className="panel-heading"><div><span className="eyebrow">EXPERIMENT LOG</span><h2 id="history-heading">Run history</h2></div><span className="history-count mono">{runs.length} {runs.length === 1 ? 'run' : 'runs'}</span></div>
          {runs.length === 0 ? <div className="empty-history"><span className="empty-history-dash">—</span><p>No runs yet. Your experiments will be recorded here.</p></div> : <div className="table-scroll"><table><thead><tr><th>Run / participant</th><th>Task</th><th>Status</th><th>Score</th><th>Time</th><th>Started</th><th><span className="sr-only">Inspect</span></th></tr></thead><tbody>{runs.map(run => <tr key={run.id} className={run.id === selected ? 'selected' : ''}><td><button className="run-link" onClick={() => inspectRun(run.id)}><strong>{participantLabel(run)}</strong><span className="mono">{run.id.slice(0, 12)}</span></button></td><td><span className="task-cell">{taskIdentity(run, tasks).title}</span><span className="table-subtext">{categoryLabel(taskIdentity(run, tasks).category)}</span></td><td><Badge status={needsReview(run) ? 'needs_review' : run.status} /></td><td><span className={`table-score ${run.score?.passed ? 'score-passed' : ''}`}>{run.score ? `${Math.round(run.score.score * 100)}%` : needsReview(run) ? 'Needs review' : '—'}</span></td><td className="table-date">{!isActive(run.status) && elapsedSeconds(run) !== null ? formatDuration(elapsedSeconds(run)!) : '—'}</td><td className="table-date">{formatDate(run.created_at)}</td><td><button className="icon-button" aria-label={`Inspect run ${run.id}`} onClick={() => inspectRun(run.id)}><Icon name="arrow" size={16} /></button></td></tr>)}</tbody></table></div>}
        </section>
        <footer className="page-footer"><span><span className="footer-dot" />Local execution. Versioned evidence.</span><span>BENCHMAXXING <span className="muted">/</span> FOUNDATION</span></footer>
      </div>
    </main>
  </div>;
}
