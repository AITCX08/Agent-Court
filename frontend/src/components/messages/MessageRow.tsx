import { useState } from 'react';
import { ChevronDown, ChevronRight, MessageSquare, User, Bot } from 'lucide-react';
import type { CommMessage } from '../../lib/api';

type Props = {
  msg: CommMessage;
  isNew?: boolean;
};

const SUMMARY_LEN = 30;

function relativeTime(ts: string): string {
  if (!ts) return '';
  const d = new Date(ts);
  const diffSec = Math.floor((Date.now() - d.getTime()) / 1000);
  if (diffSec < 60) return `${diffSec}s 前`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`;
  return `${Math.floor(diffSec / 86400)} 天前`;
}

function PlatformIcon({ platform }: { platform: string }) {
  if (platform === 'feishu') {
    return <span className="text-[10px] font-bold text-blue-500" title="feishu">飞</span>;
  }
  if (platform === 'weixin') {
    return <span className="text-[10px] font-bold text-green-500" title="weixin">微</span>;
  }
  return <MessageSquare className="w-3 h-3 text-fg-muted" aria-label={platform} />;
}

export function MessageRow({ msg, isNew }: Props) {
  const [open, setOpen] = useState(false);
  const isInbound = msg.role === 'user';
  const summary = msg.content.length > SUMMARY_LEN
    ? msg.content.slice(0, SUMMARY_LEN) + '…'
    : msg.content;
  const Chevron = open ? ChevronDown : ChevronRight;
  const DirIcon = isInbound ? User : Bot;

  return (
    <div
      className={`border-b border-border-base px-4 py-2 cursor-pointer
                  hover:bg-bg-card-hover transition
                  ${isNew ? 'bg-accent-primary/10 animate-pulse' : ''}`}
      onClick={() => setOpen((x) => !x)}
    >
      <div className="flex items-center gap-2 text-xs">
        <Chevron className="w-3 h-3 text-fg-muted flex-shrink-0" />
        <PlatformIcon platform={msg.platform} />
        <DirIcon className={`w-3 h-3 ${isInbound ? 'text-accent-primary' : 'text-fg-muted'}`} />
        <code className="text-fg-muted text-[10px]">{msg.project}·{msg.session_id}</code>
        <span className="text-fg-secondary truncate flex-1">{summary}</span>
        <span className="text-fg-muted text-[10px]" title={msg.timestamp}>
          {relativeTime(msg.timestamp)}
        </span>
      </div>
      {open && (
        <div className="mt-2 ml-5 flex flex-col gap-2">
          <div className="text-[11px] text-fg-muted flex flex-wrap gap-3">
            <span>platform: <code className="text-fg-secondary">{msg.platform}</code></span>
            <span>role: <code className="text-fg-secondary">{msg.role}</code></span>
            <span>session: <code className="text-fg-secondary">{msg.session_key}</code></span>
            <span>time: <code className="text-fg-secondary">{msg.timestamp}</code></span>
            <span>id: <code className="text-fg-secondary">{msg.msg_id}</code></span>
          </div>
          <pre className="text-[12px] text-fg-primary whitespace-pre-wrap leading-relaxed
                          bg-bg-base border border-border-base rounded-md p-3
                          max-h-[400px] overflow-auto">
            {msg.content}
          </pre>
        </div>
      )}
    </div>
  );
}
