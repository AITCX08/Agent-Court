import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2, AlertTriangle, MessageSquare } from 'lucide-react';
import { fetchMessages, subscribeMessages, type CommMessage } from '../../lib/api';
import { MessageRow } from '../messages/MessageRow';

const PAGE_SIZE = 50;
const NEW_HIGHLIGHT_MS = 2000;

export function MessagesPage() {
  const { t } = useTranslation();
  const [msgs, setMsgs] = useState<CommMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newIds, setNewIds] = useState<Set<string>>(new Set());

  const loadInitial = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const page = await fetchMessages({ limit: PAGE_SIZE });
      setMsgs(page.messages);
      setHasMore(page.messages.length >= PAGE_SIZE);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore || msgs.length === 0) return;
    setLoadingMore(true);
    try {
      const oldest = msgs[msgs.length - 1];
      const page = await fetchMessages({ limit: PAGE_SIZE, before: oldest.timestamp });
      setMsgs((cur) => [...cur, ...page.messages]);
      setHasMore(page.messages.length >= PAGE_SIZE);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoadingMore(false);
    }
  }, [msgs, loadingMore, hasMore]);

  useEffect(() => {
    loadInitial();
  }, [loadInitial]);

  useEffect(() => {
    const unsub = subscribeMessages({
      onMessage: (m) => {
        setMsgs((cur) => {
          if (cur.some((x) => x.msg_id === m.msg_id)) return cur;
          return [m, ...cur];
        });
        setNewIds((s) => {
          const next = new Set(s);
          next.add(m.msg_id);
          return next;
        });
        setTimeout(() => {
          setNewIds((s) => {
            const next = new Set(s);
            next.delete(m.msg_id);
            return next;
          });
        }, NEW_HIGHLIGHT_MS);
      },
    });
    return unsub;
  }, []);

  return (
    <div className="flex flex-col h-full">
      <header className="px-4 py-3 border-b border-border-base flex items-center gap-2">
        <MessageSquare className="w-4 h-4 text-fg-secondary" />
        <h1 className="text-sm font-semibold text-fg-primary">
          {t('messages.tab.title')}
        </h1>
        <span className="text-xs text-fg-muted">({msgs.length})</span>
      </header>

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
        {!loading && !error && msgs.length === 0 && (
          <div className="p-6 text-sm text-fg-muted text-center">
            {t('messages.tab.empty')}
          </div>
        )}
        {msgs.map((m) => (
          <MessageRow key={m.msg_id} msg={m} isNew={newIds.has(m.msg_id)} />
        ))}
        {!loading && msgs.length > 0 && hasMore && (
          <button
            type="button"
            onClick={loadMore}
            className="w-full py-3 text-xs text-fg-muted hover:text-fg-primary
                       hover:bg-bg-card-hover transition"
            disabled={loadingMore}
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
