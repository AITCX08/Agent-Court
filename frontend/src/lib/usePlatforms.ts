import { useEffect, useState } from 'react';
import { fetchPlatforms, subscribeMessages, type PlatformInfo } from './api';

const REFETCH_DEBOUNCE_MS = 800;

// 拉「有数据的平台」列表; SSE 收到新消息时 debounced refetch (新平台自动出现)。
export function usePlatforms(): PlatformInfo[] {
  const [platforms, setPlatforms] = useState<PlatformInfo[]>([]);

  useEffect(() => {
    let timer: number | null = null;
    const load = () => {
      fetchPlatforms()
        .then((r) => setPlatforms(r.platforms))
        .catch(() => { /* ignore */ });
    };
    load();
    const unsub = subscribeMessages({
      onMessage: () => {
        if (timer) window.clearTimeout(timer);
        timer = window.setTimeout(load, REFETCH_DEBOUNCE_MS);
      },
    });
    return () => {
      if (timer) window.clearTimeout(timer);
      unsub();
    };
  }, []);

  return platforms;
}
