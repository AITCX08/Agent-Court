import type { CommMessageLite } from '../../lib/api';

type Props = {
  msg: CommMessageLite;
  side: 'left' | 'right';   // assistant=left, user=right
  thinkSeconds?: number | null;  // 仅 assistant 气泡传, 显示在气泡下方
};

function fmtThink(s: number): string {
  if (s < 1) return `${Math.round(s * 1000)}ms`;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${Math.round(s % 60)}s`;
}

export function ChatBubble({ msg, side, thinkSeconds }: Props) {
  const isRight = side === 'right';
  return (
    <div className={`flex flex-col ${isRight ? 'items-end' : 'items-start'} w-full`}>
      <div
        className={`max-w-[78%] rounded-2xl px-3.5 py-2.5 text-[13px] leading-relaxed
                    whitespace-pre-wrap break-words shadow-sm
                    ${isRight
                      ? 'bg-accent-primary text-white rounded-br-md'
                      : 'bg-bg-card border border-border-base text-fg-primary rounded-bl-md'}`}
      >
        {msg.content}
      </div>
      {thinkSeconds != null && !isRight && (
        <span className="mt-1 ml-1 text-[10px] text-fg-muted">
          思考 {fmtThink(thinkSeconds)}
        </span>
      )}
    </div>
  );
}
