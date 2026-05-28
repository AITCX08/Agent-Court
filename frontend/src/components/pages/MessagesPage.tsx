import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, AlertTriangle, MessageSquare } from 'lucide-react';
import { fetchExchanges, subscribeMessages, type Exchange } from '../../lib/api';
import { groupByDate, matchSearch, type DateBucket } from '../../lib/messageGrouping';
import { ExchangeItem } from '../messages/ExchangeItem';
import { DateGroupHeader } from '../messages/DateGroupHeader';
import { MessageSearchBar } from '../messages/MessageSearchBar';

const PAGE_SIZE = 50;
const REFETCH_DEBOUNCE_MS = 600;

export function MessagesPage() {
  const { t } = useTranslation();
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const [collapsed, setCollapsed] = useState<Set<DateBucket>>(new Set());
  const refetchTimer = useRef<number | null>(null);

  const loadInitial = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await fetchExchanges({ limit: PAGE_SIZE });
      setExchanges(page.exchanges);
      setHasMore(page.exchanges.length >= PAGE_SIZE);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore || exchanges.length === 0) return;
    setLoadingMore(true);
    try {
      const oldest = exchanges[exchanges.length - 1];
      const page = await fetchExchanges({ limit: PAGE_SIZE, before: oldest.timestamp });
      setExchanges((cur) => {
        const seen = new Set(cur.map((e) => e.pair_id));
        return [...cur, ...page.exchanges.filter((e) => !seen.has(e.pair_id))];
      });
      setHasMore(page.exchanges.length >= PAGE_SIZE);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoadingMore(false);
    }
  }, [exchanges, loadingMore, hasMore]);

  useEffect(() => {
    loadInitial();
  }, [loadInitial]);

  // SSE 收到新消息 -> debounced 重新拉首页(配对由后端保证)
  useEffect(() => {
    const unsub = subscribeMessages({
      onMessage: () => {
        if (refetchTimer.current) window.clearTimeout(refetchTimer.current);
        refetchTimer.current = window.setTimeout(() => {
          fetchExchanges({ limit: PAGE_SIZE })
            .then((page) => {
              setExchanges((cur) => {
                const seen = new Set(cur.map((e) => e.pair_id));
                const fresh = page.exchanges.filter((e) => !seen.has(e.pair_id));
                return fresh.length ? [...fresh, ...cur] : cur;
              });
            })
            .catch(() => { /* ignore */ });
        }, REFETCH_DEBOUNCE_MS);
      },
    });
    return () => {
      if (refetchTimer.current) window.clearTimeout(refetchTimer.current);
      unsub();
    };
  }, []);

  const filtered = useMemo(
    () => exchanges.filter((e) => matchSearch(e, query)),
    [exchanges, query],
  );
  const groups = useMemo(() => groupByDate(filtered, new Date()), [filtered]);

  const toggleBucket = (b: DateBucket) =>
    setCollapsed((s) => {
      const next = new Set(s);
      if (next.has(b)) next.delete(b); else next.add(b);
      return next;
    });

  return (
    <div className="flex flex-col h-full">
      <header className="px-4 py-3 border-b border-border-base flex items-center gap-2">
        <MessageSquare className="w-4 h-4 text-fg-secondary" />
        <h1 className="text-sm font-semibold text-fg-primary">{t('messages.tab.title')}</h1>
        <span className="text-xs text-fg-muted">({filtered.length})</span>
      </header>

      <MessageSearchBar value={query} onChange={setQuery} />

      <div className="flex-1 overflow-auto">
        {loading && (
          <div className="p-6 text-sm text-fg-muted flex items-center gap-2">
            <Loader2 className="w-3 h-3 animate-spin" /> {t('messages.tab.loading')}
          </div>
        )}
        {error && (
          <div className="p-4 text-sm text-accent-warn flex items-center gap-2">
            <AlertTriangle className="w-4 h-4" /> {error}
          </div>
        )}
        {!loading && !error && filtered.length === 0 && (
          <div className="p-6 text-sm text-fg-muted text-center">
            {query ? t('messages.tab.no_search_result') : t('messages.tab.empty')}
          </div>
        )}

        {groups.map((g) => (
          <div key={g.bucket}>
            <DateGroupHeader
              bucket={g.bucket}
              count={g.items.length}
              collapsed={collapsed.has(g.bucket)}
              onToggle={() => toggleBucket(g.bucket)}
            />
            {!collapsed.has(g.bucket) &&
              g.items.map((ex) => <ExchangeItem key={ex.pair_id} ex={ex} />)}
          </div>
        ))}

        {!loading && filtered.length > 0 && hasMore && !query && (
          <button
            type="button"
            onClick={loadMore}
            disabled={loadingMore}
            className="w-full py-3 text-xs text-fg-muted hover:text-fg-primary
                       hover:bg-bg-card-hover transition"
          >
            {loadingMore ? (
              <span className="inline-flex items-center gap-1">
                <Loader2 className="w-3 h-3 animate-spin" /> {t('messages.tab.loading_more')}
              </span>
            ) : (
              t('messages.tab.load_more')
            )}
          </button>
        )}
      </div>
    </div>
  );
}
