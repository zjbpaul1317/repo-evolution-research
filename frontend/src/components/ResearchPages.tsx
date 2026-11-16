import { useMemo, useState } from 'react';
import {
  ArrowDownToLine,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Bookmark,
  Check,
  ChevronRight,
  Code2,
  FileText,
  Filter,
  GitBranch,
  Info,
  Search,
  ShieldCheck,
  SlidersHorizontal,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Capability, Dataset, Evidence, Topic } from '../data';
import { dateLabel, makeReport, sourceLabel, topicColors, topicEvidence } from '../data';
import { Badge, Empty, ExternalLink } from './UI';
import { workflow } from './Visuals';

export function PageHeading({
  eyebrow,
  title,
  description,
  children,
}: {
  eyebrow: string;
  title: string;
  description: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="page-heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>
          {title}
          <span className="heading-dot">.</span>
        </h1>
        <p>{description}</p>
      </div>
      {children && <div className="heading-actions">{children}</div>}
    </div>
  );
}

export function TopicsPage({
  data,
  openTopic,
}: {
  data: Dataset;
  openTopic: (topic: Topic) => void;
}) {
  const [query, setQuery] = useState('');
  const [onlyOpen, setOnlyOpen] = useState(false);
  const topics = data.topics.filter(
    (t) =>
      `${t.title} ${t.summary}`.toLowerCase().includes(query.toLowerCase()) &&
      (!onlyOpen || t.gaps.length),
  );
  return (
    <div className="page-content">
      <PageHeading
        eyebrow="FOLLOW THE IDEAS"
        title="技术主题"
        description="不仅知道改了什么，也理解为什么改变。"
      />
      <div className="toolbar">
        <label className="search-field">
          <Search size={16} />
          <input
            aria-label="搜索技术主题"
            placeholder="搜索技术方向、设计或问题…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </label>
        <button
          className={`button secondary ${onlyOpen ? 'selected-button' : ''}`}
          onClick={() => setOnlyOpen(!onlyOpen)}
        >
          <Filter size={15} />
          仅看存在缺口的主题{onlyOpen && <Check size={14} />}
        </button>
        <span className="muted tiny">{topics.length} 个主题</span>
      </div>
      <div className="full-topics-grid">
        {topics.map((topic) => {
          const index = data.topics.indexOf(topic);
          const evidence = topicEvidence(data, topic);
          return (
            <button
              key={topic.id}
              className="full-topic-card"
              onClick={() => openTopic(topic)}
              style={
                { '--topic-color': topicColors[index % topicColors.length] } as React.CSSProperties
              }
            >
              <div className="topic-card-top">
                <span className="topic-glyph">
                  <GitBranch size={25} strokeWidth={1.4} />
                </span>
                <span className="mono tiny">TOPIC {String(index + 1).padStart(2, '0')}</span>
                <ArrowUpRight size={18} />
              </div>
              <h2>{topic.title}</h2>
              <p className="topic-question">{topic.research_question}</p>
              <p>{topic.summary}</p>
              <div className="topic-capabilities">
                {data.capabilities
                  .filter((c) => c.topic_id === topic.id)
                  .slice(0, 3)
                  .map((c) => (
                    <span key={c.id}>{c.title}</span>
                  ))}
              </div>
              <div className="topic-card-bottom">
                <span>
                  <BookOpen size={14} />
                  {evidence.length} 条证据
                </span>
                <span>
                  {topic.gaps.length} 项研究缺口
                  <ChevronRight size={14} />
                </span>
              </div>
            </button>
          );
        })}
      </div>
      {!topics.length && <Empty />}
    </div>
  );
}

export function EvidencePage({
  data,
  openEvidence,
  bookmarks,
  toggleBookmark,
}: {
  data: Dataset;
  openEvidence: (evidence: Evidence) => void;
  bookmarks: Set<string>;
  toggleBookmark: (id: string) => void;
}) {
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('全部来源');
  const [verification, setVerification] = useState('all');
  const [saved, setSaved] = useState(false);
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState('newest');
  const kinds = ['全部来源', ...new Set(data.evidence.map((e) => sourceLabel(e.source_kind)))];
  const filtered = useMemo(
    () =>
      data.evidence
        .filter(
          (e) =>
            `${e.id} ${e.text} ${e.entity_id}`.toLowerCase().includes(query.toLowerCase()) &&
            (kind === '全部来源' || sourceLabel(e.source_kind) === kind) &&
            (verification === 'all' || e.verification === verification) &&
            (!saved || bookmarks.has(e.id)),
        )
        .sort(
          (a, b) =>
            (sort === 'newest' ? -1 : 1) *
            (new Date(a.source_event_at || a.observed_at).getTime() -
              new Date(b.source_event_at || b.observed_at).getTime()),
        ),
    [data, query, kind, verification, saved, bookmarks, sort],
  );
  const pages = Math.max(1, Math.ceil(filtered.length / 10));
  const current = Math.min(page, pages);
  return (
    <div className="page-content">
      <PageHeading
        eyebrow="EVIDENCE, NOT ASSUMPTIONS"
        title="证据账本"
        description="回到每一条原始来源，复核每一个研究判断。"
      >
        <button
          className={`button secondary ${saved ? 'selected-button' : ''}`}
          onClick={() => {
            setSaved(!saved);
            setPage(1);
          }}
        >
          <Bookmark size={15} fill={saved ? 'currentColor' : 'none'} />
          我的收藏 <span className="count-label">{bookmarks.size}</span>
        </button>
      </PageHeading>
      <div className="ledger-stats">
        <div>
          <span className="live-dot" />
          <strong>{data.evidence.length}</strong>
          <span>已载入证据</span>
        </div>
        <div>
          <ShieldCheck size={17} />
          <strong>{data.evidence.filter((e) => e.verification === 'verified').length}</strong>
          <span>来源标记已核验</span>
        </div>
        <div>
          <SlidersHorizontal size={17} />
          <strong>{data.evidence.filter((e) => e.completeness === 'partial').length}</strong>
          <span>内容不完整</span>
        </div>
      </div>
      <div className="panel ledger-panel">
        <div className="ledger-toolbar">
          <label className="search-field">
            <Search size={16} />
            <input
              aria-label="搜索证据"
              placeholder="搜索内容、证据 ID 或来源…"
              value={query}
              onChange={(e) => {
                setQuery(e.target.value);
                setPage(1);
              }}
            />
          </label>
          <select
            aria-label="核验状态"
            value={verification}
            onChange={(e) => {
              setVerification(e.target.value);
              setPage(1);
            }}
          >
            <option value="all">全部核验状态</option>
            <option value="verified">已核验</option>
            <option value="unverified">未核验</option>
          </select>
          <select aria-label="证据排序" value={sort} onChange={(e) => setSort(e.target.value)}>
            <option value="newest">最新在前</option>
            <option value="oldest">最早在前</option>
          </select>
        </div>
        <div className="source-tabs">
          {kinds.map((k) => (
            <button
              key={k}
              className={kind === k ? 'active' : ''}
              onClick={() => {
                setKind(k);
                setPage(1);
              }}
            >
              {k}
              <span>
                {k === '全部来源'
                  ? data.evidence.length
                  : data.evidence.filter((e) => sourceLabel(e.source_kind) === k).length}
              </span>
            </button>
          ))}
        </div>
        <div className="table-scroll">
          <table className="evidence-table">
            <thead>
              <tr>
                <th>证据 / 原文摘要</th>
                <th>来源类型</th>
                <th>核验状态</th>
                <th>事件日期</th>
                <th>
                  <Bookmark size={14} aria-label="收藏" />
                </th>
              </tr>
            </thead>
            <tbody>
              {filtered.slice((current - 1) * 10, current * 10).map((e) => (
                <tr key={e.id}>
                  <td>
                    <button className="evidence-cell" onClick={() => openEvidence(e)}>
                      <span className="mono">{e.id}</span>
                      <span>{e.text}</span>
                    </button>
                  </td>
                  <td>
                    <span className="source-tag">{sourceLabel(e.source_kind)}</span>
                  </td>
                  <td>
                    <Badge state={e.verification} />
                    {e.completeness === 'partial' && (
                      <span className="tiny amber-text">部分内容</span>
                    )}
                  </td>
                  <td className="mono tiny">{dateLabel(e.source_event_at || e.observed_at)}</td>
                  <td>
                    <button
                      className={`icon-button ${bookmarks.has(e.id) ? 'saved' : ''}`}
                      aria-label={`${bookmarks.has(e.id) ? '取消收藏' : '收藏'} ${e.id}`}
                      onClick={() => toggleBookmark(e.id)}
                    >
                      <Bookmark size={16} fill={bookmarks.has(e.id) ? 'currentColor' : 'none'} />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!filtered.length && (
          <Empty
            title={data.evidence.length ? '没有匹配的证据' : '还没有载入证据'}
            detail={
              data.evidence.length
                ? '调整筛选条件，或试试不同的关键词。'
                : '导入同一 run 的 normalized/evidence.jsonl，即可查看引用原文。'
            }
          />
        )}
        <div className="pagination">
          <span>
            共 {filtered.length} 条 · 第 {current} / {pages} 页
          </span>
          <div>
            <button
              className="button secondary small-button"
              disabled={current <= 1}
              onClick={() => setPage(current - 1)}
            >
              上一页
            </button>
            <button
              className="button secondary small-button"
              disabled={current >= pages}
              onClick={() => setPage(current + 1)}
            >
              下一页
            </button>
          </div>
        </div>
      </div>
      <div className="subtle-note">
        <Info size={14} />
        核验标记来自导入数据。浏览器展示不替代 CLI 的完整性校验。
      </div>
    </div>
  );
}

export function CapabilitiesPage({
  data,
  openCapability,
}: {
  data: Dataset;
  openCapability: (capability: Capability) => void;
}) {
  const [query, setQuery] = useState('');
  const [topicId, setTopicId] = useState('all');
  const [onlyUnknown, setOnlyUnknown] = useState(false);
  const capabilities = data.capabilities.filter(
    (c) =>
      `${c.title} ${c.scope}`.toLowerCase().includes(query.toLowerCase()) &&
      (topicId === 'all' || c.topic_id === topicId) &&
      (!onlyUnknown || c.delivery_state === 'unknown' || c.availability === 'unknown'),
  );
  return (
    <div className="page-content">
      <PageHeading
        eyebrow="A CLEARER PICTURE OF PROGRESS"
        title="能力进展"
        description="提案、实现、发布、可用性。四个维度，独立追踪。"
      />
      <div className="state-explainer">
        <ShieldCheck size={22} />
        <div>
          <strong>每一步进展，都需要自己的证据。</strong>
          <p>代码已合并，不代表进入正式版本；版本已发布，也不代表所有场景已验证。</p>
        </div>
        <span className="mono tiny">4 DIMENSIONS</span>
      </div>
      <div className="toolbar">
        <label className="search-field">
          <Search size={16} />
          <input
            value={query}
            aria-label="搜索能力"
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索能力或实现范围…"
          />
        </label>
        <select
          aria-label="按技术主题筛选"
          value={topicId}
          onChange={(e) => setTopicId(e.target.value)}
        >
          <option value="all">全部技术主题</option>
          {data.topics.map((t) => (
            <option key={t.id} value={t.id}>
              {t.title}
            </option>
          ))}
        </select>
        <button
          className={`button secondary ${onlyUnknown ? 'selected-button' : ''}`}
          onClick={() => setOnlyUnknown(!onlyUnknown)}
        >
          <Filter size={15} />
          存在待核验项
        </button>
      </div>
      <div className="panel table-scroll">
        <table className="capabilities-table">
          <thead>
            <tr>
              <th>能力 / 实现范围</th>
              <th>
                <span>01</span>提案
              </th>
              <th>
                <span>02</span>实现
              </th>
              <th>
                <span>03</span>发布
              </th>
              <th>
                <span>04</span>可用性
              </th>
              <th>证据</th>
            </tr>
          </thead>
          <tbody>
            {capabilities.map((c) => (
              <tr key={c.id}>
                <td>
                  <button className="capability-name" onClick={() => openCapability(c)}>
                    <strong>
                      {c.title}
                      <ArrowUpRight size={14} />
                    </strong>
                    <span>{c.scope}</span>
                  </button>
                </td>
                <td>
                  <Badge state={c.proposal_state} />
                </td>
                <td>
                  <Badge state={c.implementation_state} />
                </td>
                <td>
                  <Badge state={c.delivery_state} />
                </td>
                <td>
                  <Badge state={c.availability} />
                </td>
                <td>
                  <button className="evidence-count" onClick={() => openCapability(c)}>
                    {c.evidence_ids.length}
                    <ChevronRight size={13} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {!capabilities.length && <Empty />}
      </div>
    </div>
  );
}

export function ReportPage({ data, exportReport }: { data: Dataset; exportReport: () => void }) {
  const [tab, setTab] = useState('preview');
  const report = useMemo(() => makeReport(data), [data]);
  return (
    <div className="page-content">
      <PageHeading
        eyebrow="KNOWLEDGE, MADE READABLE"
        title="研究报告"
        description="把技术变化整理成有来源、有判断、也有边界的知识。"
      >
        <button className="button primary" onClick={exportReport}>
          <ArrowDownToLine size={16} />
          导出 Markdown
        </button>
      </PageHeading>
      <div className="report-layout">
        <aside className="report-outline">
          <span className="eyebrow">IN THIS RESEARCH</span>
          <h3>阅读导航</h3>
          <a href="#report-document">
            <FileText size={15} />
            研究报告
          </a>
          {data.topics.map((t, i) => (
            <a key={t.id} href={`#report-topic-${i}`}>
              <span className="mono tiny">{String(i + 1).padStart(2, '0')}</span>
              {t.title}
            </a>
          ))}
          <div className="report-meta">
            <span>研究来源</span>
            <strong>{data.source === 'demo' ? '虚构演示快照' : '本地导入快照'}</strong>
            <span>创建时间</span>
            <strong>{dateLabel(data.createdAt)}</strong>
            <span>已载入证据</span>
            <strong>{data.evidence.length} 条</strong>
          </div>
          <div className="outline-note">
            <Info size={15} />
            <p>
              {data.report
                ? '展示导入的 report.md 原文。请以对应 run 的 CLI 校验结果为准。'
                : '当前为浏览器阅读稿。正式报告需由 CLI validate / render 生成。'}
            </p>
          </div>
        </aside>
        <section className="panel report-paper" id="report-document">
          <div className="report-controls">
            <div className="segmented">
              <button
                className={tab === 'preview' ? 'active' : ''}
                onClick={() => setTab('preview')}
              >
                <BookOpen size={14} />
                阅读模式
              </button>
              <button className={tab === 'source' ? 'active' : ''} onClick={() => setTab('source')}>
                <Code2 size={14} />
                Markdown
              </button>
            </div>
            <span className="mono tiny">
              {data.source === 'demo' ? 'DEMO' : 'LOCAL SNAPSHOT'} / zh-CN
            </span>
          </div>
          {tab === 'source' ? (
            <pre className="report-source">{report}</pre>
          ) : (
            <article className="markdown-body">
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  h2: ({ children, ...props }) => {
                    const text = String(children);
                    const index = data.topics.findIndex((t) => text.includes(t.title));
                    return (
                      <h2 {...props} id={index >= 0 ? `report-topic-${index}` : undefined}>
                        {children}
                      </h2>
                    );
                  },
                  a: ({ children, href }) => (
                    <ExternalLink url={href ?? ''}>{children}</ExternalLink>
                  ),
                  img: () => <span className="muted">[外部图片未载入]</span>,
                }}
              >
                {report}
              </ReactMarkdown>
            </article>
          )}
        </section>
      </div>
    </div>
  );
}

export function GuidePage({
  data,
  newResearch,
  importSnapshot,
}: {
  data: Dataset;
  newResearch: () => void;
  importSnapshot: () => void;
}) {
  return (
    <div className="page-content">
      <PageHeading
        eyebrow="BUILT FOR TRACEABLE RESEARCH"
        title="工作流与指南"
        description="从一个 GitHub 仓库开始，到一份可以被复核的研究。"
      />
      <div className="guide-intro">
        <div>
          <span className="eyebrow">REPOSITORY EVOLUTION RESEARCH</span>
          <h2>
            保留证据，
            <br />
            让洞察经得起追问。
          </h2>
          <p>
            CLI 处理采集、存储、归一化和校验。Agent
            阅读材料、组织主题、撰写判断。这个工作台让研究结果更易于探索。
          </p>
          <button className="button primary" onClick={newResearch}>
            配置一次研究
            <ArrowRight size={16} />
          </button>
        </div>
        <div className="guide-orbit">
          <GitBranch size={66} strokeWidth={1} />
          <span>source → evidence → insight</span>
        </div>
      </div>
      <div className="guide-steps">
        {workflow.map(({ icon: Icon, title, code, description }, i) => (
          <div key={code} className="panel guide-step">
            <span className="step-index">0{i + 1}</span>
            <Icon size={23} strokeWidth={1.5} />
            <h3>{title}</h3>
            <code>{code}</code>
            <p>{description}</p>
          </div>
        ))}
      </div>
      <div className="guide-bottom">
        <section className="panel">
          <h3>把已有研究带到这里</h3>
          <p>选择一个 run 目录，或多选以下文件。数据只在当前浏览器会话中读取。</p>
          <div className="file-list">
            <span>
              <FileText size={15} />
              <code>analysis/research.json</code>
              <Badge>必需</Badge>
            </span>
            <span>
              <FileText size={15} />
              <code>manifest.json</code>
              <span>仓库与时间窗口</span>
            </span>
            <span>
              <FileText size={15} />
              <code>normalized/evidence.jsonl</code>
              <span>证据原文</span>
            </span>
            <span>
              <FileText size={15} />
              <code>report.md</code>
              <span>原始报告</span>
            </span>
          </div>
          <button className="button secondary" onClick={importSnapshot}>
            导入研究快照
            <ArrowUpRight size={15} />
          </button>
        </section>
        <section className="panel">
          <h3>当前研究的边界</h3>
          <div className="limitation-list">
            {[...data.limitations, ...data.warnings].map((l, i) => (
              <p key={i}>
                <Info size={15} />
                <span>{l}</span>
              </p>
            ))}
          </div>
          <a
            className="text-button"
            href="https://github.com/zjbpaul1317/repo-evolution-research"
            target="_blank"
            rel="noreferrer"
          >
            查看项目文档
            <ArrowUpRight size={15} />
          </a>
        </section>
      </div>
    </div>
  );
}
