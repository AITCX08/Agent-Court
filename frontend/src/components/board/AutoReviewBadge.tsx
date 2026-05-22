import { useTranslation } from 'react-i18next';
import {
  Loader2,
  CheckCircle2,
  AlertCircle,
  FileText,
  Clock,
  SkipForward,
} from 'lucide-react';
import type { AutoReviewState } from '../../lib/api';

interface Props {
  state: AutoReviewState | undefined;
}

type StyleKey = AutoReviewState['state'];

const STATE_STYLE: Record<StyleKey, { color: string; icon: typeof Clock; spin?: boolean }> = {
  discovered:     { color: 'bg-fg-muted/20 text-fg-muted',                icon: Clock },
  queued:         { color: 'bg-accent-primary/20 text-accent-primary',    icon: Clock },
  running:        { color: 'bg-accent-warn/25 text-accent-warn',          icon: Loader2, spin: true },
  review_done:    { color: 'bg-accent-warn/15 text-accent-warn',          icon: FileText },
  posted:         { color: 'bg-emerald-500/20 text-emerald-400',          icon: CheckCircle2 },
  failed:         { color: 'bg-red-500/20 text-red-400',                  icon: AlertCircle },
  dedupe_skipped: { color: 'bg-fg-muted/10 text-fg-muted',                icon: SkipForward },
};

export function AutoReviewBadge({ state }: Props) {
  const { t } = useTranslation();
  if (!state) return null;
  const style = STATE_STYLE[state.state] || STATE_STYLE.discovered;
  const Icon = style.icon;
  const label = t(`auto_review.state.${state.state}`, { defaultValue: state.state });
  const tooltip = state.runtime
    ? t('auto_review.badge.tooltip_with_runtime', { state: label, runtime: state.runtime })
    : (state.error_message ?? label);
  return (
    <span
      className={`text-[10px] px-1.5 py-0.5 rounded inline-flex items-center gap-1 ${style.color}`}
      title={tooltip}
      aria-label={tooltip}
    >
      <Icon className={`w-3 h-3 ${style.spin ? 'animate-spin' : ''}`} />
      <span>{label}</span>
    </span>
  );
}
