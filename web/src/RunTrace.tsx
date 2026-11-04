import { memo, useCallback, useLayoutEffect, useRef, useState } from 'react';
import { formatTime } from './api';
import type { RunEvent } from './api';
import type { StreamState } from './useRunEvidence';

const EventRow = memo(function EventRow({ event, onInspect }: { event: RunEvent; onInspect: () => void }) {
  const [open, setOpen] = useState(false);
  return <details className="event" onToggle={change => { setOpen(change.currentTarget.open); if (change.currentTarget.open) onInspect(); }}>
    <summary><span className="event-marker" /><span className="event-kind">{event.kind.replaceAll('_', ' ').replaceAll('.', ' / ')}</span><time className="mono" dateTime={event.timestamp}>{formatTime(event.timestamp)}</time><span className="event-seq mono">#{event.seq}</span></summary>
    {open && <pre>{JSON.stringify(event.data, null, 2)}</pre>}
  </details>;
});

export default function RunTrace({ events, count, streamState, active }: { events: RunEvent[]; count: number; streamState: StreamState; active: boolean }) {
  const list = useRef<HTMLDivElement>(null);
  const latest = useRef({ events, count });
  latest.current = { events, count };
  const frozen = useRef<{ events: RunEvent[]; count: number } | null>(null);
  const [pausedWindow, setPausedWindow] = useState<typeof frozen.current>(null);
  const displayed = pausedWindow?.events ?? events;
  const unseen = pausedWindow ? Math.max(0, count - pausedWindow.count) : 0;
  const pauseReading = useCallback(() => {
    if (frozen.current) return;
    // One bounded reading window, alongside the hook's bounded current window. No growing backlog.
    frozen.current = { events: latest.current.events.slice(-300), count: latest.current.count };
    setPausedWindow(frozen.current);
  }, []);
  useLayoutEffect(() => {
    if (!pausedWindow && list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [events, count, pausedWindow]);
  function followLatest() { frozen.current = null; setPausedWindow(null); }
  return <>
    <div className="trace-toolbar"><span className="eyebrow">SEQUENCED EVENTS</span><span className={`stream-state ${streamState === 'live' ? 'stream-live' : ''}`}><span />{streamState === 'live' ? 'Live' : streamState === 'reconnecting' ? 'Reconnecting' : streamState === 'connecting' ? 'Connecting' : 'Recorded'}</span></div>
    {pausedWindow ? <p className="trace-window-note">Reading a paused window of {displayed.length} events, sequences #{displayed[0]?.seq}–#{displayed.at(-1)?.seq}. {count} received in total. The complete trace remains in the run export.</p> : count > events.length && <p className="trace-window-note">Showing the latest {events.length} of {count} received events. The complete trace is preserved in the run export.</p>}
    {displayed.length === 0 ? <div className="tab-empty"><p>{active ? 'Waiting for the first event…' : 'No events were recorded for this run.'}</p></div> : <div className="event-list" ref={list} tabIndex={0} aria-label="Run event trace" onScroll={() => { const node = list.current; if (node && !frozen.current && node.scrollHeight - node.scrollTop - node.clientHeight >= 35) pauseReading(); }}>
      {displayed.map(event => <EventRow event={event} key={event.seq} onInspect={pauseReading} />)}
    </div>}
    {pausedWindow && <button className="button button-quiet new-events" onClick={followLatest}>{unseen ? `${unseen} new ${unseen === 1 ? 'event' : 'events'} · ` : 'Resume following · '}Jump to latest ↓</button>}
  </>;
}
