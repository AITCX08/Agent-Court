import { useEffect, useRef } from 'react';
import { sseUrl, type Status } from './api';
import { deriveActivity } from './activity';
import { useStore } from './store';

// reconnect: 1s → 2s → 4s → 8s → 16s → 30s (capped)
function nextBackoff(prev: number): number {
  if (prev <= 0) return 1000;
  return Math.min(prev * 2, 30_000);
}

export function useSSE(): void {
  const setStatus = useStore((s) => s.setStatus);
  const setConnected = useStore((s) => s.setConnected);
  const pushActivity = useStore((s) => s.pushActivity);
  const lastStatusRef = useRef<Status | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const backoffRef = useRef<number>(0);
  const reconnectTimerRef = useRef<number | null>(null);
  const stoppedRef = useRef<boolean>(false);

  useEffect(() => {
    stoppedRef.current = false;
    const connect = () => {
      if (stoppedRef.current) return;
      const es = new EventSource(sseUrl(), { withCredentials: false });
      esRef.current = es;

      es.onopen = () => {
        backoffRef.current = 0;
        setConnected(true);
      };
      es.onmessage = (ev) => {
        try {
          const snapshot = JSON.parse(ev.data) as Status;
          const events = deriveActivity(lastStatusRef.current, snapshot);
          if (events.length > 0) pushActivity(events);
          lastStatusRef.current = snapshot;
          setStatus(snapshot);
        } catch (err) {
          // 忽略 keepalive / 解析失败
          console.warn('[sse] parse failed', err);
        }
      };
      es.onerror = () => {
        setConnected(false);
        es.close();
        esRef.current = null;
        if (stoppedRef.current) return;
        backoffRef.current = nextBackoff(backoffRef.current);
        reconnectTimerRef.current = window.setTimeout(connect, backoffRef.current);
      };
    };
    connect();
    return () => {
      stoppedRef.current = true;
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      if (esRef.current) {
        esRef.current.close();
        esRef.current = null;
      }
    };
  }, [setStatus, setConnected, pushActivity]);
}
