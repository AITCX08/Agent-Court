import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, ArrowRight, Send, Loader2 } from 'lucide-react';
import { spawnFreeformAgent, getAgentPane, sendAgentInput } from '../../lib/api';
import { useToast } from '../Toast';

interface Props {
  open: boolean;
  onClose: () => void;
  /** PR-19b-2: 弹窗关闭时 (无论 step 几) 通知父刷新 agent 列表 */
  onSpawned?: (teamId: string) => void;
}

type Step = 1 | 2;

const POLL_INTERVAL_MS = 3_000;
const PANE_LINES = 500;

export function NewAgentModal({ open, onClose, onSpawned }: Props) {
  const { t } = useTranslation();
  const { push } = useToast();

  const [step, setStep] = useState<Step>(1);
  // Step 1 inputs
  const [label, setLabel] = useState('');
  const [initialPrompt, setInitialPrompt] = useState('');
  const [spawning, setSpawning] = useState(false);
  // Step 2 state
  const [teamId, setTeamId] = useState<string | null>(null);
  const [paneContent, setPaneContent] = useState<string>('');
  const [paneError, setPaneError] = useState<string | null>(null);
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);

  const paneScrollRef = useRef<HTMLPreElement | null>(null);

  // Reset state when modal opens
  useEffect(() => {
    if (!open) return;
    setStep(1);
    setLabel('');
    setInitialPrompt('');
    setTeamId(null);
    setPaneContent('');
    setPaneError(null);
    setInput('');
  }, [open]);

  // Step 2: poll pane every 3s while we have a teamId
  useEffect(() => {
    if (step !== 2 || !teamId) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const snap = await getAgentPane(teamId, PANE_LINES);
        if (!cancelled) {
          setPaneContent(snap.content);
          setPaneError(null);
        }
      } catch (err) {
        if (!cancelled) setPaneError((err as Error).message);
      }
    };
    poll();
    const id = window.setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [step, teamId]);

  // Auto-scroll pane to bottom on content change
  useEffect(() => {
    const el = paneScrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [paneContent]);

  // Esc to close
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  // ---- Step 1 handlers ----
  const submitStep1 = async () => {
    const labelTrim = label.trim();
    const promptTrim = initialPrompt.trim();
    if (!labelTrim) {
      push({ kind: 'err', text: t('agents.modal.empty_label') });
      return;
    }
    if (!promptTrim) {
      push({ kind: 'err', text: t('agents.modal.empty_prompt') });
      return;
    }
    setSpawning(true);
    try {
      const result = await spawnFreeformAgent({
        label: labelTrim,
        initial_prompt: promptTrim,
      });
      setTeamId(result.team_id);
      setStep(2);
      onSpawned?.(result.team_id);
      push({ kind: 'ok', text: t('agents.modal.spawn_success', { team: result.team_id }) });
    } catch (err) {
      push({ kind: 'err', text: t('agents.modal.spawn_failed', { detail: (err as Error).message }) });
    } finally {
      setSpawning(false);
    }
  };

  // ---- Step 2 handlers ----
  const sendMessage = async () => {
    if (!teamId) return;
    const text = input;  // 不 trim (用户可能想发末尾空格)
    if (!text.trim()) {
      push({ kind: 'err', text: t('agents.modal.empty_input') });
      return;
    }
    setSending(true);
    try {
      await sendAgentInput(teamId, text);
      setInput('');
      // 即时拉一次 pane 反映用户输入
      try {
        const snap = await getAgentPane(teamId, PANE_LINES);
        setPaneContent(snap.content);
      } catch {
        // 静默 — 主流程不阻塞, 下次 poll 会刷
      }
    } catch (err) {
      push({ kind: 'err', text: t('agents.modal.send_failed', { detail: (err as Error).message }) });
    } finally {
      setSending(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={(e) => {
        // 点击背景关闭 (不点穿到内容)
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-bg-card border border-border-strong rounded-lg shadow-2xl
                      w-[640px] max-w-[95vw] max-h-[90vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-border-base">
          <h2 className="text-sm font-semibold text-fg-primary">
            {step === 1 ? t('agents.modal.step1_title') : t('agents.modal.step2_title')}
            <span className="ml-2 text-fg-muted text-xs font-normal">
              {t('agents.modal.step_indicator', { current: step, total: 2 })}
            </span>
          </h2>
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
        <div className="flex-1 min-h-0 overflow-y-auto p-5">
          {step === 1 ? (
            <div className="flex flex-col gap-3">
              <label className="text-xs text-fg-secondary">
                {t('agents.modal.step1_label_field')}
                <input
                  type="text"
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  placeholder={t('agents.modal.step1_label_placeholder')}
                  disabled={spawning}
                  className="mt-1 w-full bg-bg-base border border-border-strong rounded-md
                             px-2.5 py-1.5 text-sm text-fg-primary placeholder:text-fg-muted
                             focus:outline-none focus:border-accent-primary/60 disabled:opacity-40"
                />
              </label>
              <label className="text-xs text-fg-secondary">
                {t('agents.modal.step1_prompt_field')}
                <textarea
                  value={initialPrompt}
                  onChange={(e) => setInitialPrompt(e.target.value)}
                  placeholder={t('agents.modal.step1_prompt_placeholder')}
                  disabled={spawning}
                  rows={8}
                  className="mt-1 w-full bg-bg-base border border-border-strong rounded-md
                             px-2.5 py-1.5 text-sm text-fg-primary placeholder:text-fg-muted
                             focus:outline-none focus:border-accent-primary/60 disabled:opacity-40
                             resize-y min-h-[8rem] font-mono"
                />
              </label>
              <p className="text-[11px] text-fg-muted leading-relaxed">
                {t('agents.modal.step1_help')}
              </p>
            </div>
          ) : (
            <div className="flex flex-col gap-2 h-[60vh]">
              <div className="text-[11px] text-fg-muted flex items-center gap-2">
                <span>team: <code className="text-fg-secondary">{teamId}</code></span>
                {paneError && (
                  <span className="text-accent-danger">⚠ {paneError}</span>
                )}
              </div>
              <pre
                ref={paneScrollRef}
                className="flex-1 min-h-0 overflow-auto bg-bg-base border border-border-base rounded-md
                           p-3 text-[11px] text-fg-secondary font-mono whitespace-pre leading-snug"
              >
                {paneContent || t('agents.modal.step2_pane_waiting')}
              </pre>
              <div className="flex items-start gap-2">
                <textarea
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                      e.preventDefault();
                      sendMessage();
                    }
                  }}
                  placeholder={t('agents.modal.step2_input_placeholder')}
                  rows={3}
                  disabled={sending}
                  className="flex-1 bg-bg-base border border-border-strong rounded-md
                             px-2.5 py-1.5 text-sm text-fg-primary placeholder:text-fg-muted
                             focus:outline-none focus:border-accent-primary/60 disabled:opacity-40
                             resize-y font-mono"
                />
                <button
                  type="button"
                  onClick={sendMessage}
                  disabled={sending || !input.trim()}
                  title={t('agents.modal.step2_send')}
                  className="px-3 py-1.5 rounded-md bg-accent-primary/15 text-accent-primary
                             border border-accent-primary/30 hover:bg-accent-primary/25 transition
                             disabled:opacity-40 inline-flex items-center gap-1 text-xs"
                >
                  {sending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                  {t('agents.modal.step2_send')}
                </button>
              </div>
              <p className="text-[11px] text-fg-muted">
                {t('agents.modal.step2_help')}
              </p>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-border-base">
          {step === 1 ? (
            <>
              <button
                type="button"
                onClick={onClose}
                disabled={spawning}
                className="text-xs px-3 py-1.5 rounded-md text-fg-muted
                           hover:bg-bg-card-hover transition disabled:opacity-40"
              >
                {t('common.cancel')}
              </button>
              <button
                type="button"
                onClick={submitStep1}
                disabled={spawning || !label.trim() || !initialPrompt.trim()}
                className="text-xs px-3 py-1.5 rounded-md bg-accent-primary/15 text-accent-primary
                           border border-accent-primary/30 hover:bg-accent-primary/25 transition
                           disabled:opacity-40 inline-flex items-center gap-1"
              >
                {spawning ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <ArrowRight className="w-3.5 h-3.5" />}
                {t('agents.modal.step1_next')}
              </button>
            </>
          ) : (
            <>
              <span className="flex-1 text-[11px] text-fg-muted">
                {t('agents.modal.step2_keep_running_hint')}
              </span>
              <button
                type="button"
                onClick={onClose}
                className="text-xs px-3 py-1.5 rounded-md bg-accent-primary/15 text-accent-primary
                           border border-accent-primary/30 hover:bg-accent-primary/25 transition
                           inline-flex items-center gap-1"
              >
                {t('agents.modal.step2_close')}
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
