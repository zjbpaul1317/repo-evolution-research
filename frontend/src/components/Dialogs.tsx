import { useRef, useState } from 'react';
import {
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronRight,
  CircleHelp,
  FileJson,
  FolderOpen,
  GitBranch,
  Info,
  LoaderCircle,
  Search,
  Terminal,
  Upload,
} from 'lucide-react';
import type { Capability, Dataset, Evidence, Page, Topic } from '../data';
import { dateLabel, sourceLabel, topicEvidence } from '../data';
import { buildCommand, readSnapshot } from '../import';
import { Badge, CopyButton, Empty, ExternalLink, Modal } from './UI';

export function NewResearchDialog({
  close,
  copy,
  copied,
}: {
  close: () => void;
  copy: (text: string) => void;
  copied: boolean;
}) {
  const [repo, setRepo] = useState('');
  const [mode, setMode] = useState('standard');
  const [days, setDays] = useState(90);
  const [focus, setFocus] = useState('');
  const [command, setCommand] = useState('');
  const [error, setError] = useState('');
  const modes = [
    { key: 'quick', title: '快速探索', days: 30, budget: 12, detail: '从一个问题开始' },
    { key: 'standard', title: '标准研究', days: 90, budget: 40, detail: '构建完整的技术脉络' },
    { key: 'deep', title: '深度研究', days: 180, budget: 80, detail: '更长窗口，更深阅读' },
  ];
  return (
    <Modal
      title={command ? '研究命令已就绪' : '开启一次新的探索'}
      subtitle={
        command
          ? '在项目根目录运行命令，再将研究结果导入工作台。'
          : '选定仓库与研究范围，把关注的问题交给研究流程。'
      }
      close={close}
      wide
    >
      {!command ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            try {
              setCommand(buildCommand(repo, mode, days, focus));
              setError('');
            } catch (err) {
              setError((err as Error).message);
            }
          }}
          className="research-form"
        >
          <label className="field-label">
            GitHub 仓库 <span>*</span>
            <div className="input-with-icon">
              <GitBranch size={17} />
              <input
                data-autofocus
                required
                aria-label="GitHub 仓库"
                placeholder="例如 vllm-project/vllm"
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
              />
            </div>
            <small>支持 owner/repo 或 https://github.com/owner/repo</small>
          </label>
          <div className="field-label">
            研究深度
            <div className="mode-grid">
              {modes.map((m) => (
                <button
                  type="button"
                  key={m.key}
                  className={`mode-option ${mode === m.key ? 'active' : ''}`}
                  onClick={() => {
                    setMode(m.key);
                    setDays(m.days);
                  }}
                >
                  <span>
                    {m.title}
                    <span className="radio-dot">{mode === m.key && <Check size={10} />}</span>
                  </span>
                  <strong>
                    {m.days}
                    <small> 天</small>
                  </strong>
                  <p>{m.detail}</p>
                  <small>深读预算 {m.budget} 个实体</small>
                </button>
              ))}
            </div>
          </div>
          <div className="form-row">
            <label className="field-label">
              研究窗口（天）
              <input
                required
                type="number"
                min="1"
                max="365"
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
              />
            </label>
            <label className="field-label">
              关注方向
              <input
                value={focus}
                onChange={(e) => setFocus(e.target.value)}
                placeholder="调度, KV cache, 多模态"
              />
              <small>多个方向以逗号分隔，可留空</small>
            </label>
          </div>
          <div className="form-note">
            <Info size={17} />
            <p>工作台生成 CLI 命令。来源采集在本地终端执行，研究写作由 Agent 完成。</p>
          </div>
          {error && (
            <div role="alert" className="error-message">
              {error}
            </div>
          )}
          <div className="modal-footer">
            <button type="button" className="button secondary" onClick={close}>
              取消
            </button>
            <button type="submit" className="button primary">
              生成研究命令
              <ArrowRight size={16} />
            </button>
          </div>
        </form>
      ) : (
        <div className="command-result">
          <div className="command-header">
            <span>
              <Terminal size={16} />
              研究工作流
            </span>
            <CopyButton value={command} copied={copied} onCopy={copy} />
          </div>
          <pre tabIndex={0}>{command}</pre>
          <div className="form-note">
            <CircleHelp size={16} />
            <p>
              需要 Python 3.11+、jsonschema 和已认证的 GitHub CLI。将 init 输出的实际目录填入
              RUN，再依次执行后续步骤。
            </p>
          </div>
          <div className="modal-footer">
            <button className="button secondary" onClick={() => setCommand('')}>
              修改配置
            </button>
            <button className="button primary" onClick={close}>
              完成
              <Check size={16} />
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

export function ImportDialog({
  close,
  loaded,
  restoreDemo,
}: {
  close: () => void;
  loaded: (data: Dataset) => void;
  restoreDemo: () => void;
}) {
  const filesRef = useRef<HTMLInputElement>(null);
  const directoryRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [drag, setDrag] = useState(false);
  async function load(files: FileList | null) {
    if (!files?.length) return;
    setBusy(true);
    setError('');
    try {
      loaded(await readSnapshot(files));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
      if (filesRef.current) filesRef.current.value = '';
      if (directoryRef.current) directoryRef.current.value = '';
    }
  }
  return (
    <Modal
      title="载入你的研究"
      subtitle="把本地的研究快照，变成可以探索的知识工作台。"
      close={close}
    >
      <input
        ref={filesRef}
        hidden
        type="file"
        multiple
        accept=".json,.jsonl,.md"
        onChange={(e) => void load(e.target.files)}
      />
      <input
        ref={(el) => {
          directoryRef.current = el;
          el?.setAttribute('webkitdirectory', '');
        }}
        hidden
        type="file"
        multiple
        onChange={(e) => void load(e.target.files)}
      />
      <div
        className={`drop-zone ${drag ? 'dragging' : ''}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDrag(true);
        }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDrag(false);
          if (!busy) void load(e.dataTransfer.files);
        }}
      >
        <div className="upload-symbol">
          {busy ? (
            <LoaderCircle size={30} className="spin" />
          ) : (
            <FolderOpen size={30} strokeWidth={1.4} />
          )}
        </div>
        <h3>{busy ? '正在读取快照…' : '研究的下一站，从这里开始'}</h3>
        <p>拖入研究文件，或选择一个完整的 run 目录</p>
        <div>
          <button
            className="button primary"
            disabled={busy}
            onClick={() => directoryRef.current?.click()}
          >
            <FolderOpen size={16} />
            选择 run 目录
          </button>
          <button
            className="button secondary"
            disabled={busy}
            onClick={() => filesRef.current?.click()}
          >
            <Upload size={16} />
            选择文件
          </button>
        </div>
        <small>支持 schema 1.2 / 1.3 · 仅在当前浏览器内读取</small>
      </div>
      <div className="import-files">
        <span>
          <FileJson size={16} />
          <strong>research.json</strong>
          <Badge>必需</Badge>
        </span>
        <p>
          可同时导入
          manifest.json、evidence.jsonl、report.md，获得完整展示。重复导入会替换当前快照；刷新页面后恢复演示。
        </p>
      </div>
      {error && (
        <div className="error-message" role="alert">
          {error}
        </div>
      )}
      <button className="demo-choice" onClick={restoreDemo}>
        <span>
          <span className="demo-icon">N</span>
          <span>
            <strong>先探索演示工作区</strong>
            <small>acme/nebula · 虚构示例 · 6 个研究主题</small>
          </span>
        </span>
        <ChevronRight size={18} />
      </button>
    </Modal>
  );
}

export type Detail =
  | { kind: 'topic'; item: Topic }
  | { kind: 'evidence'; item: Evidence }
  | { kind: 'capability'; item: Capability };
export function DetailDrawer({
  detail,
  data,
  close,
  setDetail,
}: {
  detail: Detail;
  data: Dataset;
  close: () => void;
  setDetail: (detail: Detail) => void;
}) {
  const title = detail.kind === 'evidence' ? '证据详情' : detail.item.title;
  const subtitle = detail.kind === 'evidence' ? detail.item.id : detail.item.id;
  function evidenceLinks(ids: string[]) {
    return (
      <div className="detail-evidence-list">
        {ids.length ? (
          ids.map((id) => {
            const evidence = data.evidence.find((e) => e.id === id);
            return (
              <button
                key={id}
                disabled={!evidence}
                onClick={() => evidence && setDetail({ kind: 'evidence', item: evidence })}
              >
                <BookOpen size={15} />
                <span>
                  <strong>{id}</strong>
                  <small>{evidence ? evidence.text : '该引用的证据尚未载入'}</small>
                </span>
                <ChevronRight size={15} />
              </button>
            );
          })
        ) : (
          <p className="muted">当前没有关联证据。</p>
        )}
      </div>
    );
  }
  return (
    <Modal title={title} subtitle={subtitle} close={close} drawer>
      {detail.kind === 'evidence' && (
        <div className="detail-content">
          <div className="detail-badges">
            <Badge state={detail.item.verification} />
            <Badge state={detail.item.completeness} />
            <span className="source-tag">{sourceLabel(detail.item.source_kind)}</span>
          </div>
          <section>
            <span className="eyebrow">ORIGINAL SOURCE</span>
            <h3>原文摘录</h3>
            <blockquote className="source-quote">{detail.item.text}</blockquote>
            <ExternalLink url={detail.item.source_url} className="text-button">
              前往原始来源
            </ExternalLink>
          </section>
          <section>
            <h3>来源与定位</h3>
            <dl className="metadata-list">
              <dt>来源实体</dt>
              <dd>{detail.item.entity_id}</dd>
              <dt>观察时间</dt>
              <dd>{dateLabel(detail.item.observed_at)}</dd>
              <dt>事件时间</dt>
              <dd>{dateLabel(detail.item.source_event_at ?? '')}</dd>
              <dt>行号</dt>
              <dd>
                {detail.item.locator?.line_start != null
                  ? `L${detail.item.locator.line_start}${detail.item.locator.line_end != null ? `–L${detail.item.locator.line_end}` : ''}`
                  : '未记录'}
              </dd>
              <dt>内容位置</dt>
              <dd>{detail.item.locator?.pointer ?? '未记录'}</dd>
              <dt>SHA-256</dt>
              <dd className="mono tiny">
                {detail.item.blob_sha256 ??
                  (data.source === 'demo' ? '演示内容未生成 hash' : '未载入')}
              </dd>
            </dl>
          </section>
          <section>
            <h3>支持的研究判断</h3>
            {data.claims
              .filter((c) => c.evidence_ids.includes(detail.item.id))
              .map((c) => (
                <div className="claim-card" key={c.id}>
                  <Badge state={c.kind} />
                  <p>{c.text}</p>
                  <small>{c.confidence_reason}</small>
                </div>
              ))}
            {!data.claims.some((c) => c.evidence_ids.includes(detail.item.id)) && (
              <p className="muted">当前研究未直接引用这条证据。</p>
            )}
          </section>
        </div>
      )}
      {detail.kind === 'topic' && (
        <div className="detail-content">
          <section>
            <span className="eyebrow">RESEARCH QUESTION</span>
            <h3 className="large-question">{detail.item.research_question}</h3>
            <p>{detail.item.summary}</p>
          </section>
          <section>
            <h3>研究判断</h3>
            {data.claims
              .filter((c) => Object.values(detail.item.claim_ids).flat().includes(c.id))
              .map((c) => (
                <div className="claim-card" key={c.id}>
                  <div className="claim-header">
                    <Badge state={c.kind} />
                    <span className="tiny">
                      置信度：
                      {c.confidence === 'high' ? '高' : c.confidence === 'medium' ? '中' : '低'}
                    </span>
                  </div>
                  <p>{c.text}</p>
                  <small>{c.confidence_reason}</small>
                  {evidenceLinks(c.evidence_ids)}
                </div>
              ))}
          </section>
          <section>
            <h3>关联能力</h3>
            {data.capabilities
              .filter((c) => c.topic_id === detail.item.id)
              .map((c) => (
                <button
                  className="detail-capability"
                  key={c.id}
                  onClick={() => setDetail({ kind: 'capability', item: c })}
                >
                  <GitBranch size={17} />
                  <span>{c.title}</span>
                  <Badge state={c.implementation_state} />
                  <ChevronRight size={15} />
                </button>
              ))}
          </section>
          <section>
            <h3>研究缺口</h3>
            {detail.item.gaps.map((g, i) => (
              <div className="gap-note" key={i}>
                <Info size={15} />
                {g}
              </div>
            ))}
            {!detail.item.gaps.length && <p className="muted">研究数据未列出缺口。</p>}
          </section>
          <section>
            <h3>
              主题证据{' '}
              <span className="count-label">{topicEvidence(data, detail.item).length}</span>
            </h3>
            {evidenceLinks(topicEvidence(data, detail.item).map((e) => e.id))}
          </section>
        </div>
      )}
      {detail.kind === 'capability' && (
        <div className="detail-content">
          <p>{detail.item.scope}</p>
          <div className="capability-states">
            {[
              ['提案', detail.item.proposal_state],
              ['实现', detail.item.implementation_state],
              ['发布', detail.item.delivery_state],
              ['可用性', detail.item.availability],
            ].map(([label, state], i) => (
              <div key={label}>
                <span className="mono tiny">0{i + 1}</span>
                <strong>{label}</strong>
                <Badge state={state} />
              </div>
            ))}
          </div>
          <section>
            <h3>实现范围</h3>
            {detail.item.requirements.done.map((r, i) => (
              <div className="requirement" key={`done-${i}`}>
                <Check size={16} />
                <span>{r}</span>
                <Badge>已完成</Badge>
              </div>
            ))}
            {detail.item.requirements.remaining.map((r, i) => (
              <div className="requirement pending" key={`remaining-${i}`}>
                <CircleHelp size={16} />
                <span>{r}</span>
                <Badge>待完成</Badge>
              </div>
            ))}
          </section>
          <section>
            <h3>版本与分支</h3>
            <dl className="metadata-list">
              <dt>目标分支</dt>
              <dd>{detail.item.target_branch ?? '未记录'}</dd>
              <dt>观察提交</dt>
              <dd className="mono">{detail.item.observed_revision ?? '未记录'}</dd>
              <dt>已核验版本</dt>
              <dd>{detail.item.verified_versions.join('、') || '没有版本核验证据'}</dd>
            </dl>
          </section>
          <section>
            <h3>关联证据</h3>
            {evidenceLinks(detail.item.evidence_ids)}
          </section>
        </div>
      )}
    </Modal>
  );
}

export function SearchDialog({
  data,
  close,
  navigate,
  select,
}: {
  data: Dataset;
  close: () => void;
  navigate: (page: Page) => void;
  select: (detail: Detail) => void;
}) {
  const [query, setQuery] = useState('');
  const term = query.trim().toLowerCase();
  const results: { kind: string; title: string; description: string; detail: Detail }[] = term
    ? [
        ...data.topics
          .filter((t) => `${t.title} ${t.summary}`.toLowerCase().includes(term))
          .map((t) => ({
            kind: '技术主题',
            title: t.title,
            description: t.research_question,
            detail: { kind: 'topic' as const, item: t },
          })),
        ...data.capabilities
          .filter((c) => `${c.title} ${c.scope}`.toLowerCase().includes(term))
          .map((c) => ({
            kind: '能力进展',
            title: c.title,
            description: c.scope,
            detail: { kind: 'capability' as const, item: c },
          })),
        ...data.evidence
          .filter((e) => `${e.id} ${e.text}`.toLowerCase().includes(term))
          .map((e) => ({
            kind: '证据',
            title: e.id,
            description: e.text,
            detail: { kind: 'evidence' as const, item: e },
          })),
      ]
    : [];
  return (
    <Modal title="搜索研究工作区" close={close}>
      <label className="search-field palette-search">
        <Search size={20} />
        <input
          data-autofocus
          placeholder="搜索主题、能力、证据原文…"
          aria-label="全局搜索"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <kbd>ESC</kbd>
      </label>
      <div className="search-results">
        {term ? (
          <>
            {results.slice(0, 30).map((r, i) => (
              <button key={i} onClick={() => select(r.detail)}>
                <span className="source-tag">{r.kind}</span>
                <span>
                  <strong>{r.title}</strong>
                  <small>{r.description}</small>
                </span>
                <ArrowUpRight size={15} />
              </button>
            ))}
            {!results.length && <Empty />}
            {results.length > 30 && (
              <p className="muted tiny">展示前 30 项，请输入更具体的关键词。</p>
            )}
          </>
        ) : (
          <>
            <span className="eyebrow">快速前往</span>
            {(
              [
                { page: 'topics', title: '探索技术主题' },
                { page: 'evidence', title: '查看证据账本' },
                { page: 'report', title: '阅读研究报告' },
              ] as { page: Page; title: string }[]
            ).map((r) => (
              <button
                key={r.page}
                onClick={() => {
                  close();
                  navigate(r.page);
                }}
              >
                <BookOpen size={17} />
                <span>{r.title}</span>
                <ArrowRight size={15} />
              </button>
            ))}
          </>
        )}
      </div>
    </Modal>
  );
}
