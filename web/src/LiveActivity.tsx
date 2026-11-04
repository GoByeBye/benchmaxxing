import { categoryLabel } from './catalog';
import type { ActivityConnection, ActivityReport } from './useActivity';

const phaseNames: Record<string, string> = { queued: 'Queued', preflight: 'Verifying assets', environment: 'Creating environment', participant: 'Participant', inference: 'Waiting for model', generating: 'Generating response', tool: 'Executing tool', collect: 'Collecting submission', grade: 'Grading', cleanup: 'Cleaning up' };

export function duration(seconds: number) {
  if (!Number.isFinite(seconds)) return 'Unknown';
  const value = Math.max(0, Math.floor(seconds));
  return value < 60 ? `${value}s` : value < 3600 ? `${Math.floor(value / 60)}m ${value % 60}s` : `${Math.floor(value / 3600)}h ${Math.floor(value % 3600 / 60)}m`;
}

export default function LiveActivity({ report, receivedAt, now, connection, error, following, selected, onFollow, onInspect }: {
  report: ActivityReport | null; receivedAt: number; now: number; connection: ActivityConnection;
  following: boolean; selected: string | null; onFollow: (follow: boolean) => void; onInspect: (id: string) => void;
  error: string | null;
}) {
  const running = report?.runs.filter(run => run.status === 'running') ?? [];
  const current = running.find(run => run.id === selected) ?? running[0];
  const queue = (report?.runs.filter(run => run.status === 'queued') ?? []).sort((a, b) => (a.queue_position ?? Infinity) - (b.queue_position ?? Infinity));
  const age = current?.last_event_at && report ? Math.max(0, (Date.parse(report.server_time) - Date.parse(current.last_event_at) + now - receivedAt) / 1000) : null;
  const elapsed = (run: typeof current) => run ? (run.elapsed_seconds ?? 0) + (connection === 'live' ? Math.max(0, now - receivedAt) / 1000 : 0) : 0;
  const connectionText = connection === 'live' ? 'Live' : connection === 'stale' ? 'Stale · retrying' : connection === 'reconnecting' ? 'Reconnecting' : 'Connecting';
  return <section className={`live-monitor monitor-${connection}`} aria-labelledby="live-heading">
    <div className="monitor-heading"><div><span className="eyebrow" id="live-heading">LIVE ACTIVITY</span><span className={`monitor-connection connection-${connection}`} role="status"><i />{connectionText}</span></div><label className="follow-control"><input type="checkbox" checked={following} onChange={event => onFollow(event.target.checked)} />Follow active run</label></div>
    {running.length > 1 && <p className="monitor-note">{running.length} running · Select any attempt to inspect its own trace and cancellation controls.</p>}
    {current ? running.map(run => <div className="monitor-current" key={run.id}>
      <div className="monitor-task"><button aria-label={`Inspect active run ${run.id}`} onClick={() => onInspect(run.id)}>{run.task_title || run.task_id}<span aria-hidden="true">↗</span></button><p>{categoryLabel(run.category)}<span> / </span>{run.model || (run.participant === 'scripted' ? 'Scripted reference' : run.participant)}</p></div>
      <div className="monitor-facts"><div><span>Phase</span><strong>{run.cancellation_requested ? 'Cancellation requested' : phaseNames[run.phase ?? ''] ?? (run.phase || 'Starting').replaceAll('_', ' ').replaceAll('.', ' / ')}</strong>{run.phase === 'tool' && run.last_tool && <small>{run.last_tool}</small>}{run.phase === 'grade' && !!run.checks_total && <small>{run.checks_done ?? 0} / {run.checks_total} checks</small>}</div><div><span>Model turns</span><strong className="mono">{run.turn ?? 0} / {run.max_turns}</strong></div><div><span>Elapsed / time budget</span><strong className="mono">{duration(elapsed(run))} / {duration(run.wall_seconds)}</strong></div></div>
    </div>) : <div className="monitor-idle"><strong>{report ? queue.length ? 'Waiting for the controller to start the next run' : 'No runs in progress' : 'Checking the local controller…'}</strong><span>{report && !queue.length ? 'Start a benchmark to follow its execution here.' : 'The controller starts attempts within the configured parallel limit.'}</span></div>}
    <div className="monitor-bottom"><div className="monitor-queue"><strong>{queue.length} queued</strong>{queue.slice(0, 3).map((run, index) => <button key={run.id} onClick={() => onInspect(run.id)} title={run.task_title}>{index === 0 ? 'Next: ' : ''}{run.task_title || run.task_id}</button>)}{queue.length > 3 && <span className="queue-more-desktop">+{queue.length - 3} more</span>}{queue.length > 1 && <span className="queue-more-mobile">+{queue.length - 1} more</span>}</div><span className="monitor-age">{current ? age === null ? 'No activity recorded yet' : `Last activity ${duration(age)} ago` : 'No active task'}</span></div>
    {(!following || connection !== 'live') && <p className="monitor-note">{!following && `Inspection pinned${selected ? ` to ${selected.slice(0, 12)}` : ''}. `}{connection === 'stale' ? 'Activity updates are delayed. Showing the last known controller state.' : connection === 'reconnecting' ? 'Connection interrupted. Retrying automatically.' : connection === 'connecting' ? 'Waiting for the first activity update.' : 'Enable follow to inspect the next active run automatically.'}</p>}
    {error && <p className="monitor-network-error">Live progress: {error} If a browser blocker is active, allow requests for this localhost app.</p>}
  </section>;
}
