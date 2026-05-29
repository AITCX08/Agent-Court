import { useEffect, useState, useCallback } from 'react';

// PR-16a 自写 hash router. 不引 react-router (节省 ~10KB gzip).
// hash 格式: "#/git-board" 等; "#/messages/<platform>" 为平台过滤页 (PR-22).

export type Route = '/git-board' | '/agents' | '/court-runtime' | '/messages';

const DEFAULT_ROUTE: Route = '/git-board';
const VALID_ROUTES: Route[] = ['/git-board', '/agents', '/court-runtime', '/messages'];

interface ParsedRoute {
  route: Route;
  platform: string | null;   // 仅 /messages/<platform> 时非 null
}

function parseHash(): ParsedRoute {
  const raw = window.location.hash;
  if (!raw || raw === '#' || raw === '#/') return { route: DEFAULT_ROUTE, platform: null };
  const path = raw.startsWith('#') ? raw.slice(1) : raw;

  // /messages 或 /messages/<platform>
  if (path === '/messages' || path.startsWith('/messages/')) {
    const platform = path.startsWith('/messages/') ? path.slice('/messages/'.length) : '';
    return { route: '/messages', platform: platform || null };
  }

  if ((VALID_ROUTES as string[]).includes(path)) return { route: path as Route, platform: null };
  return { route: DEFAULT_ROUTE, platform: null };
}

export function useHash(): [Route, (r: Route, sub?: string) => void, string | null] {
  const [parsed, setParsed] = useState<ParsedRoute>(parseHash);

  useEffect(() => {
    const onChange = () => setParsed(parseHash());
    window.addEventListener('hashchange', onChange);
    // 首次 mount 时若 URL 没 hash, 主动塞默认值 (利于 reload 后 URL 一致)
    if (!window.location.hash || window.location.hash === '#') {
      window.location.hash = DEFAULT_ROUTE;
    }
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  const setHash = useCallback((r: Route, sub?: string) => {
    const target = sub ? `${r}/${sub}` : r;
    if (window.location.hash !== `#${target}`) {
      window.location.hash = target;
    }
  }, []);

  return [parsed.route, setHash, parsed.platform];
}
