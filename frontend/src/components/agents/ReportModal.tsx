/**
 * PR-20b: Agent report modal — three-section briefing (Problem / Investigation / Solution).
 *
 * Used by:
 * - AgentTeamCard footer "View Report" button (this PR)
 * - IssueCard / PrCard linked_team report button (PR-20c, same component)
 */
import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { FileText, Loader2, Sparkles, X } from 'lucide-react';
import { getAgentReport } from '../../lib/api';
import type { AgentReport } from '../../lib/api';

type Props = {
  teamId: string;
  onClose: () => void;
};

export function ReportModal({ teamId, onClose }: Props) {
  const { t } = useTranslation();
  const [data, setData] = useState<AgentReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getAgentReport(teamId)
      .then((j) => {
        if (cancelled) return;
        setData(j);
        if (j.error) setError(j.error);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [teamId]);

  const hasContent = data && (data.problem || data.investigation || data.solution);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        className="bg-bg-card border border-border-strong rounded-lg shadow-2xl
                      w-[720px] max-w-[95vw] max-h-[88vh] flex flex-col"
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-border-base">
          <div className="text-sm font-semibold text-fg-primary flex items-center gap-2">
            <FileText className="w-4 h-4" />
            {t('agents.card.view_report_title')}
            <code className="text-xs text-fg-muted font-mono">{teamId}</code>
          </div>
          <button
            type="button"
            onClick={onClose}
            title={t('common.close')}
            aria-label={t('common.close')}
            className="w-7 h-7 inline-flex items-center justify-center rounded-md
                       text-fg-muted hover:text-fg-primary hover:bg-bg-card-hover transition"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 min-h-0 overflow-auto p-5">
          {loading && (
            <div className="text-sm text-fg-muted flex items-center gap-2">
              <Loader2 className="w-3 h-3 animate-spin" />
              {t('agents.card.view_report_loading')}
            </div>
          )}

          {!loading && error && (
            <div className="text-sm text-accent-warn">
              {t('agents.card.view_report_error', { detail: error })}
            </div>
          )}

          {!loading && !error && data && data.source === 'missing' && !hasContent && (
            <div className="text-sm text-fg-secondary">
              {t('agents.card.view_report_missing')}
            </div>
          )}

          {!loading && !error && data && hasContent && (
            <div className="flex flex-col gap-4">
              {data.source === 'fallback' && (
                <div className="text-[11px] text-fg-muted flex items-center gap-1">
                  <Sparkles className="w-3 h-3" />
                  {t('agents.card.view_report_fallback_note')}
                </div>
              )}
              <ReportMeta
                status={data.status}
                phase={data.phase}
                updated_at={data.updated_at}
              />
              <ReportSection
                title={t('agents.card.view_report_section_problem')}
                body={data.problem}
              />
              <ReportSection
                title={t('agents.card.view_report_section_investigation')}
                body={data.investigation}
              />
              <ReportSection
                title={t('agents.card.view_report_section_solution')}
                body={data.solution}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ReportMeta({
  status,
  phase,
  updated_at,
}: {
  status: string;
  phase: string;
  updated_at: string;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex flex-wrap gap-3 text-[11px] text-fg-muted">
      <span>
        {t('agents.card.view_report_status')}:{' '}
        <code className="text-fg-secondary">{status}</code>
      </span>
      <span>
        {t('agents.card.view_report_phase')}:{' '}
        <code className="text-fg-secondary">{phase}</code>
      </span>
      {updated_at && (
        <span>
          {t('agents.card.view_report_updated_at')}:{' '}
          <code className="text-fg-secondary">{updated_at}</code>
        </span>
      )}
    </div>
  );
}

function ReportSection({ title, body }: { title: string; body: string }) {
  if (!body.trim()) return null;
  return (
    <div className="flex flex-col gap-1">
      <h3 className="text-sm font-semibold text-fg-primary">{title}</h3>
      <pre
        className="text-[12px] text-fg-secondary whitespace-pre-wrap leading-relaxed
                       bg-bg-base border border-border-base rounded-md p-3"
      >
        {body}
      </pre>
    </div>
  );
}
