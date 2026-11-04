import { useEffect, useState } from 'react';
import { request } from './api';
import type { components } from './api-types';

export type ActivityRun = components['schemas']['RunActivity'];
export type ActivityReport = components['schemas']['ActivitySnapshot'];
export type ActivityConnection = 'connecting' | 'live' | 'reconnecting' | 'stale';

export function useActivity() {
  const [report, setReport] = useState<ActivityReport | null>(null);
  const [receivedAt, setReceivedAt] = useState(0);
  const [failed, setFailed] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [now, setNow] = useState(Date.now);
  const [startedAt] = useState(Date.now);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | null = null;
    async function poll() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 6000);
      try {
        const value = await request<ActivityReport>('/run-progress', { signal: controller.signal, cache: 'no-store' });
        if (!disposed) { setReport(value); setReceivedAt(Date.now()); setFailed(false); setError(null); }
      } catch (problem) {
        if (!disposed) { setFailed(true); setError(problem instanceof Error ? problem.name === 'AbortError' ? 'Progress request timed out.' : problem.message.slice(0, 200) : 'The progress request failed.'); }
      }
      finally { clearTimeout(timeout); if (!disposed) timer = setTimeout(() => void poll(), 1000); }
    }
    void poll();
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => { disposed = true; clearTimeout(timer); clearInterval(clock); controller?.abort(); };
  }, []);

  const connection: ActivityConnection = now - (receivedAt || startedAt) > 5000 ? 'stale'
    : failed ? 'reconnecting' : report ? 'live' : 'connecting';
  return { report, receivedAt, now, connection, error };
}
