import { useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';
import { ArrowUpRight, Check, CircleHelp, Copy, X } from 'lucide-react';
import { safeUrl, stateLabels } from '../data';

export function Logo({ small = false }: { small?: boolean }) {
  return (
    <span className={`brand-mark ${small ? 'small' : ''}`} aria-hidden="true">
      <svg viewBox="0 0 40 40" fill="none">
        <path
          d="M10 11h21L12 21h17L10 31h21"
          stroke="currentColor"
          strokeWidth="3.4"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  );
}
export function Badge({ state, children }: { state?: string; children?: ReactNode }) {
  const color = /^(merged|released|verified|complete|available|accepted)$/.test(state ?? '')
    ? 'green'
    : /^(in_progress|partially_merged|partial|inferred)$/.test(state ?? '')
      ? 'amber'
      : /^(proposed|observed)$/.test(state ?? '')
        ? 'violet'
        : 'neutral';
  return (
    <span className={`badge ${color}`}>
      <span />
      {children ?? stateLabels[state ?? ''] ?? state}
    </span>
  );
}
export function Empty({
  title = '没有找到匹配的内容',
  detail = '试试其他关键词，或调整筛选条件。',
  action,
}: {
  title?: string;
  detail?: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <CircleHelp size={28} strokeWidth={1.3} />
      <h3>{title}</h3>
      <p>{detail}</p>
      {action}
    </div>
  );
}
export function ExternalLink({
  url,
  children,
  className = '',
}: {
  url: string;
  children: ReactNode;
  className?: string;
}) {
  if (url.startsWith('#'))
    return (
      <a href={url} className={className}>
        {children}
      </a>
    );
  const href = safeUrl(url);
  return href ? (
    <a href={href} target="_blank" rel="noreferrer" className={className}>
      {children}
      <ArrowUpRight size={14} />
    </a>
  ) : (
    <span className={`muted ${className}`}>
      {children}
      <span className="tiny">未提供来源链接</span>
    </span>
  );
}
export function Modal({
  title,
  subtitle,
  children,
  close,
  wide = false,
  drawer = false,
}: {
  title: string;
  subtitle?: string;
  children: ReactNode;
  close: () => void;
  wide?: boolean;
  drawer?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const id = useId();
  const onClose = useRef(close);
  onClose.current = close;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const frame = requestAnimationFrame(() => {
      const auto = ref.current?.querySelector<HTMLElement>('[data-autofocus]');
      (auto ?? ref.current)?.focus();
    });
    function key(event: KeyboardEvent) {
      if (event.key === 'Escape') {
        event.preventDefault();
        onClose.current();
      }
      if (event.key !== 'Tab') return;
      const items = Array.from(
        ref.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), a[href], input, select, textarea, [tabindex="0"]',
        ) ?? [],
      ).filter((el) => el.getClientRects().length);
      if (!items.length) {
        event.preventDefault();
        return;
      }
      const first = items[0],
        last = items[items.length - 1];
      if (!ref.current?.contains(document.activeElement)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
        return;
      }
      if (
        event.shiftKey &&
        (document.activeElement === first || document.activeElement === ref.current)
      ) {
        event.preventDefault();
        last.focus();
      } else if (
        !event.shiftKey &&
        (document.activeElement === last || document.activeElement === ref.current)
      ) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener('keydown', key);
    return () => {
      cancelAnimationFrame(frame);
      document.body.style.overflow = oldOverflow;
      document.removeEventListener('keydown', key);
      previous?.focus();
    };
  }, []);
  return (
    <div
      className={`modal-backdrop ${drawer ? 'drawer-backdrop' : ''}`}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) close();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={id}
        tabIndex={-1}
        ref={ref}
        className={`modal ${wide ? 'wide' : ''} ${drawer ? 'drawer' : ''}`}
      >
        <div className="modal-heading">
          <div>
            <span className="eyebrow">EVOLUTION WORKSPACE</span>
            <h2 id={id}>{title}</h2>
            {subtitle && <p>{subtitle}</p>}
          </div>
          <button className="icon-button" aria-label="关闭弹窗" onClick={close}>
            <X size={20} />
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
export function CopyButton({
  value,
  copied,
  onCopy,
}: {
  value: string;
  copied: boolean;
  onCopy: (value: string) => void;
}) {
  return (
    <button className="button secondary small-button" onClick={() => onCopy(value)}>
      {copied ? <Check size={15} /> : <Copy size={15} />}
      {copied ? '已复制' : '复制命令'}
    </button>
  );
}
export function downloadFile(
  filename: string,
  content: string,
  type = 'text/markdown;charset=utf-8',
) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
