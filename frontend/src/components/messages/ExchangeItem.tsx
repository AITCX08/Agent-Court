import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight } from 'lucide-react';
import type { Exchange } from '../../lib/api';
import { ChatBubble } from './ChatBubble';

type Props = {
  ex: Exchange;
  isNew?: boolean;
};

const SUMMARY_LEN = 42;

function relativeTime(ts: string): string {
  if (!ts) return '';
  const d = new Date(ts);
  if (isNaN(d.getTime())) return '';
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 60) return `${diffSec}s 前`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  return `${Math.floor(diffSec / 86400)} 天前`;
}

function PlatformBadge({ platform }: { platform: string }) {
  if (platform === 'feishu') {
    return <span className="text-[10px] font-bold text-blue-500" title="feishu">飞</span>;
  }
  if (platform === 'weixin') {
    return <span className="text-[10px] font-bold text-green-500" title="weixin">微</span>;
  }
  return <span className="text-[10px] text-fg-muted" title={platform}>·</span>;
}

export function ExchangeItem({ ex, isNew }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  // 摘要优先取 user 问题; 孤立 assistant 则取 assistant 内容
  const head = ex.user?.content ?? ex.assistant?.content ?? '';
  const summary = head.length > SUMMARY_LEN ? head.slice(0, SUMMARY_LEN) + '…' : head;
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <div
      className={`border-b border-border-base transition
                  ${isNew ? 'bg-accent-primary/10' : 'hover:bg-bg-card-hover'}`}
    >
      <button
        type="button"
        onClick={() => setOpen((x) => !x)}
        className="w-full flex items-center gap-2 px-4 py-2.5 text-left"
      >
        <Chevron className="w-3.5 h-3.5 text-fg-muted flex-shrink-0" />
        <PlatformBadge platform={ex.platform} />
        <span className="text-[13px] text-fg-secondary truncate flex-1">{summary}</span>
        {ex.think_seconds != null && (
          <span className="text-[10px] text-fg-muted flex-shrink-0">
            {ex.think_seconds < 60 ? `${ex.think_seconds.toFixed(1)}s` : `${Math.floor(ex.think_seconds / 60)}m`}
          </span>
        )}
        <span className="text-[10px] text-fg-muted flex-shrink-0" title={ex.timestamp}>
          {relativeTime(ex.timestamp)}
        </span>
      </button>
      {open && (
        <div className="px-4 pb-4 pt-1 flex flex-col gap-2.5">
          {ex.user && <ChatBubble msg={ex.user} side="right" />}
          {ex.assistant && (
            <ChatBubble msg={ex.assistant} side="left" thinkSeconds={ex.think_seconds} />
          )}
          {!ex.assistant && (
            <span className="text-[10px] text-fg-muted ml-1">{t('messages.tab.no_reply')}</span>
          )}
        </div>
      )}
    </div>
  );
}
