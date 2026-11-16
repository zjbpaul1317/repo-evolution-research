import {
  ArrowDownToLine,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronRight,
  FileSearch,
  GitBranch,
  GitPullRequest,
  Layers3,
  ScanLine,
  Sparkles,
} from 'lucide-react';
import type { Dataset, Evidence, Page, Topic } from '../data';
import { dateLabel, sourceLabel, topicColors, topicEvidence } from '../data';
import { Badge } from './UI';
import { ActivityChart, EvolutionVisual, workflow } from './Visuals';

type Props = {
  data: Dataset;
  navigate: (page: Page) => void;
  openTopic: (topic: Topic) => void;
  openEvidence: (evidence: Evidence) => void;
  newResearch: () => void;
  exportReport: () => void;
};
export default function Overview({
  data,
  navigate,
  openTopic,
  openEvidence,
  newResearch,
  exportReport,
}: Props) {
  const unknowns = data.claims.filter((c) => c.kind === 'unknown').length;
  const merged = data.capabilities.filter((c) => c.implementation_state === 'merged').length;
  const metrics = [
    {
      icon: ScanLine,
      label: '来源证据',
      english: 'EVIDENCE',
      value: data.evidence.length,
      footer: `${new Set(data.evidence.map((e) => sourceLabel(e.source_kind))).size} 类来源已载入`,
      page: 'evidence' as Page,
      graph: 'one',
    },
    {
      icon: Layers3,
      label: '技术主题',
      english: 'RESEARCH TOPICS',
      value: data.topics.length,
      footer: '连接变化，理解设计动机',
      page: 'topics' as Page,
      graph: 'two',
    },
    {
      icon: GitBranch,
      label: '能力追踪',
      english: 'CAPABILITIES',
      value: data.capabilities.length,
      footer: `${merged} 项实现状态为已合并`,
      page: 'capabilities' as Page,
      graph: 'three',
    },
    {
      icon: FileSearch,
      label: '待解问题',
      english: 'OPEN QUESTIONS',
      value: unknowns,
      footer: '保留未知，让判断有边界',
      page: 'topics' as Page,
      graph: 'four',
    },
  ];
  return (
    <div className="page-content overview">
      <div className="page-heading">
        <div>
          <div className="eyebrow">
            <span className="live-dot" /> YOUR RESEARCH, CONNECTED
          </div>
          <h1>
            研究总览<span className="heading-dot">.</span>
          </h1>
          <p>穿过代码的变化，看见项目演进的脉络。</p>
        </div>
        <div className="heading-actions">
          <button className="button secondary" onClick={exportReport}>
            <ArrowDownToLine size={16} />
            导出报告
          </button>
          <button className="button primary" onClick={newResearch}>
            <span className="plus">+</span>新建研究
          </button>
        </div>
      </div>

      <section className="hero-panel">
        <div className="hero-copy">
          <span className="hero-eyebrow">
            <span /> FROM COMMITS TO CLARITY
          </span>
          <h2>
            变化不止于代码。
            <br />
            洞察，<span>有迹可循。</span>
          </h2>
          <p>
            连接提案、实现与发布，
            <br />
            把零散的技术信号，变成有据可查的研究。
          </p>
          <button className="hero-link" onClick={() => navigate('report')}>
            探索研究报告
            <ArrowUpRight size={17} />
          </button>
        </div>
        <EvolutionVisual />
        <span className="hero-index">RESEARCH SYSTEM / 001</span>
      </section>

      <div className="repo-strip">
        <div className="repo-identity">
          <span className="repo-avatar">
            {data.repo.split('/').at(-1)?.[0]?.toUpperCase() ?? 'R'}
          </span>
          <div>
            <span className="tiny">当前研究仓库</span>
            <strong>
              {data.repo.includes('/') ? (
                <>
                  <span>{data.repo.split('/')[0]} / </span>
                  {data.repo.split('/').slice(1).join('/')}
                </>
              ) : (
                data.repo
              )}
            </strong>
          </div>
          <Badge>{data.source === 'demo' ? '演示快照' : '本地快照'}</Badge>
        </div>
        <div className="repo-period">
          <span className="tiny">研究窗口</span>
          <span>
            {dateLabel(data.start)} <span className="muted">—</span> {dateLabel(data.end)}
          </span>
        </div>
        <button
          className="icon-button"
          aria-label="查看研究快照说明"
          onClick={() => navigate('guide')}
        >
          <ChevronRight size={18} />
        </button>
      </div>

      <div className="metrics-grid">
        {metrics.map(({ icon: Icon, ...metric }, i) => (
          <button
            className="metric-card"
            key={metric.label}
            onClick={() => navigate(metric.page)}
            style={{ '--card-delay': `${i * 70}ms` } as React.CSSProperties}
          >
            <div className="metric-top">
              <span>{metric.label}</span>
              <Icon size={17} strokeWidth={1.6} />
            </div>
            <div className="metric-number">
              <strong>{String(metric.value).padStart(2, '0')}</strong>
              <div className={`mini-bars ${metric.graph}`} aria-hidden="true">
                {[30, 48, 38, 65, 47, 75, 61, 88, 76, 100].map((h, j) => (
                  <i key={j} style={{ height: `${h}%` }} />
                ))}
              </div>
            </div>
            <div className="metric-footer">
              <span>{metric.footer}</span>
              <ArrowUpRight size={13} />
            </div>
          </button>
        ))}
      </div>

      <div className="overview-middle">
        <ActivityChart data={data} />
        <section className="panel workflow-panel">
          <div className="panel-heading">
            <div>
              <h3>从来源，到洞察</h3>
              <p>一条可追溯的研究路径</p>
            </div>
            <span className="icon-label">
              <Sparkles size={17} />
            </span>
          </div>
          <div className="workflow-list">
            {workflow.map(({ icon: Icon, title, code }, i) => (
              <button key={code} onClick={() => navigate('guide')}>
                <span className={`workflow-node ${i === 2 ? 'agent-node' : ''}`}>
                  <Icon size={16} />
                </span>
                <span className="workflow-title">
                  {title}
                  <span>{code}</span>
                </span>
                {i === 2 ? (
                  <span className="agent-tag">Agent</span>
                ) : (
                  <Check size={13} className="muted" />
                )}
              </button>
            ))}
          </div>
          <div className="workflow-caption">CLI 负责确定性流程 · Agent 负责研究判断</div>
        </section>
      </div>

      <section className="topics-section">
        <div className="section-heading">
          <div>
            <h3>
              值得关注的技术方向{' '}
              <span className="count-label">{String(data.topics.length).padStart(2, '0')}</span>
            </h3>
            <p>从设计动机到实现进展，把变化放回上下文。</p>
          </div>
          <button className="text-button" onClick={() => navigate('topics')}>
            全部主题
            <ArrowRight size={15} />
          </button>
        </div>
        <div className="topic-preview-grid">
          {data.topics.slice(0, 3).map((topic, i) => (
            <button
              className="topic-preview"
              key={topic.id}
              onClick={() => openTopic(topic)}
              style={
                { '--topic-color': topicColors[i % topicColors.length] } as React.CSSProperties
              }
            >
              <div className="topic-card-top">
                <span className="topic-glyph">
                  <GitBranch size={22} strokeWidth={1.4} />
                </span>
                <span className="mono tiny">0{i + 1} / RESEARCH</span>
                <ArrowUpRight size={17} />
              </div>
              <h3>{topic.title}</h3>
              <p>{topic.summary}</p>
              <div className="topic-card-bottom">
                <span>
                  <BookOpen size={13} />
                  {topicEvidence(data, topic).length} 条证据
                </span>
                <span>
                  {topic.capability_ids.length} 项能力
                  <ChevronRight size={14} />
                </span>
              </div>
            </button>
          ))}
        </div>
        {data.topics.length === 0 && <p className="muted">该研究尚未包含技术主题。</p>}
      </section>

      <section className="panel latest-panel">
        <div className="panel-heading">
          <div>
            <h3>
              证据账本 <span className="tiny">EVERY CLAIM HAS A SOURCE</span>
            </h3>
            <p>原始内容、来源与定位，让研究可以被复核。</p>
          </div>
          <button className="text-button" onClick={() => navigate('evidence')}>
            查看全部
            <ArrowRight size={15} />
          </button>
        </div>
        <div className="recent-evidence">
          {data.evidence.slice(0, 4).map((e) => (
            <button key={e.id} onClick={() => openEvidence(e)}>
              <span className="source-icon">
                <GitPullRequest size={17} />
              </span>
              <div>
                <strong>{e.text}</strong>
                <span>
                  {e.id}
                  <i>·</i>
                  {sourceLabel(e.source_kind)}
                </span>
              </div>
              <Badge state={e.verification} />
              <ChevronRight size={15} />
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}
