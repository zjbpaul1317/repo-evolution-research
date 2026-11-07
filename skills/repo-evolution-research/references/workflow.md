# 工作流与 CLI 契约

本文件说明命令契约、阶段输入输出、运行状态机、selection 协议、产物所有权和恢复规则。采集语义见 [`sources.md`](sources.md)，证据与状态规则见 [`evidence-and-state.md`](evidence-and-state.md)，报告与门禁见 [`report-and-evaluation.md`](report-and-evaluation.md)。

## 调用与输出

从任意目录调用 `scripts/research.py`；它会把相邻 `scripts/` 加入模块查找路径。

- JSON 是默认输出：结构化结果写 stdout，进度和人类可读诊断写 stderr。
- `--format text` 把人类可读结果写 stdout；错误仍写 stderr。
- `--format` 可放在子命令之前或之后。
- JSON 错误统一为 `{"ok": false, "command": ..., "error": {...}}`；`error` 含 `code`、`message`、`source`、`retryable`、`recovery`、`details`。
- 退出码映射只在 `scripts/rer/errors.py`。`retryable=true` 的内部错误回落为 4，不映射到 6。

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令按声明完成；仍需检查 manifest coverage 是否有缺口 |
| 2 | 用法或 schema 错误；未启动依赖步骤 |
| 3 | 缺少必需工具、认证或环境能力 |
| 4 | 可恢复中断、锁竞争、预算耗尽或部分结果 |
| 5 | 证据/状态校验失败；不得作为正式报告发布 |
| 6 | 内部不变量或存储完整性错误；保留快照用于诊断 |

## 阶段 I/O

| 阶段 | 命令 | 输入 | 产物 | 失败语义 |
| --- | --- | --- | --- | --- |
| doctor | `doctor [--repo]` | 环境、可选仓库 | stdout checks | 关键项失败退出 3 |
| audit | `audit --run` | 已保存快照（可只读） | stdout 分类诊断与恢复步骤，不写入文件 | 仅旧派生索引需重建为 4；其他完整性问题为 6 |
| init | `init --repo ...` | 参数 | run 目录、`manifest.json`、`config.resolved.json` | 参数错误退出 2 |
| collect | `collect --run` | manifest/config | raw 文档、blobs、activity 视图、selection 决策、coverage、checkpoint | 阻塞缺口或 run-level 预算耗尽退出 4 |
| prepare | `prepare --run` | collect 产物 | `normalized/evidence.jsonl`、`relations.jsonl`、`analysis/candidates.json`、`pack-index.json`、`packs/*.md`、`prepare-report.json` | collect 未完成退出 2；零证据退出 4；派生失败保持 failed |
| import-source | `import-source --run --input` | 外部来源 envelope、已 prepared 实体 | 导入 evidence、Agent 确认的关系 | schema/实体/证据错误退出 2；使 prepare/validate/render 失效，需重跑 prepare |
| selection | Agent 写 `analysis/selection.json` | packs、candidates | Agent 决策 | schema 错误由下一步拒绝 |
| 增量深读 | `collect --run --selection <path>` | selection | 新深读文档、`selection-report.json` | 预算耗尽退出 4，报告可见 |
| 研究 | Agent 写 `analysis/research.json` | packs、evidence、文档 | topics/claims/capabilities/unknowns/reading_list/limitations | schema 错误由 validate 拒绝 |
| validate | `validate --run` | research + ledger | `validation.json`、`evidence-review.md` | 门禁失败退出 5 |
| render | `render --run` | 已校验 research、evidence 依赖 blob | `report.md`、`render-report.json` | 失败退出 5，写 `incomplete.md` |

`--language` 和 `--compare-previous` 是保留参数：V0.1 报告固定中文，且尚未实现与上一窗口比较；传入不会改变输出，`init` 会在 JSON `warnings` 和 `manifest.notes` 中写一条可见提示。不要把它们描述成当前能力。

`import-source` 必须在首次 `prepare` 之后运行，且导入关系的两端实体必须已存在；后续 envelope 可以连续导入，全部导入后重跑一次 `prepare`。envelope 结构见 `assets/schemas/source_envelope.schema.json`。envelope 至少包含 `source_url`、`retrieved_at`、`provider`、`content`、`entity_id`；`relations[]` 的条目包含 `from_id`、`to_id`、`type`、`basis`、`verification`，可选 `id`、`evidence_ids`、`scope`。未提供 `evidence_ids` 时自动引用本次导入的 evidence；`verification=verified` 的关系必须至少引用一条 verified evidence（`contains_commit` 允许空 evidence），且 `basis` 只能是 `api_explicit` / `text_explicit`；`verified_in_release` 还必须引用本次导入的 evidence。正文按 `blobs/<sha256>` 去重；来源 artifact 位于 `imported/<source_key>/<content_sha256>.json`，`source_key` 由 `source_kind` / `entity_id` / `source_url` / `source_revision` 决定，重复检索写入 artifact 的 `observations`。导入按 `(from_id, to_id, type)` 合并：`evidence_ids` 取并集，`verification` 按 `disputed > verified > candidate`，来源观察写入 `scope.observations`，被合并的显式 ID 写入 `scope.aliases`。`relation.scope` 只能追加额外键，不能覆盖 `artifact` / `observed_at` / `provider` / `source_url` / `generator`，也不能提供内部字段 `observations` / `aliases` / `resolution`。导入不会自动把 release-note 提及升级为 `verified_in_release`，人工确认语义由调用方显式写入。

`prepare` 前置条件是 `stages.collect ∈ {complete, partial}`；成功后写 `stages.prepare=complete`、`state=prepared`。`validate` 与 `render` 不访问网络。

若 `evidence_total=0`，派生过程仍完成，以便后续 selection / import-source 补充材料，
但 CLI 返回退出码 4、`partial=true`、`has_evidence=false` 和
`no_research_evidence` warning。检查 coverage 后补读来源；预算已耗尽时新建较小窗口的 run，
或导入外部来源后重跑 prepare。`has_evidence=true` 仅表示证据流非空，不保证研究质量。
空证据快照的 validate 会以 `no_research_evidence` 错误拒绝正式报告。

## 运行状态机

`manifest.state`：`initialized` → `collecting` → `prepared` → `researching` → `validated` → `rendered`；异常时为 `partial` / `interrupted` / `failed`。

`manifest.stages` 的每个阶段只取 `not_started` / `in_progress` / `complete` / `partial` / `failed` / `skipped`。

- `collect` 为 `complete` 或 `partial` 才允许 `prepare`。
- `prepare` 比较 evidence/relations/candidates/pack-index/pack 文件快照。任一变化会把 `research` / `validate` / `render` 重置为 `not_started`，并丢弃旧报告。
- `validate` 成功时 `state=validated`；若已有 `render=complete` 且本次 research hash 未变，则保持 `state=rendered`（报告仍对应当前 research）。失败时 `stages.validate=failed`、`stages.render=not_started`。
- `render` 成功时 `state=rendered`，删除旧 `incomplete.md`；失败时 `stages.render=failed`，删除旧 `report.md` / `render-report.json`。
- 再次 `collect` 改变输入时，下游阶段失效；`--force` 只控制 collect 重跑，不重置预算。

## Selection 协议

Agent 读取 `analysis/packs/*.md` 后写 `analysis/selection.json`。该文件归 Agent 所有，`prepare` 不覆盖。

```json
{
  "schema_version": "1.3",
  "run_id": "<manifest run_id>",
  "created_at": "<ISO 8601>",
  "entities": [
    {
      "entity_id": "github.com/owner/repo/pull/123",
      "reason": "为什么值得深读",
      "depth": "quick",
      "read": {"body": true, "comments": true, "reviews": true, "files": true, "diff": false},
      "questions": ["要回答的问题"],
      "theme_hint": "scheduling"
    }
  ],
  "notes": []
}
```

- `depth` 为 `quick` / `deep`；`read` 的五个布尔值控制可独立请求的 section。Issue/Discussion 的 body 随 detail 查询返回，没有独立请求，因此 `read.body` 对这两类不裁剪，权威文档始终保存 body；`comments` 控制 Issue 的 comments/timeline 与 Discussion 的 comments/replies。`reviews`/`files`/`diff` 只对 PR 有意义，`diff` 是保留项，当前版本不抓取完整 diff。
- 只接受已存在实体；重复实体、未知 `run_id`、缺少字段会被拒绝。
- 工作流上限为两轮 selection。CLI 不强制轮数，但不要为填表无限补抓。
- `collect --selection` 只读取已选且尚未读全的对象；PR/Issue/Discussion 可按 section 补读并与已有文档合并；Release/Doc 只记录为阅读选择，不重新抓取。
- 已读对象若 section 覆盖完整记 `skipped_unchanged`；后续 selection 可只补缺失 section。
- 深读预算不足时停止网络读取，把剩余对象标为 `not_attempted`，写 `analysis/selection-report.json`，CLI 返回部分结果退出码；不得静默丢弃选择对象。
- 选择理由以 `selection.json` 为准；`pr-selection.json` / `proposal-selection.json` 是 collector 的决策记录，不承载 Agent 理由。

## 候选排序与阅读包

`prepare` 在 evidence/relations 之后离线生成三组派生产物，不访问网络：

- `analysis/candidates.json`：`rule_version=candidates-v1`。候选来自 PR/提案 selection、`raw/prs.jsonl` / `issues.jsonl` / `discussions.jsonl`、release/doc/design 实体、activity `candidate_ids` 和图上的 1 跳邻居。`candidate_id=cand-<sha256(rule_version|entity_id)[:16]>`，重复 prepare 必须稳定。每个候选显式保存 `bucket`、`module`/`module_source`、`deep_read`、`score`、固定九个 `score_components`、`selection_reason`、`missing_features`、`raw_refs`、`evidence_ids`。分数只是排序启发式，不写概率或“可信度”。
- 深读配额使用 `max_deep_entities - budget_consumed.deep_entities`；standard 的 design/merged_pr/open_proposal/release_doc/exploration 比例约为 8/14/8/6/4。探索位至少约 10%，focus 命中不超过总配额一半。配额不足、模块无法识别、候选被预算截断、关系扩张被截断，都必须出现在 `counts` / `limitations`。
- `agent_pr_reserve` 在初始 collect 时从 `max_deep_prs` 中预留（standard 4）；Agent selection 仍可使用完整 `max_deep_prs`。推荐阶段同时检查总深读余额和 PR 子预算，`budget_fit=blocked_pr_budget` 的候选不得推荐。`budget_fit=fetchable` 只表示“当前仍有可用预算”，不等于“会被推荐”；是否推荐看 `recommended` / `selection_status`（`recommended`、`skipped_sub_budget`、`skipped_quota`、`already_read`）。
- 显式 selection 与 `deep_read` 实体在候选截断前受保护；超出硬上限时写入 `agent_selected_omitted` / `deep_read_omitted`，不得静默丢弃。
- `analysis/pack-index.json` + `analysis/packs/part-NNN.md`：按 `source_kind + module` 分组。module 优先取 PR changed files，其次 doc path / labels / scope，无法判断写 `unknown`。每个 pack 有稳定 `pack_id`、候选 ID、字符数和 truncated/omitted 信息。
- 第二次 prepare（存在 `analysis/selection.json`）把 pack 分成 `Research materials` 与 `Candidate index`：selected/deep-read 在前，附正文/关键评论 excerpt、evidence ID、locator 和 source URL；普通候选只保留短 preview。预算不足时先删普通候选，再删 deep-read，selected 最后才删；任何删除写入 `omitted_candidates`。第一次 prepare 仍优先展示待探索材料。
- 总字符受 `max_pack_chars` 限制；不是简单增大上限，而是优先保证研究材料有足够上下文。写入新 index 时删除不再被引用的旧 pack 文件。`preview_slots` 统计槽位，`previews_total` 只统计非空 preview，`previews_truncated` / `items_with_truncated_preview` 按最终渲染 cap 统计；任何被截断的 excerpt 必须带 `(truncated)`，且 pack 的 `truncated` 必须与 excerpt/omitted 状态一致。
- `prepare` 比较 evidence/relations/candidates/pack-index/pack 文件快照；任一变化都会把 `research` / `validate` / `render` 重置为 `not_started`。仅 selection 变化也会通过 `agent_selected` 反映到 candidates，触发同样的 stale 规则。

## 产物所有权

| 产物 | 所有者 | 性质 | 恢复方式 |
| --- | --- | --- | --- |
| `blobs/<sha256>` | store | 权威、内容寻址 | 不可重建 |
| `raw/pr-details/<n>.json` | PR collector | 权威，一个 PR 一次事务 | 网络重读 |
| `raw/releases/<release_id>.json` | release collector | 权威，一个 Release 一次事务 | 网络重读 |
| `raw/issues/<n>.json` / `raw/discussions/<n>.json` | proposals collector | 权威，一对象一次事务 | 网络重读 |
| `raw/docs/<revision>/<path>.json` | docs collector | 权威，固定修订快照 | 固定 SHA 可复用 |
| `raw/prs.jsonl` / `issues.jsonl` / `discussions.jsonl` | activity | 权威持续流 | 网络重读 |
| `analysis/activity-overview.json` | activity | 查询审计视图 | 可从视图状态重算 |
| `analysis/pr-selection.json` / `proposal-selection.json` | selection / proposals | 决策记录 | 可从候选与结果重算 |
| `checkpoints/collect.json` | progress | 运行状态提示 | 不可重建，但不是真相 |
| `normalized/entities.jsonl` | entity_store | 物化视图 | 从 raw 重建 |
| `normalized/evidence.jsonl` / `relations.jsonl` | prepare + import-source | 混合流：prepare 只替换 `generator=prepare` 的派生行，导入行保留 | 派生行从 raw 重建；导入行需重新导入 |
| `analysis/candidates.json` / `pack-index.json` / `packs/*.md` | ranking/packs | 派生索引 | 重跑 prepare |
| `analysis/selection.json` / `research.json` | Agent | Agent 决策 | 不重建、不覆盖 |
| `analysis/selection-report.json` | selection_protocol | 增量读取审计 | 从 selection 与读取结果重算 |
| `analysis/validation.json` | validation | 门禁报告 | 从 research/ledger/coverage 重算 |
| `report.md` / `analysis/render-report.json` | render | 正式报告与渲染审计 | 重渲染 |
| `incomplete.md` | render/validate | 拒绝出正式报告的原因 | 下次成功 render 时删除 |

已删除的重复表示（`raw/pr_details.jsonl`、5 条 `raw/pr_*.jsonl` 子流、`analysis/pr-details/<n>.json`）由 `verify_integrity` 识别为 `legacy_snapshot`，不要重新引入。

## 提交与恢复

1. 读取并解析所有 section。
2. 保存全部长文本/blob（内容寻址，可重复写入）。
3. 构造权威文档并校验不变量。
4. 原子写权威文档：这是提交点。
5. 更新 entity 投影、selection、coverage。
6. best-effort 写 checkpoint；checkpoint 失败不能覆盖原始错误。

恢复规则：

- 文档不存在：重新读取。
- 代码修订键（PR 为 `head_sha + base_sha`）不变、section 完整、全部 `text_ref` 的 blob/hash/bytes 校验通过：只复用 `files` / `commits`。
- 可变材料（正文、评论、review、合并状态、Release note、Issue/Discussion 内容）在 collector 重建文档时始终重读；selection 的 `already_read` 分支复用已完整快照，不得据此推断可变材料未变。
- 重建文档与旧文档比较时忽略 `observed_at` 和 `selection_reason`；内容未变记 `skipped_unchanged`，不刷新快照时间。
- 旧文档 `coverage.detail == complete`、本次重建不是 `complete` 时不得覆盖：保留旧文档，候选记 `failed` / `refresh_incomplete`，coverage 记 `partial`，下次重试；`--force` 同样适用。
- checkpoint 与文档冲突：以文档为准；checkpoint 只作提示。
- 派生视图缺失：`reconcile_*` 或 `verify_integrity(repair=True)` 从权威文档重建；丢失的权威 blob 不能由 repair 恢复。

## 快照兼容

当前 `SCHEMA_VERSION=1.3`、`TOOL_VERSION=0.2.8`。每个快照在 manifest 记录 `schema_version`。新运行写 1.3；`init`/`collect` 只接受当前版本，旧 run 不能续跑 collect。校验器和 `prepare` 仅对 config/manifest/research/validation 的 1.2 结构做兼容读取；`pack-index` / `candidates` 的 schema 只接受 1.3，任何 1.2 版本标记都会被拒绝（即使字段齐全），必须先由当前版本 `prepare` 重建。在首次真实端到端验收前，快照是开发产物，不保证完全向后兼容。

### 只读升级诊断与副本恢复

先执行 `research.py audit --run <OLD_RUN>`。audit 不请求网络、不修复 JSONL、
不修改 manifest/报告，也不会为归档快照创建锁文件。存在运行中写入锁时返回 4，稍后重试。
归档目录无锁文件时直接读取；请对稳定快照执行，不与外部文件编辑并行。

- `integrity` 保留全部严格检查结果；只有全部通过才 `ok=true`。它检查存储完整性，
  不承诺证据充分、研究已完成或语义正确，证据就绪与报告门禁仍由 prepare/validate 负责。
- `rebuild_required` 仅识别版本为 1.2 的 candidates/pack-index schema 问题，
  表示已知派生索引需重建，退出 4；不会把它们当作通过完整性检查。
- `attention_required` 保留其他所有问题；例如缺失 blob、损坏 JSONL、缺失 selection、
  当前版本索引错误或过期报告。存在任一项即退出 6；和旧索引问题同时出现时也不会被降级。
- `collection_schema_compatible` 仅说明 manifest 版本是否支持 collect，
  不代表预算、覆盖、阶段和完整性等其他前置条件已满足。

恢复旧索引时先复制 run 到新 output root，保留 `host/owner/repo/run_id` 的结构，
在副本执行 `prepare`。不要仅把 JSON 的 schema_version 改成 1.3，也不要手工升级 manifest
来绕过 collect 限制。prepare 会使旧 validation/report 失效，并可能删除副本中的旧报告；
原始材料仍留在原 run。之后检查 research 的证据定位与语义，再执行 validate、render、audit。
旧报告不符合新门禁时按错误修正 research，不能把重建成功解释为报告已验收。

候选状态：已关闭且未合并的 PR 使用 `closed_unmerged`，不占 `open_proposal`
通道配额；初轮 PR 深读使用 `closed` bucket，仅在开放/合并配额有余位时补选。
关闭方案仍保留在候选和关联阅读中，可用于研究未采纳方案与技术取舍。

初轮 PR 深读在各状态配额内预留最多约一半的不同标题区域探索位；
显式 `[area]` 标题或 parser/formatter/WASM 等常见区域词仅作为 `module_source=title` 的启发式分组，不冒充源码路径。
已知文件/标签分组优先。阅读包预算至少 8,000 字符时，为最多四个不同模块的
发现候选保留位置；Agent 明确选择的材料仍有更高优先级，硬预算不足时会披露省略。

阅读包摘录过滤标题空壳、PR 模板与已识别的机器人/政策提示，优先提供正文概述、
方案修订、限制与维护者评论；时间和关键词只影响阅读顺序，不提高 evidence 的可信等级。
摘录始终是原文连续子串，保留 evidence ID 与 locator；完整历史仍在 evidence/raw 中。
发现摘要目标长度为 240 字符，预算紧张时降到 160 字符并标记截断。
