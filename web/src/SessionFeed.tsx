import { memo, useCallback, useLayoutEffect, useRef, useState } from 'react';
import type { Run, Task } from './api';
import { isActive, participantLabel, statusLabel } from './api';
import { taskIdentity } from './catalog';
import { argumentText } from './sessionState';
import type { SessionItem, SessionState } from './sessionState';
import type { StreamState } from './useRunEvidence';

const toolNames: Record<string, string> = { read_file: 'Read file', write_file: 'Write file', execute: 'Run command', submit: 'Submit solution' };
const toolStatuses: Record<string, string> = { assembling: 'Preparing arguments', ready: 'Ready to execute', running: 'Running', complete: 'Completed', error: 'Error' };

const SessionCard = memo(function SessionCard({ item, terminal }: { item: SessionItem; terminal: boolean }) {
  if (item.kind === 'notice') return <div className={`session-notice session-${item.status}`}>{item.content}</div>;
  if (item.kind === 'assistant') {
    if (item.status === 'complete' && !item.content && !item.reasoning && !item.refusal) return null;
    return <article className="session-card assistant-card" data-session-id={item.id}>
      <header><span className="session-avatar">A</span><strong>Assistant</strong><span className="session-turn">Turn {item.turn}</span><span className="session-card-status">{item.status === 'generating' && !terminal ? item.mode === 'json' ? 'Buffered response' : 'Generating' : 'Response recorded'}</span></header>
      {item.reasoning && <div className="provider-reasoning"><span>Provider reasoning · returned by the model</span><div className="session-prose">{item.reasoning}</div></div>}
      {item.content && <div className="session-prose assistant-text">{item.content}</div>}
      {item.refusal && <div className="provider-refusal"><span>Provider refusal</span><div className="session-prose">{item.refusal}</div></div>}
      {!item.content && !item.reasoning && !item.refusal && <p className="session-waiting">{terminal ? 'No text was received before this run ended.' : item.mode === 'json' ? 'This endpoint returns a buffered response. Waiting for the full response…' : 'Model generating · waiting for returned text or tool arguments…'}</p>}
      {item.truncated && <p className="session-limit">Long fields are shortened in this feed. Full evidence remains in the trace and export.</p>}
    </article>;
  }
  const path = argumentText(item.arguments, 'path');
  const command = argumentText(item.arguments, 'command');
  const content = argumentText(item.arguments, 'content');
  const pending = ['assembling', 'ready', 'running'].includes(item.status);
  return <article className={`session-card tool-card tool-${item.status}`} data-session-id={item.id}>
    <header><span className="session-avatar tool-avatar">{item.name === 'execute' ? '›_' : item.name === 'write_file' ? '+' : item.name === 'read_file' ? '≡' : '↗'}</span><strong>{toolNames[item.name] || item.name || 'Preparing tool call'}</strong><span className="session-turn">Turn {item.turn}</span><span className="session-card-status">{terminal && pending ? 'Run ended' : toolStatuses[item.status]}</span></header>
    {path && <div className="session-path mono">{path}</div>}
    {command && <pre className="session-code session-command"><span aria-hidden="true">$ </span>{command}</pre>}
    {content && <pre className="session-code session-file-content">{content}</pre>}
    {item.arguments && !path && !command && !content && item.arguments !== '{}' && <pre className="session-code session-arguments">{item.arguments}</pre>}
    {item.stdout && <div className="session-output"><span>{item.name === 'read_file' ? 'File contents' : 'Output'}</span><pre className="session-code">{item.stdout}</pre></div>}
    {item.stderr && <div className="session-output session-stderr"><span>Standard error</span><pre className="session-code">{item.stderr}</pre></div>}
    {item.summary && <p className="session-tool-summary">{item.summary}</p>}
    {item.truncated && <p className="session-limit">Long fields are shortened in this feed. Full evidence remains in the trace and export.</p>}
  </article>;
});

export default function SessionFeed({ run, tasks, session, streamState }: { run: Run | null; tasks: Task[]; session: SessionState; streamState: StreamState }) {
  const list = useRef<HTMLDivElement>(null);
  const terminal = !!run && !isActive(run.status);
  const latest = useRef({ session, terminal }); latest.current = { session, terminal };
  const frozen = useRef<typeof latest.current | null>(null);
  const [paused, setPaused] = useState<typeof latest.current | null>(null);
  const displayed = paused?.session ?? session;
  const unseen = Math.max(0, session.updates - (paused?.session.updates ?? session.updates));
  const pause = useCallback(() => { if (!frozen.current) { frozen.current = latest.current; setPaused(frozen.current); } }, []);
  useLayoutEffect(() => { if (!paused && list.current) list.current.scrollTop = list.current.scrollHeight; }, [session, paused]);
  function resume() { frozen.current = null; setPaused(null); }
  const identity = run ? taskIdentity(run, tasks) : null;
  return <section className="panel session-feed" aria-labelledby="session-heading" id="session-feed">
    <div className="session-heading"><div><span className="eyebrow">LIVE SESSION</span><h2 id="session-heading">{identity?.title ?? 'Follow the work as it happens'}</h2>{run && <p>{participantLabel(run)}<span> / </span><span className="mono">{run.id.slice(0, 12)}</span><span> / </span>{statusLabel(run.status)}</p>}</div><div className="session-controls"><span className={`stream-state ${streamState === 'live' ? 'stream-live' : ''}`}><span />{!run ? 'Ready' : streamState === 'complete' ? 'Recorded' : streamState === 'reconnecting' ? 'Reconnecting' : streamState === 'connecting' ? 'Connecting' : 'Live'}</span>{run && <button className="button button-quiet" onClick={paused ? resume : pause}>{paused ? 'Resume live feed' : 'Pause feed'}</button>}</div></div>
    {!run ? <div className="session-empty">Choose a benchmark and start a run. Returned assistant text and tool activity will appear here.</div> : <>
      <div className="session-reading-note">{paused ? 'Feed paused for reading. Incoming activity continues to be recorded.' : 'Assistant text and tool actions appear as they are received.'}{displayed.omitted > 0 && ` Showing recent session items; ${displayed.omitted} earlier items remain in the run export.`}</div>
      <div className="session-items" ref={list} tabIndex={0} aria-label="Session conversation" onPointerDownCapture={event => { if ((event.target as HTMLElement).closest('pre, .session-prose')) pause(); }} onWheelCapture={event => { if ((event.target as HTMLElement).closest('pre')) pause(); }} onScroll={() => { const node = list.current; if (node && !frozen.current && node.scrollHeight - node.scrollTop - node.clientHeight >= 35) pause(); }}>
        {displayed.items.length ? displayed.items.map(item => <SessionCard key={item.id} item={item} terminal={paused?.terminal ?? terminal} />) : <div className="session-empty">{run.status === 'queued' ? 'Queued · waiting for an execution slot.' : terminal ? 'No assistant or tool output was recorded for this run.' : 'Preparing the task environment. The session will appear here when the participant starts.'}</div>}
      </div>
      {paused && <button className="button button-quiet session-jump" onClick={resume}>{unseen ? `${unseen} new ${unseen === 1 ? 'update' : 'updates'} · ` : ''}Jump to live session ↓</button>}
    </>}
  </section>;
}
