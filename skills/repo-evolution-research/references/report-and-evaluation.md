# 报告、门禁与评测

本文件说明 `research.json` 的结构、报告生成、`validate`/`render` 门禁，以及评测层应检查的质量维度。证据与状态规则见 [`evidence-and-state.md`](evidence-and-state.md)。

## research.json 结构

顶层必填：`schema_version`、`run_id`、`created_at`、`topics`、`claims`、`capabilities`、`unknowns`、`reading_list`、`limitations`。`run_id` 必须与 manifest 一致；`limitations` 不得为空。

| 对象 | 必填要点 |
| --- | --- |
| `topic` | `id`、`title`、`research_question`、`summary`、`claim_ids`、`capability_ids`、`reading_item_ids`、`gaps` |
| `claim` | `id`、`text`、`kind`、`evidence_ids`、`contradicting_evidence_ids`、`confidence`、`confidence_reason`、`scope`、`reason`、`missing_evidence`、`state_time_basis` |
| `capability` | `id`、`topic_id`、`title`、`scope`、四个状态维度、`target_branch`、`observed_revision`、`verified_versions`、`requirements`、`evidence_ids` |
| `unknown` | `id`、`topic_id`、`claim_id` |
| `reading_item` | `id`、`priority`（P0/P1/P2）、`kind`、`title`、`url`、`entity_id`、`why`、`order` |

ID 在各自命名空间内唯一。topic 必须引用 claim/capability/reading；capability/unknown 必须回到 topic；unknown 必须指向 `kind=unknown` 的 claim；reading 的 `entity_id` 必须存在。`reading.url` 必须是 HTTP(S)，非 external 项的 host 必须等于仓库 host。

## 报告结构

模板 `assets/report-template.md`（版本 2）使用精简阅读结构：

1. 研究范围与结论速览
2. 按主题组织的重点技术演进：每个主题连续展示问题、设计、实现、取舍与后续判断
3. 功能进展表
4. 未解决问题
5. 推荐阅读（合并材料索引，保留来源、阅读顺序和原因）
6. 方法与局限（所有校验警告保持可见）
7. 可展开的证据附录（完整脚注；原始证据还可在 `analysis/evidence-review.md` 逐项核对）

摘要不再复列全部脚注；正文结论保留直接引用。主题摘要只在速览中出现一次，
不再分散到近期方向、技术演进、后续方向重复展开。共享 claim 的正文只写一次，
其他主题使用引用。折叠附录减少默认阅读长度，不删除引文、定位、反证和置信度。
模板升级后旧 report 会标 stale，需重新 validate/render。

报告只由结构化 topic/claim/capability/reading 生成，不读取自由文本草稿。报告头记录 `run_id`、research hash、inputs hash 和模板版本。claim 用脚注 `[^C001]` 标注，脚注定义包含 claim ID、kind、置信度/理由、evidence 来源类型、entity、locator（行号/字符区间）、claim 级简短引文、引文偏移、verification 和反证。长 evidence block 必须提供 `evidence_quotes` 精确引文；没有 claim 级引文时不能把块首当作支撑句。标题/空壳证据会在脚注中显式标记。功能进展表使用 `validation.lifecycle` 的推导结果，标注发布核验与适用版本，而不是手抄状态。

“校验警告”按 scope 元数据、lifecycle、coverage、evidence 分类；每条显示对象 ID、次数和可修复位置。`diff_not_requested` 作为 V0.1 统一限制合并说明。

## validate 门禁

`validate` 只读 `analysis/research.json`、normalized 流、raw 文档和 manifest，不访问网络；成功后写 `analysis/validation.json`，并生成 `analysis/evidence-review.md`（claim/capability → evidence 原文 → locator 的人工核对视图）。实现分为六组 `checks`（schema 单独成组）：

| 组 | 阻断项 | 只告警 |
| --- | --- | --- |
| `research_schema` | research schema、`run_id` 不一致、topics 为空 | — |
| `evidence_integrity` | evidence blob/locator/quote、relation 端点、PR/Release/Doc 投影、candidates、pack-index、selection 的问题（`integrity:` 前缀） | lifecycle 读取的文档 schema 非法 |
| `reference_targets` | ID 重复、topic/capability/unknown/reading 引用悬空、reading URL/host 非法 | 未被引用的 claim/capability、重复 reading order |
| `claim_evidence` | observed/inferred 缺 evidence、claim/capability 的 evidence 不存在、scope entity 不存在、反证不存在、observed/inferred/capability 的全部 evidence 为标题/空壳；长 evidence 缺 `evidence_quotes`；quote 不属于 `evidence_ids`、不是原文精确片段、少于 24 字符、在 evidence 中不唯一或自身为标题/空壳 | 引用未 verified 的 evidence；unknown 的标题/空壳证据；doc/source_file 缺 revision；release 缺 version/versions；scope 版本不一致 |
| `lifecycle` | merged/released/availability/reverted 等状态冲突、release 早于实现 PR 合并、commit 目标时间线无法解析、claim 的 capability 不存在、unknown 缺 reason/missing_evidence、claim 版本未验证 | `gaps` 与 declared/derived 差异 |
| `coverage_limitations` | limitations 为空；阻塞 channel 的降级未在 limitations 说明；collect 为 partial 或预算耗尽但未说明 | 非阻塞降级 `coverage_degraded`；`not_requested` |

`validation.lifecycle` 保存每个 capability 的 `declared` / `derived` / `delivery_verification` / `state_time_basis` / `observed_revision` / `verified_versions` / `supporting_entities` / `gaps` / `conflicts`；报告的功能进展表使用这些推导结果，不手抄状态。

`evidence_integrity` 复用 `verify_integrity(include_research=False)`，逐项检查：blob 存在；blob hash 与 `sha256` 一致；实际字节数与 `bytes` 一致；可按 UTF-8 解码；非空文本 `preview` 非空且等于 `text[:len(preview)]`；`preview_truncated == (len(text) > len(preview))`；`sha256=null` 且 `bytes=0` 的空文本允许。它同时校验 relation identity 唯一性、verified relation 的 evidence 信任（`contains_commit` 的空 evidence 例外）、imported source artifact 与 evidence 的溯源字段一致性、import-source relation 的 `scope.artifact` / `scope.observations` 及 observation evidence 归属、PR/Release/Doc 文档投影、Issue/Discussion 文档与 `raw_state.proposal_read`、candidates、pack-index、selection 计数和 per-carrier coverage。`repair=True` 只重建派生投影，不恢复丢失或损坏的权威原文；这类缺口必须重新 collect 或保留快照并记录。

失败处理：退出 5，写结构化诊断到 `validation.json`，删除旧 `report.md` / `render-report.json`，写 `incomplete.md`，`stages.validate=failed`、`stages.render=not_started`、`state=researching`。缺失 `research.json` 是 usage error（退出 2），走同样的报告清理。`validation.json` 不写运行时刻，重复 validate 字节一致。

## render 与 stale

`render` 只使用已通过校验的 research：

- 必须 `stages.validate=complete`；`failed` 退出 5；`not_started` 或缺失/损坏的 validation 是 usage/schema 错误。
- 重新计算 `research_sha256`（research.json 字节）和 `ledger_sha256`（normalized 三流、`raw/prs.jsonl` / `issues.jsonl` / `discussions.jsonl`、`pr-selection.json` / `proposal-selection.json`、candidates、pack-index、raw 深读文档目录、imported 来源 artifact 目录、manifest.coverage 与 default_branch_sha）。Agent 的 `analysis/selection.json` 不在 digest 中；它的影响经 `candidates.agent_selected` / `agent_depth` / `agent_read` 传导。任一 hash 与 validation 记录不一致、或 `validation.ok=false`，拒绝渲染（退出 5）。
- 在 stale gate 之后、生成报告之前，按 claims / capabilities / relations 的 evidence 闭包重新校验 blob 存在与 hash；缺失记 `missing_evidence_blob`、损坏记 `corrupt_evidence_blob`，写 `incomplete.md` 并退出 5，不调用报告构建。
- 成功写 `report.md` 和 `analysis/render-report.json`（含 validation hash、report hash、字符数、section 列表；不写运行时刻），`stages.render=complete`、`state=rendered`，删除旧 `incomplete.md`。重复 render 字节一致。
- 失败写 `incomplete.md`，删除 `report.md` / `render-report.json`，`stages.render=failed`；绝不留下一份外观完整但过期的正式报告。
- `verify_integrity` 会报告 `stale_validation` / `stale_validation_inputs`（research 或派生输入变化）、`stale_report`（report 被手改、模板升级、validation 重新生成）、有 report 但缺 validation/render-report 等不一致。
- `incomplete.md` 是拒绝原因，不是报告。修复 research 或环境后重新 validate/render；不要手改 `report.md` 绕过门禁。

## 交付约定

- `report.md` 是唯一正式报告；`incomplete.md` 只解释失败。
- limitations 必须覆盖：实际窗口、采集缺口、未读材料、状态核对截止时间、以及任何 validation warning 的影响。
- 报告中的 review/issue 速度类观察只能说“观察到”，不能说“项目一定在加速/放缓”。
- 合并状态、发布核验、可用性声明必须能追溯到 evidence 或 `validation.lifecycle`。

## 质量指标与评测挂点

评测层（`evals/`，后续实现）应自动汇总以下结构指标，语义深度保留人工评分：

| 维度 | 自动检查 |
| --- | --- |
| 结构覆盖 | 每个 topic 有 claim/capability/reading；每个 capability 有四个状态维度和 evidence；unknown 有 reason/missing_evidence |
| 事实可核查 | 每个 observed/inferred claim 有 evidence；引文 quote 与 blob 一致；无悬空 ID |
| 状态正确性 | `validation.lifecycle` 与声明一致；merged ≠ released；released 有 `verified_in_release`；revert/remaining 冲突为零 |
| 关系覆盖 | 关键 PR→commit、PR→Issue、Release→note、revert 关系存在；无把 mentions 当 implements |
| 预算与局限 | collect 的 partial/阻塞通道进入 limitations；未读对象和抽样范围可见 |
| 报告确定性 | 重复 validate/render 字节一致；无手改报告；模板版本匹配 |

人工评分关注：研究问题是否具体、动机与取舍是否解释、是否给出替代方案和反例、实现细节是否到模块/行为层、未知项是否诚实、下一步阅读是否有用。

自动评测的反例集至少覆盖：悬空 evidence、伪引文（quote 与 blob 不符）、缺 limitations、stale validation、窗口外事件当历史、merge 晚于 release、release note 当发布证明、`mentions` 当实现关系。离线闭环使用 fake transport 重放 `init → collect → prepare → selection → collect --selection → prepare → research → validate → render`，不访问网络。
