import { useCallback, useEffect, useState } from 'react';
import {
  ArrowUpRight,
  BookOpen,
  Check,
  ChevronDown,
  ChevronRight,
  Command,
  FileText,
  Github,
  GitBranch,
  HelpCircle,
  LayoutDashboard,
  Layers3,
  Menu,
  Plus,
  Search,
  Sparkles,
  Upload,
  X,
} from 'lucide-react';
import { demoData, makeReport } from './data';
import type { Dataset, Page } from './data';
import { downloadFile, Logo } from './components/UI';
import Overview from './components/Overview';
import {
  CapabilitiesPage,
  EvidencePage,
  GuidePage,
  ReportPage,
  TopicsPage,
} from './components/ResearchPages';
import { DetailDrawer, ImportDialog, NewResearchDialog, SearchDialog } from './components/Dialogs';
import type { Detail } from './components/Dialogs';

const navigation = [
  { id: 'overview' as const, title: '研究总览', icon: LayoutDashboard },
  { id: 'topics' as const, title: '技术主题', icon: Layers3 },
  { id: 'evidence' as const, title: '证据账本', icon: BookOpen },
  { id: 'capabilities' as const, title: '能力进展', icon: GitBranch },
  { id: 'report' as const, title: '研究报告', icon: FileText },
];
function initialPage(): Page {
  const value = new URLSearchParams(window.location.search).get('view');
  return [...navigation.map((n) => n.id), 'guide'].includes(value ?? '')
    ? (value as Page)
    : 'overview';
}
export default function App() {
  const [page, setPage] = useState<Page>(initialPage);
  const [data, setData] = useState<Dataset>(demoData);
  const [modal, setModal] = useState<'new' | 'import' | 'search' | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [bookmarks, setBookmarks] = useState(new Set<string>());
  const [sidebar, setSidebar] = useState(false);
  const [toast, setToast] = useState('');
  const [copied, setCopied] = useState(false);
  const navigate = useCallback((next: Page) => {
    setPage(next);
    setSidebar(false);
    setDetail(null);
    const url = new URL(window.location.href);
    url.searchParams.set('view', next);
    url.hash = '';
    window.history.pushState(null, '', url);
    window.scrollTo({ top: 0, behavior: 'instant' });
  }, []);
  useEffect(() => {
    const pop = () => {
      setPage(initialPage());
      setDetail(null);
      setModal(null);
    };
    window.addEventListener('popstate', pop);
    return () => window.removeEventListener('popstate', pop);
  }, []);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setSidebar(false);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setDetail(null);
        setModal((m) => (m === 'search' ? null : 'search'));
      }
    };
    window.addEventListener('keydown', key);
    return () => window.removeEventListener('keydown', key);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 4000);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 2500);
    return () => clearTimeout(timer);
  }, [copied]);
  useEffect(() => {
    document.title = `${page === 'guide' ? '工作流与指南' : navigation.find((n) => n.id === page)?.title} · Evolution`;
  }, [page]);
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setToast('命令已复制，可以在项目根目录执行。');
    } catch {
      setToast('浏览器未允许剪贴板访问，请选中命令文本手动复制。');
    }
  }
  function exportReport() {
    downloadFile(
      `${data.repo.replace(/[^\w.-]/g, '-')}-${data.source === 'demo' ? 'demo' : data.report ? 'report' : 'reading-draft'}.md`,
      makeReport(data),
    );
    setToast('Markdown 文件已准备下载。');
  }
  function loadData(next: Dataset) {
    setData(next);
    setBookmarks(new Set());
    setModal(null);
    navigate('overview');
    setToast(
      next.source === 'demo'
        ? '已切换到演示工作区。'
        : `已载入 ${next.topics.length} 个主题、${next.evidence.length} 条证据。`,
    );
  }
  function toggleBookmark(id: string) {
    setBookmarks((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }
  const currentTitle =
    page === 'guide' ? '工作流与指南' : navigation.find((n) => n.id === page)?.title;
  return (
    <div className="app-shell">
      <a href="#main-content" className="skip-link">
        跳到主内容
      </a>
      {sidebar && <div className="sidebar-shade" onClick={() => setSidebar(false)} />}
      <aside inert={Boolean(modal || detail)} className={`sidebar ${sidebar ? 'open' : ''}`}>
        <button className="brand" onClick={() => navigate('overview')}>
          <Logo />
          <span>
            evolution<span>REPOSITORY RESEARCH</span>
          </span>
          <span className="brand-period">®</span>
        </button>
        <button className="workspace-switch" onClick={() => setModal('import')}>
          <span className="workspace-avatar">
            <Layers3 size={17} />
          </span>
          <span>
            <strong>研究工作空间</strong>
            <small>{data.source === 'demo' ? 'Demo workspace' : 'Local workspace'}</small>
          </span>
          <ChevronDown size={14} />
        </button>
        <div className="nav-label">
          WORKSPACE <span>01</span>
        </div>
        <nav aria-label="主要导航">
          {navigation.map(({ id, title, icon: Icon }) => (
            <button
              key={id}
              className={`nav-item ${page === id ? 'active' : ''}`}
              onClick={() => navigate(id)}
              aria-current={page === id ? 'page' : undefined}
            >
              <Icon size={18} strokeWidth={1.6} />
              <span>{title}</span>
              {id === 'topics' && <span className="nav-count">{data.topics.length}</span>}
              {id === 'evidence' && <span className="nav-count">{data.evidence.length}</span>}
              {id === page && <span className="nav-active-dot" />}
            </button>
          ))}
        </nav>
        <div className="sidebar-divider" />
        <div className="nav-label">RESOURCES</div>
        <button
          className={`nav-item ${page === 'guide' ? 'active' : ''}`}
          onClick={() => navigate('guide')}
        >
          <HelpCircle size={18} strokeWidth={1.6} />
          <span>工作流与指南</span>
        </button>
        <a
          className="nav-item"
          href="https://github.com/zjbpaul1317/repo-evolution-research"
          target="_blank"
          rel="noreferrer"
        >
          <Github size={18} strokeWidth={1.6} />
          <span>GitHub 项目</span>
          <ArrowUpRight size={13} />
        </a>
        <div className="sidebar-bottom">
          <div className="start-card">
            <span className="start-card-icon">
              <Sparkles size={20} strokeWidth={1.4} />
            </span>
            <strong>下一个洞察，从这里开始</strong>
            <p>
              给好奇心一个方向。
              <br />
              研究你关心的开源项目。
            </p>
            <button onClick={() => setModal('new')}>
              创建研究
              <Plus size={15} />
            </button>
            <div className="start-card-glow" />
          </div>
          <button className="profile-row" onClick={() => setModal('import')}>
            <span className="profile-avatar">R</span>
            <span>
              <strong>Researcher</strong>
              <small>
                <span className="live-dot" />
                Local workspace
              </small>
            </span>
            <ChevronDown size={13} />
          </button>
        </div>
      </aside>
      <div className="main-shell" inert={Boolean(modal || detail || sidebar)}>
        <header className="topbar">
          <div className="breadcrumb">
            <button
              className="icon-button mobile-menu"
              aria-label="打开导航菜单"
              onClick={() => setSidebar(!sidebar)}
            >
              <Menu size={20} />
            </button>
            <span>工作空间</span>
            <ChevronRight size={12} />
            <strong>{currentTitle}</strong>
          </div>
          <div className="topbar-actions">
            <button
              className="global-search"
              aria-label="搜索工作区"
              onClick={() => setModal('search')}
            >
              <Search size={15} />
              <span>搜索工作区…</span>
              <kbd>
                <Command size={10} />K
              </kbd>
            </button>
            <span className="header-separator" />
            <button
              className="icon-button"
              aria-label="查看使用指南"
              onClick={() => navigate('guide')}
            >
              <HelpCircle size={18} />
            </button>
            <button
              className="header-avatar"
              aria-label="切换研究工作区"
              onClick={() => setModal('import')}
            >
              R
            </button>
          </div>
        </header>
        <main id="main-content" tabIndex={-1}>
          <div className="workspace-notice">
            <span>
              <span className={`status-dot ${data.source === 'import' ? 'imported' : ''}`} />
              {data.source === 'demo' ? '演示工作区' : '本地研究快照'}
              <span className="notice-divider">/</span>
              <span className="notice-detail">
                {data.source === 'demo'
                  ? '当前内容为虚构示例，探索有据可查的研究方式。'
                  : '数据仅在本次浏览器会话中读取 · 未执行 CLI 校验'}
              </span>
            </span>
            <button onClick={() => setModal('import')}>
              <Upload size={13} />
              导入快照
              <ArrowUpRight size={13} />
            </button>
          </div>
          <div key={`${page}-${data.runId}`} className="page-transition">
            {page === 'overview' && (
              <Overview
                data={data}
                navigate={navigate}
                openTopic={(item) => setDetail({ kind: 'topic', item })}
                openEvidence={(item) => setDetail({ kind: 'evidence', item })}
                newResearch={() => setModal('new')}
                exportReport={exportReport}
              />
            )}
            {page === 'topics' && (
              <TopicsPage data={data} openTopic={(item) => setDetail({ kind: 'topic', item })} />
            )}
            {page === 'evidence' && (
              <EvidencePage
                data={data}
                openEvidence={(item) => setDetail({ kind: 'evidence', item })}
                bookmarks={bookmarks}
                toggleBookmark={toggleBookmark}
              />
            )}
            {page === 'capabilities' && (
              <CapabilitiesPage
                data={data}
                openCapability={(item) => setDetail({ kind: 'capability', item })}
              />
            )}
            {page === 'report' && <ReportPage data={data} exportReport={exportReport} />}
            {page === 'guide' && (
              <GuidePage
                data={data}
                newResearch={() => setModal('new')}
                importSnapshot={() => setModal('import')}
              />
            )}
          </div>
        </main>
        <footer className="app-footer">
          <span>
            <Logo small />
            Evidence first. Insight follows.
          </span>
          <span>
            REPO EVOLUTION RESEARCH <span className="footer-dot">·</span> v0.2.9
          </span>
          <a
            href="https://github.com/zjbpaul1317/repo-evolution-research/blob/main/LICENSE"
            target="_blank"
            rel="noreferrer"
          >
            OPEN SOURCE
            <ArrowUpRight size={12} />
          </a>
        </footer>
      </div>
      {modal === 'new' && (
        <NewResearchDialog
          close={() => setModal(null)}
          copy={(text) => void copy(text)}
          copied={copied}
        />
      )}
      {modal === 'import' && (
        <ImportDialog
          close={() => setModal(null)}
          loaded={loadData}
          restoreDemo={() => loadData(demoData)}
        />
      )}
      {modal === 'search' && (
        <SearchDialog
          data={data}
          close={() => setModal(null)}
          navigate={navigate}
          select={(d) => {
            setModal(null);
            setDetail(d);
          }}
        />
      )}
      {detail && (
        <DetailDrawer
          detail={detail}
          data={data}
          close={() => setDetail(null)}
          setDetail={setDetail}
        />
      )}
      <div className="toast-region" aria-live="polite" role="status">
        {toast && (
          <div className="toast">
            <Check size={16} />
            <span>{toast}</span>
            <button className="icon-button" aria-label="关闭提示" onClick={() => setToast('')}>
              <X size={14} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
