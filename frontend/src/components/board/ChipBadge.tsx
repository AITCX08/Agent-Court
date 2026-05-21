// 通用 chip 徽章. 支持 PR-16/17 plan §2.2 描述的 3 种基础色 + grey 默认.
import type { ReactNode } from 'react';

type ChipTone = 'open' | 'review' | 'participating' | 'wip' | 'gray';

interface Props {
  tone?: ChipTone;
  children: ReactNode;
}

const TONE_CLASS: Record<ChipTone, string> = {
  open: 'bg-accent-success/15 text-accent-success border-accent-success/30',
  review: 'bg-accent-warn/15 text-accent-warn border-accent-warn/30',
  participating: 'bg-accent-purple/15 text-accent-purple border-accent-purple/30',
  wip: 'bg-accent-primary/15 text-accent-primary border-accent-primary/30',
  gray: 'bg-fg-muted/10 text-fg-muted border-border-base',
};

export function ChipBadge({ tone = 'gray', children }: Props) {
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded-full border ${TONE_CLASS[tone]}
                  inline-flex items-center gap-1`}
    >
      {children}
    </span>
  );
}
