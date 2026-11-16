import { useMemo, useState } from 'react';
import {
  ArrowUpRight,
  FileText,
  GitBranch,
  GitPullRequest,
  Layers3,
  ScanLine,
  ShieldCheck,
  Sparkles,
} from 'lucide-react';
import type { Dataset } from '../data';
import { dateLabel, sourceLabel } from '../data';

export function EvolutionVisual() {
  return (
    <div className="evolution-visual" aria-hidden="true">
      <div className="visual-grid" />
      <div className="visual-halo" />
      <svg className="orbit-lines" viewBox="0 0 540 300" fill="none">
        <ellipse
          cx="286"
          cy="156"
          rx="172"
          ry="75"
          transform="rotate(-25 286 156)"
          stroke="#bfdc9b"
          strokeOpacity=".14"
        />
        <ellipse
          cx="286"
          cy="156"
          rx="206"
          ry="103"
          transform="rotate(-25 286 156)"
          stroke="#bfdc9b"
          strokeOpacity=".07"
        />
        <path d="M76 83H174L260 152H370L453 220" className="network-path" />
        <path d="M124 239L203 185L260 152L366 70H456" className="network-path secondary-path" />
        <path d="M76 83H174L260 152H370L453 220" className="network-flow" />
        <path d="M124 239L203 185L260 152L366 70H456" className="network-flow second-flow" />
        <circle cx="174" cy="83" r="3" fill="#b8d788" />
        <circle cx="370" cy="152" r="3" fill="#b8d788" />
        <circle cx="366" cy="70" r="3" fill="#b8d788" />
        <path
          d="M434 42v8m-4-4h8M170 144v8m-4-4h8M379 260v8m-4-4h8"
          stroke="#c0df9c"
          strokeOpacity=".4"
        />
      </svg>
      <div className="visual-chip chip-source">
        <GitPullRequest size={17} />
        <span>Pull requests</span>
        <i />
      </div>
      <div className="visual-chip chip-evidence">
        <ScanLine size={17} />
        <span>Evidence</span>
        <span className="chip-number">01–48</span>
      </div>
      <div className="visual-chip chip-report">
        <FileText size={17} />
        <span>Research report</span>
        <ArrowUpRight size={13} />
      </div>
      <div className="visual-core">
        <div className="core-rings" />
        <Layers3 size={38} strokeWidth={1.15} />
        <span>CONNECT THE DOTS</span>
      </div>
      <div className="visual-label">
        <span className="live-dot" /> SIGNAL → INSIGHT
      </div>
    </div>
  );
}

export function ActivityChart({ data }: { data: Dataset }) {
  const [days, setDays] = useState(90);
  const [active, setActive] = useState<number | null>(null);
  const buckets = useMemo(() => {
    const end = new Date(data.end).getTime();
    if (!Number.isFinite(end)) return [];
    const start = end - days * 86400000;
    return Array.from({ length: 12 }, (_, index) => {
      const from = start + ((index * days) / 12) * 86400000;
      const to = start + (((index + 1) * days) / 12) * 86400000;
      const rows = data.evidence.filter((e) => {
        const time = new Date(e.source_event_at || e.observed_at).getTime();
        return time >= from && time < to;
      });
      return {
        from,
        to,
        total: rows.length,
        prs: rows.filter((e) => sourceLabel(e.source_kind) === 'Pull Request').length,
      };
    });
  }, [data, days]);
  const max = Math.max(4, ...buckets.map((b) => b.total));
  const points = buckets.map((b, i) => `${42 + i * 43},${148 - (b.total / max) * 111}`).join(' ');
  const prPoints = buckets.map((b, i) => `${42 + i * 43},${148 - (b.prs / max) * 111}`).join(' ');
  const total = buckets.reduce((n, b) => n + b.total, 0);
  return (
    <section className="panel activity-panel">
      <div className="panel-heading">
        <div>
          <h3>
            来源活动趋势 <span className="tiny">SOURCE ACTIVITY</span>
          </h3>
          <p>按证据事件时间观察研究窗口</p>
        </div>
        <div className="segmented">
          {[30, 90].map((d) => (
            <button
              key={d}
              onClick={() => {
                setDays(d);
                setActive(null);
              }}
              className={days === d ? 'active' : ''}
            >
              {d} 天
            </button>
          ))}
        </div>
      </div>
      <div className="chart-summary">
        <strong>{active === null ? total : (buckets[active]?.total ?? 0)}</strong>
        <span>
          {active === null
            ? '条窗口内证据'
            : `${dateLabel(new Date(buckets[active].from).toISOString()).slice(5)} 起的证据`}
        </span>
        <span className="chart-legend">
          <i />
          全部证据 <i className="purple" />
          PR 证据
        </span>
      </div>
      <div className="chart-wrap">
        <svg viewBox="0 0 550 185" role="img" aria-label={`最近 ${days} 天内共有 ${total} 条证据`}>
          <defs>
            <linearGradient id="chart-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#bce884" stopOpacity=".19" />
              <stop offset="1" stopColor="#bce884" stopOpacity="0" />
            </linearGradient>
          </defs>
          {[0, 1, 2, 3].map((i) => (
            <g key={i}>
              <line
                x1="35"
                x2="526"
                y1={37 + i * 37}
                y2={37 + i * 37}
                stroke="#ffffff"
                strokeOpacity=".055"
                strokeDasharray="3 5"
              />
              <text x="8" y={41 + i * 37} className="chart-label">
                {Math.round(max * (1 - i / 3))}
              </text>
            </g>
          ))}
          {buckets.length > 0 && (
            <>
              <polygon points={`42,148 ${points} 515,148`} fill="url(#chart-fill)" />
              <polyline
                points={prPoints}
                fill="none"
                stroke="#9a9bc7"
                strokeWidth="1.6"
                strokeDasharray="4 5"
              />
              <polyline
                points={points}
                fill="none"
                stroke="#c2ed87"
                strokeWidth="2.5"
                strokeLinejoin="round"
                className="chart-line"
              />
              {buckets.map((b, i) => (
                <g key={i}>
                  {active === i && (
                    <>
                      <line
                        x1={42 + i * 43}
                        x2={42 + i * 43}
                        y1="20"
                        y2="148"
                        stroke="#c2ed87"
                        strokeOpacity=".4"
                      />
                      <circle
                        cx={42 + i * 43}
                        cy={148 - (b.total / max) * 111}
                        r="5"
                        fill="#c2ed87"
                        stroke="#192015"
                        strokeWidth="3"
                      />
                    </>
                  )}
                  {i % 2 === 0 && (
                    <text x={42 + i * 43} y="175" className="chart-label" textAnchor="middle">
                      {dateLabel(new Date(b.from).toISOString()).slice(5)}
                    </text>
                  )}
                </g>
              ))}
            </>
          )}
        </svg>
        <div className="chart-interactions">
          {buckets.map((b, i) => (
            <button
              key={i}
              aria-label={`${dateLabel(new Date(b.from).toISOString())}：${b.total} 条证据`}
              onMouseEnter={() => setActive(i)}
              onMouseLeave={() => setActive(null)}
              onFocus={() => setActive(i)}
              onBlur={() => setActive(null)}
              onClick={() => setActive(active === i ? null : i)}
            />
          ))}
        </div>
      </div>
    </section>
  );
}

export const workflow = [
  {
    icon: GitBranch,
    title: '采集来源',
    code: 'collect',
    description: '从 PR、Issue、RFC、Review、Release 和文档中采集材料，保留原始内容与来源。',
  },
  {
    icon: Layers3,
    title: '整理证据',
    code: 'prepare',
    description: '归一化文本，生成证据账本、关系索引与阅读包，定位每条结论的依据。',
  },
  {
    icon: Sparkles,
    title: '研究分析',
    code: 'Agent',
    description: 'Agent 阅读材料、组织技术主题、解释设计取舍，并明确结论与未知项。',
  },
  {
    icon: ShieldCheck,
    title: '校验引用',
    code: 'validate',
    description: '检查结构、引用定位和状态规则。通过校验不等于语义解释正确。',
  },
  {
    icon: FileText,
    title: '生成报告',
    code: 'render',
    description: '从通过门禁的研究数据生成中文 Markdown 报告，附证据、阅读索引与局限。',
  },
];
