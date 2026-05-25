import { useEffect, useMemo, useRef, useState } from 'react';
import { Terminal, ServerCog, Pencil, Check, X, Cpu, ExternalLink, Square, Eye, FileText, Loader2, Send } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { AgentTeam, AgentSummary } from '../../lib/api';
import { setAgentTeamLabel, killAgent, getAgentSummary, sendAgentInput, getToken } from '../../lib/api';
import { useToast } from '../Toast';
import { ReportModal } from './ReportModal';

/**
 * PR-19f: parse `(a)/(b)/(c)` 选项分组
 *
 * 用 bootstrap_protocol 强制 agent 输出格式:
 *   1. <Q1>
 *      (a) ...
 *      (b) ...
 *      (c) ...
 *   2. <Q2>
 *      (a) ...
 *
 * 返回最后一组连续 question (每问 ≥2 个 (a)(b)... 字母连续). 不在最后区块的旧
 * pane 内容忽略, 避免 scrollback 误识别已经回答过的选项题.
 */
interface OptionItem { letter: string; text: string }
interface OptionGroup { questionIndex: number; items: OptionItem[] }

function parseOptionGroups(paneContent: string): OptionGroup[] {
  if (!paneContent) return [];
  // 拿 pane 最后 ~80 行 (= 当前屏 + 一点), 避免抓到 scrollback 里早已答过的旧题
  const lines = paneContent.split('\n').slice(-80);
  // regex 抓行: 可选缩进 / `-`*` / `1.` 前缀, 然后 `(a)` ~ `(z)` 字母 + 文本
  const optionRe = /^\s*(?:[-*]\s+|\d+\.\s+)?\(([a-z])\)\s+(.+?)\s*$/;
  // 抓"独立 question 行": `1. ...` / `2. ...` (顶格或缩进开头)
  const questionRe = /^\s*(\d+)\.\s+(?!\()(.+)$/;

  const groups: OptionGroup[] = [];
  let currentQuestion: number | null = null;
  let currentItems: OptionItem[] = [];

  const flush = () => {
    if (currentItems.length >= 2 && currentQuestion !== null) {
      // 字母连续校验: a,b,c... 不能跳
      const letters = currentItems.map((x) => x.letter);
      const expected = letters.map((_, i) => String.fromCharCode(97 + i));
      const ok = letters.every((l, i) => l === expected[i]);
      if (ok) {
        groups.push({ questionIndex: currentQuestion, items: [...currentItems] });
      }
    }
    currentItems = [];
  };

  for (const line of lines) {
    const qm = line.match(questionRe);
    if (qm) {
      flush();
      currentQuestion = parseInt(qm[1], 10);
      continue;
    }
    const om = line.match(optionRe);
    if (om && currentQuestion !== null) {
      currentItems.push({ letter: om[1], text: om[2] });
    }
  }
  flush();

  return groups;
}

interface Props {
  team: AgentTeam;
  onLabelSaved?: () => void;
  onTeamKilled?: () => void;
  /** PR-19a: 看板跳转过来时高亮目标卡片 — 加 ring + scroll into view */
  highlighted?: boolean;
}

const SUMMARY_REFRESH_MS = 30_000;  // PR-19c-3: 卡片 summary 每 30s 刷一次

function formatStartedAt(iso: string): string {
  if (!iso) return '--';
  // 输入 "2026-05-21T10:19:37" (本地时间, 无 tz). Date(...) 把它当本地时间解析正好.
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { hour12: false });
}

export function AgentTeamCard({ team, onLabelSaved, onTeamKilled, highlighted }: Props) {
  const { t } = useTranslation();
  const { push } = useToast();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(team.label);
  const [saving, setSaving] = useState(false);
  const [confirmingStop, setConfirmingStop] = useState(false);
  const [stopping, setStopping] = useState(false);
  // PR-19a: 高亮时 scroll into view (block: 'center' 让目标卡片居中)
  const rootRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (highlighted) {
      rootRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }
  }, [highlighted]);
  // PR-19c-3: summary 30s 周期拉
  const [summary, setSummary] = useState<AgentSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  // PR-19c-3: view-terminal modal
  const [viewerOpen, setViewerOpen] = useState(false);
  // PR-20b: view-report modal
  const [reportOpen, setReportOpen] = useState(false);

  const isGhostty = team.kind === 'ghostty';
  const KindIcon = isGhostty ? Terminal : ServerCog;
  const kindLabel = isGhostty ? t('agents.card.kind_ghostty') : t('agents.card.kind_tmux');

  // PR-19c-3: summary 拉取 + 30s 周期刷
  useEffect(() => {
    let cancelled = false;
    const pull = async () => {
      setSummaryLoading(true);
      try {
        // PR-19d: ghostty 类型必传 pid, 后端用 lsof 查 cwd → claude jsonl
        const s = await getAgentSummary(team.id, { pid: team.pid ?? null });
        if (!cancelled) setSummary(s);
      } catch {
        // 单卡失败不影响其他, 静默
      } finally {
        if (!cancelled) setSummaryLoading(false);
      }
    };
    pull();
    const id = window.setInterval(pull, SUMMARY_REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [team.id]);

  // PR-19c-3 / PR-19d: 渲染 summary 行的文案
  const summaryText = (() => {
    const s = summary?.sentinel;
    if (s === 'ghostty-no-capture') return t('agents.card.summary_ghostty_hint');
    if (s === 'ghostty-no-pid') return t('agents.card.summary_ghostty_no_pid');
    if (s === 'ghostty-no-cwd') return t('agents.card.summary_ghostty_no_cwd');
    if (s === 'ghostty-no-session') return t('agents.card.summary_ghostty_no_session');
    if (s === 'ghostty-no-content') return t('agents.card.summary_ghostty_no_content');
    if (s === 'error') return t('agents.card.summary_error', { detail: summary?.error || '' });
    if (summary?.summary) return summary.summary;
    if (summaryLoading) return t('agents.card.summary_loading');
    return t('agents.card.summary_pending');
  })();

  const startEdit = () => {
    setDraft(team.label);
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setDraft(team.label);
  };

  const doStop = async () => {
    setStopping(true);
    try {
      await killAgent(team.id);
      push({ kind: 'ok', text: t('agents.card.stop_success') });
      setConfirmingStop(false);
      onTeamKilled?.();
    } catch (err) {
      push({ kind: 'err', text: t('agents.card.stop_error', { detail: (err as Error).message }) });
    } finally {
      setStopping(false);
    }
  };

  const save = async () => {
    setSaving(true);
    try {
      await setAgentTeamLabel(team, draft.trim());
      push({ kind: 'ok', text: t('agents.card.label_saved') });
      setEditing(false);
      onLabelSaved?.();
    } catch (err) {
      push({ kind: 'err', text: (err as Error).message });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      ref={rootRef}
      className={`rounded-lg bg-bg-card border p-4 flex flex-col gap-3 transition ${
        highlighted
          ? 'border-accent-purple ring-2 ring-accent-purple/60 ring-offset-2 ring-offset-bg-base shadow-lg shadow-accent-purple/20'
          : 'border-border-base'
      }`}
    >
      {/* Header: kind icon + label/edit + chip */}
      <div className="flex items-start gap-3">
        <div className={`w-9 h-9 rounded-md flex items-center justify-center flex-shrink-0 ${
          isGhostty
            ? 'bg-accent-primary/15 text-accent-primary'
            : 'bg-accent-purple/15 text-accent-purple'
        }`}>
          <KindIcon className="w-4 h-4" />
        </div>
        <div className="flex-1 min-w-0">
          {editing ? (
            <div className="flex items-center gap-1.5">
              <input
                type="text"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') save();
                  if (e.key === 'Escape') cancelEdit();
                }}
                placeholder={t('agents.card.label_placeholder')}
                autoFocus
                className="flex-1 bg-bg-base border border-border-strong rounded-md
                           px-2 py-1 text-sm text-fg-primary placeholder:text-fg-muted
                           focus:outline-none focus:border-accent-primary/60"
              />
              <button
                type="button"
                onClick={save}
                disabled={saving}
                title={t('agents.card.label_save')}
                aria-label={t('agents.card.label_save')}
                className="w-7 h-7 inline-flex items-center justify-center rounded-md
                           text-accent-success hover:bg-accent-success/15 transition
                           disabled:opacity-40"
              >
                <Check className="w-3.5 h-3.5" />
              </button>
              <button
                type="button"
                onClick={cancelEdit}
                disabled={saving}
                title={t('agents.card.label_cancel')}
                aria-label={t('agents.card.label_cancel')}
                className="w-7 h-7 inline-flex items-center justify-center rounded-md
                           text-fg-muted hover:bg-bg-card-hover transition
                           disabled:opacity-40"
              >
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          ) : (
            <div className="flex items-center gap-1.5">
              {team.label ? (
                <span className="text-sm text-fg-primary font-medium truncate">{team.label}</span>
              ) : (
                <span className="text-sm text-fg-muted italic truncate">{t('agents.card.label_placeholder')}</span>
              )}
              <button
                type="button"
                onClick={startEdit}
                title={t('agents.card.label_edit')}
                aria-label={t('agents.card.label_edit')}
                className="text-fg-muted hover:text-fg-primary transition
                           w-5 h-5 inline-flex items-center justify-center flex-shrink-0"
              >
                <Pencil className="w-3 h-3" />
              </button>
            </div>
          )}
          <div className="text-[10px] text-fg-muted mt-0.5 flex items-center gap-1.5 flex-wrap">
            <span className="px-1.5 py-0.5 rounded bg-bg-base border border-border-base">
              {kindLabel}
            </span>
            <span>{team.cli}</span>
          </div>
          {team.linked && (
            <a
              href={team.linked.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[11px] text-accent-primary hover:underline
                         flex items-center gap-1 mt-1"
            >
              <ExternalLink className="w-3 h-3" />
              {t('agents.card.linked_target', {
                repo: team.linked.repo,
                number: team.linked.number,
              })}
            </a>
          )}
        </div>
      </div>

      {/* PR-19c-3: AI summary 一行 — 卡片头部下方 */}
      <div className="pl-12 -mt-1">
        <div className="text-[11px] text-fg-secondary leading-snug
                        bg-bg-base/50 border border-border-base/50 rounded px-2 py-1
                        flex items-start gap-1.5">
          <span className="text-fg-muted flex-shrink-0">{t('agents.card.summary_label')}</span>
          <span className={summary?.sentinel === 'error' ? 'text-accent-warn' : ''}>
            {summaryText}
          </span>
        </div>
      </div>

      {/* Body: identity (tty/session) + pid + started */}
      <div className="text-[11px] text-fg-secondary space-y-0.5 leading-snug pl-12">
        {isGhostty ? (
          <div className="flex items-center gap-3 text-fg-muted">
            <span>tty</span>
            <code className="text-fg-secondary">{team.tty || '--'}</code>
            <span className="text-border-strong">·</span>
            <span>{t('agents.card.pid_short')}</span>
            <code className="text-fg-secondary">{team.pid ?? '--'}</code>
          </div>
        ) : (
          <div className="flex items-center gap-3 text-fg-muted">
            <span>{t('agents.card.session_short')}</span>
            <code className="text-fg-secondary truncate">{team.session}</code>
            <span className="text-border-strong">·</span>
            <span>{t('agents.card.windows', { count: team.windows })}</span>
          </div>
        )}
        <div className="text-fg-muted">
          {t('agents.card.started_at')}: <span className="text-fg-secondary">{formatStartedAt(team.started_at)}</span>
        </div>
      </div>

      {/* MCP subprocs (ghostty only) */}
      {isGhostty && (
        <div className="text-[11px] pl-12">
          <div className="text-fg-muted mb-1">{t('agents.card.subprocs')}</div>
          {team.mcp_subprocs.length === 0 ? (
            <div className="text-fg-muted/70 italic">{t('agents.card.no_subprocs')}</div>
          ) : (
            <ul className="flex flex-wrap gap-1.5">
              {team.mcp_subprocs.map((sp) => (
                <li
                  key={sp.pid}
                  title={sp.command}
                  className="flex items-center gap-1 px-1.5 py-0.5 rounded
                             bg-bg-base border border-border-base text-fg-secondary"
                >
                  <Cpu className="w-2.5 h-2.5 text-accent-purple" />
                  <span className="truncate max-w-[140px]">{sp.name}</span>
                  <span className="text-fg-muted">{sp.pid}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {/* Footer: 查看终端 + 查看汇报 + (tmux: Stop / ghostty: hint) */}
      <div className="pt-2 mt-1 border-t border-border-base/50 flex justify-between items-center gap-2">
        <div className="flex items-center gap-1">
          {/* PR-19c-3: 查看终端 按钮 (所有 kind 都有) */}
          <button
            type="button"
            onClick={() => setViewerOpen(true)}
            title={t('agents.card.view_terminal')}
            aria-label={t('agents.card.view_terminal')}
            className="text-[11px] px-2 py-1 rounded
                       text-fg-muted hover:text-accent-primary
                       hover:bg-accent-primary/10 transition
                       inline-flex items-center gap-1"
          >
            <Eye className="w-3 h-3" />
            {t('agents.card.view_terminal')}
          </button>
          {/* PR-20b: 查看汇报 按钮 */}
          <button
            type="button"
            onClick={() => setReportOpen(true)}
            title={t('agents.card.view_report')}
            aria-label={t('agents.card.view_report')}
            className="text-[11px] px-2 py-1 rounded
                       text-fg-muted hover:text-accent-primary
                       hover:bg-accent-primary/10 transition
                       inline-flex items-center gap-1"
          >
            <FileText className="w-3 h-3" />
            {t('agents.card.view_report')}
          </button>
        </div>
        {team.kind === 'tmux' ? (
          confirmingStop ? (
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-fg-secondary">{t('agents.card.stop_confirm_title')}</span>
              <button
                type="button"
                onClick={doStop}
                disabled={stopping}
                className="text-[11px] px-2 py-1 rounded
                           bg-accent-danger/15 text-accent-danger border border-accent-danger/30
                           hover:bg-accent-danger/25 transition disabled:opacity-40
                           inline-flex items-center gap-1"
              >
                <Square className="w-3 h-3" />
                {t('agents.card.stop_confirm_yes')}
              </button>
              <button
                type="button"
                onClick={() => setConfirmingStop(false)}
                disabled={stopping}
                className="text-[11px] px-2 py-1 rounded text-fg-muted
                           hover:bg-bg-card-hover transition disabled:opacity-40"
              >
                {t('agents.card.stop_confirm_no')}
              </button>
            </div>
          ) : (
            <button
              type="button"
              onClick={() => setConfirmingStop(true)}
              title={t('agents.card.stop')}
              aria-label={t('agents.card.stop')}
              className="text-[11px] px-2 py-1 rounded
                         text-fg-muted hover:text-accent-danger
                         hover:bg-accent-danger/10 transition
                         inline-flex items-center gap-1"
            >
              <Square className="w-3 h-3" />
              {t('agents.card.stop')}
            </button>
          )
        ) : (
          <span className="text-[10px] text-fg-muted italic">
            {t('agents.card.stop_ghostty_hint')}
          </span>
        )}
      </div>

      {/* PR-19c-3: 查看终端 modal */}
      {viewerOpen && (
        <TerminalViewerModal
          team={team}
          onClose={() => setViewerOpen(false)}
        />
      )}
      {/* PR-20b: 查看汇报 modal */}
      {reportOpen && (
        <ReportModal teamId={team.id} onClose={() => setReportOpen(false)} />
      )}
    </div>
  );
}


// PR-19c-3: 查看终端 modal — 仅 tmux 类型走 SSE 实时显示 pane; ghostty 提示
function TerminalViewerModal({ team, onClose }: { team: AgentTeam; onClose: () => void }) {
  const { t } = useTranslation();
  const { push } = useToast();
  const isTmux = team.kind === 'tmux';
  const [content, setContent] = useState('');
  const [streamError, setStreamError] = useState<string | null>(null);
  const preRef = useRef<HTMLPreElement | null>(null);
  // PR-19f: 输入框 + chip 选项
  const [input, setInput] = useState('');
  const [sending, setSending] = useState(false);
  // chip selection: { [questionIndex]: letter } — 每题最多 1 个
  const [chipSelection, setChipSelection] = useState<Record<number, string>>({});

  // 拿 tmux session 名 (去 "tmux:" 前缀)
  const tmuxId = team.id.startsWith('tmux:') ? team.id.slice('tmux:'.length) : team.id;

  useEffect(() => {
    if (!isTmux) return;
    const token = getToken();
    const url = `/api/agent/${encodeURIComponent(tmuxId)}/pane/stream?t=${encodeURIComponent(token)}`;
    const es = new EventSource(url);
    es.onmessage = (ev) => {
      try {
        const payload = JSON.parse(ev.data);
        if (payload.error) {
          setStreamError(String(payload.error));
          return;
        }
        if (typeof payload.content === 'string') {
          setContent(payload.content);
          setStreamError(null);
        }
      } catch {}
    };
    es.onerror = () => setStreamError('connection lost (retrying...)');
    return () => es.close();
  }, [isTmux, tmuxId]);

  useEffect(() => {
    const el = preRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [content]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  // PR-19f: 解析 pane 当前可见选项
  const optionGroups = useMemo(() => parseOptionGroups(content), [content]);

  // 当 pane 内容变化 (agent 新出题), 清掉旧 selection
  // (用 group keys 做 dependency 判断 — group 列表变化时重置)
  const groupKey = useMemo(
    () => optionGroups.map((g) => `${g.questionIndex}:${g.items.map((i) => i.letter).join('')}`).join('|'),
    [optionGroups],
  );
  useEffect(() => {
    setChipSelection({});
  }, [groupKey]);

  const toggleChip = (q: number, letter: string) => {
    setChipSelection((prev) => {
      const next = { ...prev };
      if (next[q] === letter) delete next[q];
      else next[q] = letter;
      return next;
    });
  };

  const buildPayload = (): string => {
    // 拼 chip selection: "1a 2c 3b" (按 questionIndex 升序)
    const chipPart = optionGroups
      .map((g) => g.questionIndex)
      .filter((q) => chipSelection[q])
      .sort((a, b) => a - b)
      .map((q) => `${q}${chipSelection[q]}`)
      .join(' ');
    const textPart = input.trim();
    if (chipPart && textPart) return `${chipPart} ${textPart}`;
    return chipPart || textPart;
  };

  const payload = buildPayload();
  const canSend = isTmux && !sending && payload.length > 0;

  const sendNow = async () => {
    if (!canSend) return;
    setSending(true);
    try {
      await sendAgentInput(tmuxId, payload);
      setInput('');
      setChipSelection({});
    } catch (err) {
      push({ kind: 'err', text: t('agents.card.terminal_send_failed', { detail: (err as Error).message }) });
    } finally {
      setSending(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div className="bg-bg-card border border-border-strong rounded-lg shadow-2xl
                      w-[820px] max-w-[95vw] max-h-[92vh] flex flex-col">
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-3 border-b border-border-base">
          <div className="text-sm font-semibold text-fg-primary flex items-center gap-2">
            <Terminal className="w-4 h-4" />
            {t('agents.card.view_terminal_title')}
            <code className="text-xs text-fg-muted font-mono">{team.id}</code>
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
        <div className="flex-1 min-h-0 overflow-auto p-4">
          {isTmux ? (
            <>
              {streamError && (
                <div className="mb-2 text-[11px] text-accent-warn">
                  ⚠ {streamError}
                </div>
              )}
              <pre
                ref={preRef}
                className="bg-bg-base border border-border-base rounded-md p-3
                           text-[11px] text-fg-secondary font-mono whitespace-pre leading-snug
                           min-h-[50vh] max-h-[60vh] overflow-auto"
              >
                {content || (
                  <span className="text-fg-muted italic">
                    <Loader2 className="inline w-3 h-3 animate-spin mr-1" />
                    {t('agents.card.view_terminal_loading')}
                  </span>
                )}
              </pre>
            </>
          ) : (
            <div className="text-sm text-fg-secondary leading-relaxed p-6 text-center">
              <Terminal className="w-8 h-8 mx-auto mb-3 text-fg-muted" />
              <p>{t('agents.card.view_terminal_ghostty_hint')}</p>
              <p className="text-[11px] text-fg-muted mt-3">
                tty: <code className="text-fg-secondary">{team.tty || '--'}</code>
                {' · '}
                pid: <code className="text-fg-secondary">{team.pid ?? '--'}</code>
              </p>
            </div>
          )}
        </div>

        {/* PR-19f: 选项 chip 行 + 输入框 (仅 tmux) */}
        {isTmux && (
          <div className="border-t border-border-base px-5 py-3 flex flex-col gap-2">
            {optionGroups.length > 0 && (
              <div className="flex flex-col gap-1.5">
                <div className="text-[10px] text-fg-muted">
                  {t('agents.card.terminal_options_hint')}
                </div>
                {optionGroups.map((g) => (
                  <div key={g.questionIndex} className="flex items-center gap-1.5 flex-wrap">
                    <span className="text-[11px] text-fg-secondary font-medium min-w-[1.5rem]">
                      {g.questionIndex}.
                    </span>
                    {g.items.map((opt) => {
                      const selected = chipSelection[g.questionIndex] === opt.letter;
                      return (
                        <button
                          key={opt.letter}
                          type="button"
                          onClick={() => toggleChip(g.questionIndex, opt.letter)}
                          title={opt.text}
                          className={`text-[11px] px-2 py-0.5 rounded-md border transition
                                     inline-flex items-center gap-1 max-w-[260px] ${
                            selected
                              ? 'bg-accent-primary/25 text-accent-primary border-accent-primary/60'
                              : 'bg-bg-base text-fg-secondary border-border-base hover:border-accent-primary/40 hover:text-fg-primary'
                          }`}
                        >
                          <span className="font-mono font-semibold">({opt.letter})</span>
                          <span className="truncate">{opt.text}</span>
                        </button>
                      );
                    })}
                  </div>
                ))}
              </div>
            )}
            <div className="flex items-start gap-2">
              <textarea
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    sendNow();
                  }
                }}
                placeholder={t('agents.card.terminal_input_placeholder')}
                rows={2}
                disabled={sending}
                className="flex-1 bg-bg-base border border-border-strong rounded-md
                           px-2.5 py-1.5 text-sm text-fg-primary placeholder:text-fg-muted
                           focus:outline-none focus:border-accent-primary/60
                           disabled:opacity-40 resize-y font-mono"
              />
              <button
                type="button"
                onClick={sendNow}
                disabled={!canSend}
                title={t('agents.card.terminal_send')}
                className="px-3 py-1.5 rounded-md bg-accent-primary/15 text-accent-primary
                           border border-accent-primary/30 hover:bg-accent-primary/25 transition
                           disabled:opacity-40 inline-flex items-center gap-1 text-xs"
              >
                {sending ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Send className="w-3.5 h-3.5" />}
                {t('agents.card.terminal_send')}
              </button>
            </div>
            {payload && (
              <div className="text-[10px] text-fg-muted">
                {t('agents.card.terminal_send_preview')}: <code className="text-fg-secondary">{payload}</code>
              </div>
            )}
          </div>
        )}

        {/* Footer */}
        <div className="flex items-center justify-end px-5 py-3 border-t border-border-base">
          <button
            type="button"
            onClick={onClose}
            className="text-xs px-3 py-1.5 rounded-md bg-accent-primary/15 text-accent-primary
                       border border-accent-primary/30 hover:bg-accent-primary/25 transition"
          >
            {t('common.close')}
          </button>
        </div>
      </div>
    </div>
  );
}
