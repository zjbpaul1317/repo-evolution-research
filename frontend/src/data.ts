export type Page = 'overview' | 'topics' | 'evidence' | 'capabilities' | 'report' | 'guide';
export type Evidence = {
  id: string;
  entity_id: string;
  source_url: string;
  source_kind: string;
  text: string;
  observed_at: string;
  source_event_at?: string | null;
  verification: string;
  completeness: string;
  blob_sha256?: string;
  locator?: { line_start?: number | null; line_end?: number | null; pointer?: string | null };
};
export type Topic = {
  id: string;
  title: string;
  research_question: string;
  summary: string;
  claim_ids: Record<string, string[]>;
  capability_ids: string[];
  reading_item_ids: string[];
  gaps: string[];
};
export type Claim = {
  id: string;
  text: string;
  kind: string;
  evidence_ids: string[];
  confidence: string;
  confidence_reason: string;
};
export type Capability = {
  id: string;
  topic_id: string;
  title: string;
  scope: string;
  proposal_state: string;
  implementation_state: string;
  delivery_state: string;
  availability: string;
  target_branch?: string | null;
  observed_revision?: string | null;
  verified_versions: string[];
  requirements: { done: string[]; remaining: string[] };
  evidence_ids: string[];
};
export type Reading = {
  id: string;
  priority: string;
  kind: string;
  title: string;
  url: string;
  why: string;
  order: number;
};
export type Dataset = {
  source: 'demo' | 'import';
  repo: string;
  runId: string;
  createdAt: string;
  start: string;
  end: string;
  topics: Topic[];
  claims: Claim[];
  capabilities: Capability[];
  evidence: Evidence[];
  reading: Reading[];
  limitations: string[];
  report?: string;
  warnings: string[];
};

export const topicColors = ['#c2ed87', '#a6b6ff', '#e8b981', '#87cec8', '#c4a1e5', '#a9bac9'];
const topicDefinitions = [
  [
    'KV Cache 生命周期',
    '从隐式回收到可追踪的资源管理',
    '为何需要显式缓存清理，以及不同调度策略如何协作？',
    '显式清理 API 已合并，跨策略资源释放仍在实现中。研究聚焦缓存所有权与异常路径，发布状态需要进一步核验。',
    '缓存清理接口',
    '跨策略资源释放',
  ],
  [
    '推理调度与吞吐优化',
    '让每一次调度更有上下文',
    '动态批处理如何在吞吐量与尾部延迟之间取舍？',
    '调度器引入请求感知的批处理策略。作者报告了基准收益，长尾请求与混合负载仍需独立验证。',
    '动态批处理',
    '请求优先级调度',
  ],
  [
    '分布式执行架构',
    '计算与通信的边界正在重塑',
    '分离式执行如何改变节点通信与容错设计？',
    '执行层开始拆分控制平面与数据平面，新的通信抽象已进入主分支，节点故障恢复仍有开放讨论。',
    '通信层抽象',
    '节点故障恢复',
  ],
  [
    '模型接入与多模态',
    '更开放的模型，更统一的入口',
    '统一接口如何兼容不同模态的数据与状态？',
    '模型适配协议已有实现，多模态输入校验仍处于提案阶段。不同模型的兼容范围需要逐项确认。',
    '模型适配协议',
    '多模态输入校验',
  ],
  [
    '可观测性与诊断',
    '让系统内部的变化清晰可见',
    '如何把执行链路与性能退化关联起来？',
    '请求追踪已合并，指标导出正在实现。采样开销和跨节点关联的验证尚未完成，不能据此确认生产可用性。',
    '请求链路追踪',
    '指标导出协议',
  ],
  [
    '发布与兼容性',
    '从合并代码到可靠交付',
    '版本发布中有哪些兼容性承诺与未决变更？',
    '配置迁移工具已进入实现，API 废弃策略仍待讨论。研究保留版本、分支与能力状态之间的区别。',
    '配置迁移工具',
    'API 兼容策略',
  ],
];
const snippets = [
  'This proposal introduces an explicit lifecycle boundary. Ownership should remain unambiguous when resources are shared across workers.',
  'The first implementation adds the core interface. Follow-up work is needed for the error path and cross-worker coordination.',
  'We should preserve the existing behavior by default and make the new policy configurable. This avoids breaking existing integrations.',
  'The change was merged into the main branch. A merge alone does not establish inclusion in a release or runtime compatibility.',
  'The benchmark measures this specific configuration. Mixed workloads and long-tail latency need a separate evaluation.',
  'This review identifies an edge case during cancellation. The current patch does not yet cover all cleanup paths.',
  'The release notes mention this component, but the ancestry of the implementation commit has not been verified in this snapshot.',
  'The documentation now describes the interface contract and lists the configuration requirements. Deployment validation remains out of scope.',
];
const kinds = [
  'issue_body',
  'pr_body',
  'review_comment',
  'pr_body',
  'pr_body',
  'review_comment',
  'release_body',
  'doc_text',
];
const demoEventTimes = [1, 3, 2, 5, 3, 6, 4, 7, 3, 5, 4, 5].flatMap((count, bucket) =>
  Array.from({ length: count }, (_, event) =>
    new Date(
      Date.UTC(2026, 7, 15, 8, 30) + (bucket + (event + 1) / (count + 1)) * 7.5 * 86400000,
    ).toISOString(),
  ),
);
export const demoEvidence: Evidence[] = Array.from({ length: 48 }, (_, i) => ({
  id: `ev-demo-${String(i + 1).padStart(4, '0')}`,
  entity_id: `demo/nebula/${kinds[i % 8]}/${1200 + i}`,
  source_url: '',
  source_kind: kinds[i % 8],
  text: snippets[i % 8],
  observed_at: '2026-11-13T08:30:00Z',
  source_event_at: demoEventTimes[i],
  verification: i % 9 === 0 ? 'unverified' : 'verified',
  completeness: i % 11 === 0 ? 'partial' : 'complete',
  locator: { line_start: 12 + i, line_end: 16 + i, pointer: '/body' },
}));
const demoTopics: Topic[] = topicDefinitions.map((t, i) => ({
  id: `topic-${i + 1}`,
  title: t[0],
  research_question: t[2],
  summary: t[3],
  claim_ids: {
    problem: [`claim-${i * 3 + 1}`],
    implementation: [`claim-${i * 3 + 2}`],
    unresolved: [`claim-${i * 3 + 3}`],
  },
  capability_ids: [`cap-${i * 2 + 1}`, `cap-${i * 2 + 2}`],
  reading_item_ids: [`read-${i + 1}`],
  gaps: [
    '尚未执行目标项目测试，也未核验实际部署行为。',
    ...(i % 2 ? [] : ['缺少版本 tag 与合并提交的祖先关系证据。']),
  ],
}));
const demoCapabilities: Capability[] = topicDefinitions.flatMap((t, i) =>
  [0, 1].map((j) => ({
    id: `cap-${i * 2 + j + 1}`,
    topic_id: `topic-${i + 1}`,
    title: t[4 + j],
    scope: t[1],
    proposal_state: 'proposed',
    implementation_state:
      j === 0 ? (i === 5 ? 'in_progress' : 'merged') : i % 2 ? 'proposed' : 'in_progress',
    delivery_state: i === 1 && j === 0 ? 'released' : 'unknown',
    availability: 'unknown',
    target_branch: 'main',
    observed_revision: null,
    verified_versions: i === 1 && j === 0 ? ['v0.8.0-demo'] : [],
    requirements: {
      done: j === 0 ? ['核心接口实现', '基础回归用例'] : ['设计方案'],
      remaining: ['部署验证', ...(j === 1 ? ['边界条件实现'] : [])],
    },
    evidence_ids: demoEvidence.slice(i * 8 + j * 3, i * 8 + j * 3 + 3).map((e) => e.id),
  })),
);
export const demoData: Dataset = {
  source: 'demo',
  repo: 'acme/nebula',
  runId: 'demo-20261113',
  createdAt: '2026-11-13T08:30:00Z',
  start: '2026-08-15T00:00:00+08:00',
  end: '2026-11-13T16:30:00+08:00',
  topics: demoTopics,
  capabilities: demoCapabilities,
  evidence: demoEvidence,
  claims: demoTopics.flatMap((t, i) => [
    {
      id: `claim-${i * 3 + 1}`,
      text: `该方向的提案聚焦于“${topicDefinitions[i][1]}”，要求明确接口边界与兼容范围。`,
      kind: 'observed',
      evidence_ids: [demoEvidence[i * 8].id],
      confidence: 'high',
      confidence_reason: '演示：提案正文直接说明目标。',
    },
    {
      id: `claim-${i * 3 + 2}`,
      text: t.summary,
      kind: 'observed',
      evidence_ids: demoEvidence.slice(i * 8 + 1, i * 8 + 5).map((e) => e.id),
      confidence: 'high',
      confidence_reason: '演示：结论由 PR 正文、对象状态及评审内容共同支持。',
    },
    {
      id: `claim-${i * 3 + 3}`,
      text: t.gaps.join(' '),
      kind: 'unknown',
      evidence_ids: [],
      confidence: 'low',
      confidence_reason: '演示：缺少独立验证及发布证据。',
    },
  ]),
  reading: demoTopics.map((t, i) => ({
    id: `read-${i + 1}`,
    priority: i < 2 ? 'P0' : 'P1',
    kind: i % 2 ? 'pr' : 'rfc',
    title: topicDefinitions[i][1],
    url: '',
    why: t.research_question,
    order: i,
  })),
  limitations: [
    '这是虚构仓库 acme/nebula 的界面演示数据，不代表任何真实项目的研究结论。',
    '采集不包含完整 diff，也不执行目标仓库的测试或部署。',
    '合并、发布与实际可用性分开判断；自动校验不证明语义解释正确。',
  ],
  warnings: [],
};

export const sourceLabel = (kind: string) =>
  kind.startsWith('pr_')
    ? 'Pull Request'
    : /review|comment/.test(kind)
      ? 'Review'
      : /issue/.test(kind)
        ? 'Issue / RFC'
        : /release/.test(kind)
          ? 'Release'
          : /doc/.test(kind)
            ? '文档'
            : /discussion/.test(kind)
              ? 'Discussion'
              : kind;
export const stateLabels: Record<string, string> = {
  proposed: '已提案',
  accepted: '已接受',
  merged: '已合并',
  partially_merged: '部分合并',
  in_progress: '实现中',
  released: '已发布',
  not_found: '未找到',
  unknown: '待核验',
  available: '可用',
  not_started: '未开始',
  not_proposed: '未提案',
  rejected: '已拒绝',
  closed_unmerged: '关闭未合并',
  reverted: '已撤回',
  verified: '已核验',
  unverified: '未核验',
  inferred: '推断',
  observed: '事实',
  complete: '完整',
  partial: '部分',
};
export function safeUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
export function topicEvidence(data: Dataset, topic: Topic) {
  const claimIds = Object.values(topic.claim_ids).flat();
  const ids = new Set([
    ...data.claims.filter((c) => claimIds.includes(c.id)).flatMap((c) => c.evidence_ids),
    ...data.capabilities.filter((c) => c.topic_id === topic.id).flatMap((c) => c.evidence_ids),
  ]);
  return data.evidence.filter((e) => ids.has(e.id));
}
export function dateLabel(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? '时间未知'
    : date
        .toLocaleDateString('zh-CN', {
          year: 'numeric',
          month: '2-digit',
          day: '2-digit',
          timeZone: 'Asia/Shanghai',
        })
        .replaceAll('/', '.');
}
export function makeReport(data: Dataset) {
  if (data.report) return data.report;
  return `# ${data.repo} 技术演进研究\n\n> ${data.source === 'demo' ? '界面演示 · 虚构数据' : '浏览器阅读稿 · 未执行 CLI validate/render，不是正式报告'}\n> 研究窗口：${dateLabel(data.start)} — ${dateLabel(data.end)}\n> Run：${data.runId}\n\n## 研究概览\n\n本次研究包含 ${data.topics.length} 个技术主题、${data.capabilities.length} 项能力与 ${data.evidence.length} 条已载入证据。\n\n${data.topics
    .map(
      (t, i) =>
        `## ${String(i + 1).padStart(2, '0')} · ${t.title}\n\n**研究问题**：${t.research_question}\n\n${t.summary}\n\n${data.claims
          .filter((c) => Object.values(t.claim_ids).flat().includes(c.id))
          .map(
            (c) =>
              `- **${stateLabels[c.kind] ?? c.kind}**：${c.text}${c.evidence_ids.length ? `（证据：${c.evidence_ids.map((id) => `\`${id}\``).join('、')}）` : ''}`,
          )
          .join('\n')}\n\n${t.gaps.length ? `**待补充**：${t.gaps.join('；')}` : ''}`,
    )
    .join(
      '\n\n',
    )}\n\n## 能力进展\n\n| 能力 | 提案 | 实现 | 发布 | 可用性 |\n| --- | --- | --- | --- | --- |\n${data.capabilities.map((c) => `| ${c.title.replaceAll('|', '\\|')} | ${stateLabels[c.proposal_state] ?? c.proposal_state} | ${stateLabels[c.implementation_state] ?? c.implementation_state} | ${stateLabels[c.delivery_state] ?? c.delivery_state} | ${stateLabels[c.availability] ?? c.availability} |`).join('\n')}\n\n## 推荐阅读\n\n${data.reading.map((r, i) => `${i + 1}. ${safeUrl(r.url) ? `[${r.title.replaceAll('[', '\\[').replaceAll(']', '\\]')}](${safeUrl(r.url)!.replaceAll(')', '%29')})` : r.title} — ${r.why}`).join('\n')}\n\n## 方法与局限\n\n${[...data.limitations, ...data.warnings].map((l) => `- ${l}`).join('\n')}\n\n## 证据附录\n\n${data.evidence
    .map(
      (e) =>
        `### ${e.id}\n\n来源：${sourceLabel(e.source_kind)}${safeUrl(e.source_url) ? ` · ${safeUrl(e.source_url)}` : ' · 演示来源'}\n\n${e.text
          .split('\n')
          .map((l) => `> ${l}`)
          .join('\n')}`,
    )
    .join('\n\n')}\n`;
}
