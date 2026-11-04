import { useCallback, useEffect, useRef, useState } from 'react';
import { isActive, request } from './api';
import type { Run, RunDetail, RunEvent } from './api';
import { appendSessionEvents, emptySession } from './sessionState';

export type StreamState = 'connecting' | 'live' | 'complete' | 'reconnecting';
const EVENT_WINDOW = 300;

export function useRunEvidence(selected: string | null, onRun: (run: Run) => void, onError: (message: string) => void) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [eventCount, setEventCount] = useState(0);
  const [streamState, setStreamState] = useState<StreamState>('connecting');
  const [session, setSession] = useState(emptySession);
  const reloadRef = useRef<() => void>(() => {});
  const reload = useCallback(() => reloadRef.current(), []);

  useEffect(() => {
    setDetail(null); setEvents([]); setEventCount(0); setSession(emptySession()); setStreamState('connecting');
    if (!selected) return;
    let disposed = false;
    let status = 'queued';
    let source: EventSource | null = null;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let refreshTimer: ReturnType<typeof setTimeout> | undefined;
    let flushTimer: ReturnType<typeof setTimeout> | undefined;
    let detailController: AbortController | null = null;
    let inFlight = false;
    let refreshAgain = false;
    let streamCompleted = false;
    let latestSeq = 0;
    let buffered: RunEvent[] = [];

    function flush() {
      clearTimeout(flushTimer); flushTimer = undefined;
      if (disposed || !buffered.length) return;
      const batch = buffered; buffered = [];
      setEvents(current => [...current, ...batch].slice(-EVENT_WINDOW));
      setEventCount(current => current + batch.length);
      setSession(current => appendSessionEvents(current, batch));
    }
    async function refreshRun() {
      if (disposed) return;
      if (inFlight) { refreshAgain = true; return; }
      inFlight = true;
      detailController = new AbortController();
      const timeout = setTimeout(() => detailController?.abort(), 10000);
      try {
        const result = await request<RunDetail>(`/runs/${encodeURIComponent(selected!)}`, { signal: detailController.signal });
        if (!disposed) { status = result.run.status; setDetail(result); onRun(result.run); }
      } catch (problem) { if (!disposed) onError(problem instanceof Error ? problem.message : 'Unable to refresh the selected run.'); }
      finally {
        clearTimeout(timeout); inFlight = false;
        if (refreshAgain && !disposed) { refreshAgain = false; void refreshRun(); }
      }
    }
    function scheduleRefresh() {
      if (refreshTimer) return;
      refreshTimer = setTimeout(() => { refreshTimer = undefined; void refreshRun(); }, 1000);
    }
    reloadRef.current = () => void refreshRun();
    void refreshRun();

    function connect() {
      if (disposed) return;
      source = new EventSource(`/api/v1/runs/${encodeURIComponent(selected!)}/events?after=${latestSeq}`);
      source.onopen = () => { if (!disposed) setStreamState(streamCompleted ? 'complete' : 'live'); };
      source.addEventListener('run_event', (message: MessageEvent<string>) => {
        if (disposed) return;
        try {
          const event = JSON.parse(message.data) as RunEvent;
          if (event.run_id !== selected || !Number.isInteger(event.seq) || event.seq <= latestSeq) return;
          if (typeof event.kind !== 'string') throw new Error('Invalid event kind');
          latestSeq = event.seq;
          buffered.push(event);
          if (!flushTimer) flushTimer = setTimeout(flush, 80);
          if (event.kind === 'phase' || event.kind.startsWith('run.') || event.kind === 'cancellation_requested') scheduleRefresh();
        } catch { onError('An event could not be decoded. The stored run is still available.'); }
      });
      source.addEventListener('complete', () => { if (!disposed) { streamCompleted = true; flush(); source?.close(); setStreamState('complete'); void refreshRun(); } });
      source.onerror = () => {
        source?.close(); flush();
        if (disposed) return;
        if (streamCompleted) { setStreamState('complete'); return; }
        setStreamState('reconnecting');
        void refreshRun();
        reconnect = setTimeout(connect, 1500);
      };
    }
    connect();
    const poll = setInterval(() => { if (isActive(status)) void refreshRun(); }, 3000);
    return () => { disposed = true; reloadRef.current = () => {}; source?.close(); detailController?.abort(); clearTimeout(reconnect); clearTimeout(refreshTimer); clearTimeout(flushTimer); clearInterval(poll); };
  }, [selected, onRun, onError]);

  // Selection changes render before effect cleanup; never briefly show the previous run under a new selection.
  return { detail: detail?.run.id === selected ? detail : null, events, eventCount, streamState, session, reload };
}
