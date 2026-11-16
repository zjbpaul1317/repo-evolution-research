import { z } from 'zod';
import type { Dataset } from './data';

const strings = z.array(z.string());
const topic = z.object({
  id: z.string(),
  title: z.string(),
  research_question: z.string(),
  summary: z.string(),
  claim_ids: z.record(strings),
  capability_ids: strings,
  reading_item_ids: strings,
  gaps: strings,
});
const claim = z.object({
  id: z.string(),
  text: z.string(),
  kind: z.string(),
  evidence_ids: strings,
  confidence: z.string(),
  confidence_reason: z.string(),
});
const capability = z.object({
  id: z.string(),
  topic_id: z.string(),
  title: z.string(),
  scope: z.string(),
  proposal_state: z.string(),
  implementation_state: z.string(),
  delivery_state: z.string(),
  availability: z.string(),
  target_branch: z.string().nullable().optional(),
  observed_revision: z.string().nullable().optional(),
  verified_versions: strings,
  requirements: z.object({ done: strings, remaining: strings }),
  evidence_ids: strings,
});
const reading = z.object({
  id: z.string(),
  priority: z.string(),
  kind: z.string(),
  title: z.string(),
  url: z.string(),
  why: z.string(),
  order: z.number(),
});
const researchSchema = z.object({
  schema_version: z.enum(['1.2', '1.3']),
  run_id: z.string().min(1),
  created_at: z.string(),
  topics: z.array(topic),
  claims: z.array(claim),
  capabilities: z.array(capability),
  reading_list: z.array(reading),
  limitations: strings,
});
const evidenceSchema = z.object({
  id: z.string(),
  entity_id: z.string(),
  source_url: z.string(),
  source_kind: z.string(),
  text: z.string(),
  observed_at: z.string(),
  source_event_at: z.string().nullable().optional(),
  verification: z.string(),
  completeness: z.string(),
  blob_sha256: z.string().optional(),
  locator: z
    .object({
      line_start: z.number().nullable().optional(),
      line_end: z.number().nullable().optional(),
      pointer: z.string().nullable().optional(),
    })
    .optional(),
});
const manifestSchema = z.object({
  run_id: z.string(),
  repository: z.object({ slug: z.string() }),
  activity_window: z.object({ start: z.string(), end: z.string() }),
});
export type ImportFile = { name: string; text: string };

export function parseSnapshot(files: ImportFile[]): Dataset {
  function find(pattern: RegExp) {
    const matches = files.filter((f) => pattern.test(f.name.split('/').at(-1)!));
    if (matches.length > 1) throw new Error('检测到多个研究快照，请一次只导入一个 run 目录。');
    return matches[0];
  }
  const researchFile = find(/^(?:research(?:\.sample)?|baseline-research)\.json$/);
  if (!researchFile)
    throw new Error(
      '缺少 research.json。请选择包含 analysis/research.json 的 run 目录，或选择研究文件。',
    );
  let research: z.infer<typeof researchSchema>;
  try {
    research = researchSchema.parse(JSON.parse(researchFile.text));
  } catch (error) {
    if (error instanceof z.ZodError)
      throw new Error(
        `研究数据格式不兼容：${error.issues[0].path.join('.')}。支持 schema 1.2 / 1.3。`,
      );
    throw new Error('research.json 不是有效的 JSON 文件。');
  }
  const manifestFile = find(/^manifest(?:\.sample)?\.json$/);
  let manifest: z.infer<typeof manifestSchema> | undefined;
  if (manifestFile) {
    try {
      manifest = manifestSchema.parse(JSON.parse(manifestFile.text));
    } catch {
      throw new Error('manifest.json 格式不正确，无法读取仓库与研究窗口。');
    }
    if (manifest.run_id !== research.run_id)
      throw new Error('manifest 与 research 的 run_id 不一致，请勿混合不同研究快照。');
  }
  const evidenceFile = find(/^evidence(?:\.sample)?\.(jsonl|json)$/);
  let evidence: z.infer<typeof evidenceSchema>[] = [];
  if (evidenceFile) {
    try {
      const raw = evidenceFile.name.endsWith('.jsonl')
        ? evidenceFile.text
            .split(/\r?\n/)
            .filter((l) => l.trim())
            .map((l) => JSON.parse(l))
        : JSON.parse(evidenceFile.text);
      evidence = z.array(evidenceSchema).parse(Array.isArray(raw) ? raw : [raw]);
    } catch {
      throw new Error('证据文件格式不正确，请使用 normalized/evidence.jsonl。');
    }
    if (new Set(evidence.map((e) => e.id)).size !== evidence.length)
      throw new Error('证据文件存在重复 ID，请先使用 CLI audit 检查快照。');
  }
  const report = find(/^report\.md$/)?.text;
  const warnings = ['浏览器仅检查展示所需的数据结构；未执行 CLI 完整性、引用或状态校验。'];
  if (!manifest) warnings.push('未导入 manifest.json，仓库名与研究窗口未确认。');
  if (!evidenceFile) warnings.push('未导入证据账本，引用原文暂不可用。');
  const evidenceIds = new Set(evidence.map((e) => e.id));
  const missing = new Set(
    [...research.claims, ...research.capabilities]
      .flatMap((c) => c.evidence_ids)
      .filter((id) => !evidenceIds.has(id)),
  );
  if (missing.size)
    warnings.push(
      `${missing.size} 条被引用证据未载入；请补充同一 run 的 normalized/evidence.jsonl。`,
    );
  return {
    source: 'import',
    repo: manifest?.repository.slug ?? '本地研究快照',
    runId: research.run_id,
    createdAt: research.created_at,
    start: manifest?.activity_window.start ?? '',
    end: manifest?.activity_window.end ?? '',
    topics: research.topics,
    claims: research.claims,
    capabilities: research.capabilities,
    evidence,
    reading: research.reading_list.sort((a, b) => a.order - b.order),
    limitations: research.limitations,
    report,
    warnings,
  };
}

export async function readSnapshot(files: FileList | File[]) {
  const selected = Array.from(files).filter((f) =>
    /^(research(?:\.sample)?\.json|baseline-research\.json|manifest(?:\.sample)?\.json|evidence(?:\.sample)?\.(jsonl|json)|report\.md)$/.test(
      f.name,
    ),
  );
  if (
    selected.some((f) => f.size > 12 * 1024 * 1024) ||
    selected.reduce((n, f) => n + f.size, 0) > 32 * 1024 * 1024
  )
    throw new Error('本次导入超过浏览器限制（单文件 12 MB，总计 32 MB）。请选择较小的研究快照。');
  return parseSnapshot(
    await Promise.all(
      selected.map(async (f) => ({ name: f.webkitRelativePath || f.name, text: await f.text() })),
    ),
  );
}

export function buildCommand(repo: string, mode: string, days: number, focus: string) {
  const slug = repo
    .trim()
    .replace(/^https:\/\/github\.com\//, '')
    .replace(/\/$/, '');
  if (!/^[\w.-]+\/[\w.-]+$/.test(slug))
    throw new Error('请输入 owner/repo 或完整 GitHub 仓库地址。');
  if (
    !['quick', 'standard', 'deep'].includes(mode) ||
    !Number.isInteger(days) ||
    days < 1 ||
    days > 365
  )
    throw new Error('研究窗口应为 1–365 天。');
  const quote = (s: string) => `'${s.replaceAll("'", "'\\''")}'`;
  const focuses = focus
    .split(/[,，]/)
    .map((f) => f.trim())
    .filter(Boolean);
  const cli = 'python skills/repo-evolution-research/scripts/research.py';
  return `${cli} doctor --repo ${quote(slug)}\n\n${cli} init \\\n  --repo ${quote(slug)} --mode ${mode} --days ${days} \\\n  --timezone Asia/Shanghai${focuses.map((f) => ` \\\n  --focus ${quote(f)}`).join('')}\n\n# 把 init 输出的 result.run 填入下方后，再继续执行\nRUN='<实际 run 目录>'\n${cli} collect --run "$RUN"\n${cli} prepare --run "$RUN"\n\n# 由 Agent 阅读 analysis/packs/，并编写 analysis/research.json\n# 完成研究写作后，执行以下命令\n${cli} validate --run "$RUN"\n${cli} render --run "$RUN"`;
}
