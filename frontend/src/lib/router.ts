import { useEffect, useState, useCallback } from 'react';

// PR-16a 自写 hash router. 不引 react-router (节省 ~10KB gzip).
// 唯一 API: useHash() / setHash(). hash 格式: "#/git-board" 之类, 不带 query.

export type Route = '/git-board' | '/agents' | '/court-runtime' | '/messages';

const DEFAULT_ROUTE: Route = '/git-board';
const VALID_ROUTES: Route[] = ['/git-board', '/agents', '/court-runtime', '/messages'];

function parseHash(): Route {
  const raw = window.location.hash;
  if (!raw || raw === '#' || raw === '#/') return DEFAULT_ROUTE;
  const path = raw.startsWith('#') ? raw.slice(1) : raw;
  if ((VALID_ROUTES as string[]).includes(path)) return path as Route;
  return DEFAULT_ROUTE;
}

export function useHash(): [Route, (r: Route) => void] {
  const [route, setRoute] = useState<Route>(parseHash);

  useEffect(() => {
    const onChange = () => setRoute(parseHash());
    window.addEventListener('hashchange', onChange);
    // 首次 mount 时若 URL 没 hash, 主动塞默认值 (利于 reload 后 URL 一致)
    if (!window.location.hash || window.location.hash === '#') {
      window.location.hash = DEFAULT_ROUTE;
    }
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  const setHash = useCallback((r: Route) => {
    if (window.location.hash !== `#${r}`) {
      window.location.hash = r;
    }
  }, []);

  return [route, setHash];
}
