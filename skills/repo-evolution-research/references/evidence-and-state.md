# 证据、关系与状态

本文件说明 ID、evidence/relation/claim 契约、状态真值表和生命周期提升规则。采集来源见 [`sources.md`](sources.md)，报告门禁见 [`report-and-evaluation.md`](report-and-evaluation.md)。

## 实体 ID

| Kind | 形式 |
| --- | --- |
| repository | `host/owner/repo` |
| pull_request / issue / discussion | `host/owner/repo/{pull\|issue\|discussion}/<number>` |
| release | `host/owner/repo/release/<tag>` |
| commit | `host/owner/repo/<40-char-sha>` |
| doc / source_file | `host/owner/repo/<40-char-sha>/<path>` |
| comment / review | `host/owner/repo/{comment\|review}/<numeric-id>` |

ID 不得含 `.` 或 `..` 路径段。实体词表冻结；`proposal` 不是 entity kind，RFC/design 是角色。

## Evidence

覆盖的文本源：PR body、issue comments、reviews、review comments、commit message、Issue body/comments、Discussion body/comments/replies、Release body、Doc content。review comment 单独记 `pull_request_review_comment`，不与 PR conversation comment 混用。

- 文本按空行分段；超长段落按行切到 `MAX_BLOCK_CHARS=4000`，单行仍超限时按字符切分。
- locator 使用 `char_range`，对 LF 化文本精确；`text` 必须等于对应 blob 的切片。空文本不生成 evidence。
- evidence ID：`ev-<sha256(source_kind|entity_id|source_url|artifact|blob_sha256|char_start|char_end)[:16]>`。重复 ID fail closed，不能静默去重。
- prepare 对已保存的文本完整建立 evidence 索引，不按文档前 200 块截断；后期 review、修订与反例使用同样的精确 locator。网络响应/总字节与阅读包预算独立限制成本。底层显式采样调用仍会报告 `evidence_truncated`。
- 声明了 `sha256` 但 blob 缺失/非 UTF-8 时 prepare 失败，错误 details 必须包含 `artifact` 和 `json_pointer`。
- prepare 只替换自己产生的 source_kind，保留 `external_page` / `api_response` 等导入证据。`prepare-report` 同时记录 `evidence_derived` 与 `evidence_total`。

`verification` 必填：

| 值 | 含义 |
| --- | --- |
| `verified` | quote 与保存的 blob + locator 一致；这只证明引文可复现，不证明结论为真 |
| `unverifiable` | 无法用保存材料复核；`url_fragment` 一律记此值，不能当作 verified |
| `failed` | 尝试复核但失败 |

## Relation

prepare 识别完整 GitHub URL（`/pull/<n>`、`/issues/<n>`、`/discussions/<n>`、`/releases/tag/<tag>`、`/blob/<sha>/<path>`）、`owner/repo#n`、同仓库 `#n`、40 位 SHA 和可唯一解析的短 SHA。

- fenced code、inline code、blockquote 中的引用只计数并丢弃，分别记 `ignored_code_refs` / `ignored_quote_refs`。fence 状态在完整 source 上计算后再切片。
- `Fixes` / `Closes` / `Resolves`（含复数、多目标）只从 PR body、PR conversation comment、commit message 识别，类型 `closing_reference`，`verification=candidate`；关键词后必须紧跟引用列表，目标仅限 Issue/PR；review comment 不参与 closing。没有 `closedByPullRequestsReferences` API，不能提升为 verified。
- 文档正文只认完整 URL 和 `owner/repo#n`，不把 Markdown 锚点当 Issue。
- Release body 的引用类型为 `release_note_mentions`，只作交付线索；`mentions` 是普通正文引用。
- 完整 URL 指向其他仓库且 kind 可确定时，完整创建 `external: true` 的最小引用实体；`max_external_entities` 只限制阅读候选的外部关联扩展，不截断离线引用图，也不自动读取外部 issue。外部节点不按本仓库同号 PR/Issue 补全元数据。`owner/repo#n` 指向其他仓库时无法区分 Issue/PR，不建实体，记 `ambiguous_external_refs`。同仓库未知编号记 `unresolved_refs`。
- 去重键为 `(from_id, to_id, type)`；重复出现合并 `evidence_ids`。自引用丢弃并记 `self_refs`。单纯 `mentions` 不得升级为 `implements_part_of`。合并后的 `verification` 按 `disputed > verified > candidate` 取值：只要存在 disputed 观察，后续 verified 导入不能把它翻回。
- prepare 生成的 relation 在 `scope.generator` 记 `prepare`；重建只替换 prepare-owned relation。派生关系与导入关系具有相同 `(from_id, to_id, type)` 时，合并到导入行并保留导入行的 ID/`scope`：`evidence_ids` 取并集，`verification` 按 `disputed > verified > candidate`，合并数写入 prepare report 的 `relations_merged`，不再报 ID 冲突。导入行按 identity 唯一化；被合并掉的显式 ID 记入 `scope.aliases`，每次观察的来源写入 `scope.observations`。同名 identity 的重复导入不得以新 ID 绕过既有 disputed。不再出现的 `discovery_reasons` 含 `pr_commit` / `external_reference` 的 prepare-owned 实体必须清理。
- 从 PR detail 的 `commits` 投影 commit 实体，并建立 `pull_request → commit` 的 `contains_commit`：`basis=api_explicit`、`verification=verified`、`evidence_ids=[]`，来源写在 `scope`。
- `Graph` 提供 `by_entity` / `outgoing` / `incoming` / `traverse(start, max_hops, budget)`；`budget` 是包含起点的最大节点数，耗尽时只保留两端都在结果中的边。

完整性检查：relation 的 evidence 必须存在；`evidence.entity_id` 必须是 relation 的端点（`from_id` 或 `to_id`）；evidence/relation ID 不得重复；同一 `(from_id, to_id, type)` 不得出现多个 ID；`verification=verified` 的关系必须引用 verified evidence（`contains_commit` 的空 evidence 例外）；imported evidence 的 artifact 必须存在并与 `source_url` / `entity_id` / `source_revision` / `content_sha256` 一致；import-source relation 的 `scope.artifact` / `scope.observations` 必须存在且 observation evidence 属于 relation evidence；自环必须报告；端点必须存在于 entity 流；写前检查失败时 stage 置 `failed`，不得置 `complete`。

## Claim 与 Unknown

`claim` 是最小事实单元，`kind` 只取：

- `observed`：材料直接陈述；必须有 `evidence_ids`。
- `inferred`：Agent 综合判断；必须有 `evidence_ids`，并写 `confidence` / `confidence_reason`。
- `unknown`：当前材料无法回答；必须有 `reason` 和至少一条 `missing_evidence`，不能伪造 evidence。

规则：

- `unknowns[]` 只是索引，`claim_id` 必须指向 `kind=unknown` 的 claim；`topic_id` 可空。
- `scope.entity_ids` 必须存在；`scope.capability_id` 必须存在；doc/source_file 缺 `scope.revision`、release 缺 `scope.version` 是 warning。
- 单版本用 `scope.version`；同一 claim 覆盖至少两个 release 时用 `scope.versions` 数组。二者互斥，`scope.versions` 至少两项。`scope.version(s)` 必须与 scope 中的 release 实体一致。
- observed/inferred claim 的全部 evidence 都是标题、分隔线、图片或空代码围栏时阻断；unknown 的同类弱证据只告警。ATX、setext、blockquote 标题都视为空壳，不能证明事实内容。超过 400 字符的 evidence block 必须用 `evidence_quotes` 提供精确支撑句；引文必须至少 24 字符、在 evidence 中唯一出现，且不能是标题/空壳。报告和评审视图优先展示该句，而不是固定取块首。
- 正文措辞要体现证据级别：release note 写成“某版本发布说明记载……”；PR 正文写成“已合并 PR 描述/实现……，尚未做运行验证”。不要把 release note 的宣布写成 artifact 级交付核验。
- 观察到的 capability claim 若声明 `scope.version` 或 `scope.versions`，每个声明版本都必须由 `verified_in_release` 支持，否则 `claim_version_not_verified` 阻断。只有发布说明提及（`release_note_mentions`）不够；如果 claim 只是记录发布说明，不要声明版本 scope。非 capability claim 的 `scope.version(s)` 只表示来源版本，不会触发交付核验，也不能当作 `released` 证明。
- 反证写入 `contradicting_evidence_ids`，不能只保留支持材料。
- 未被 topic 引用的 claim/capability 是 warning；不要为了消除 warning 强行编造主题。

## 状态真值表

Section 状态：`complete` / `partial` / `unavailable` / `not_requested` / `not_applicable`。

`derive_detail_status`：

| 输入 | detail |
| --- | --- |
| 所有请求 section `complete`，head 已确认，diff 未请求或 `complete` | `complete` |
| 任一请求 section `partial` | `partial` |
| 任一 section `unavailable`，但还有其他 `complete` | `partial` |
| 所有请求 section 都 `unavailable` | `partial`（详情元数据仍可用） |
| `head_changed` 或 `head_unverified` | `partial` |
| diff 为 `budget_capped` / `unavailable` | `partial` |
| 没有任何活动 section | `not_requested` |

`derive_pr_detail_channel_status`：

| 输入 | channel |
| --- | --- |
| 未选择任何 PR | `not_requested` |
| 所有 selected PR 失败且无文档 | `unavailable` |
| 有 selected PR 未尝试（fatal 中断） | `partial` |
| 部分 selected 失败，或任一文档 detail != `complete` | `partial` |
| 所有 selected 成功或 `skipped_unchanged` | `complete` |

- `skipped_unchanged` 有两种来源：请求的 section 已完整（`already_read`，不再请求），以及本轮确实重读可变材料且内容未变（`materials_unchanged`）。只有后者证明可变材料未变；前者复用旧快照。不能只比较 head 就跳过评论、review 或合并状态。
- 状态聚合必须走 `collectors/status.py` 的 `derive_*`；新增状态要先改 `status.py`、`tests/test_status_truth_table.py` 和本表，按契约评审处理。
- 已知例外：activity 的 view/shard 状态机仍使用本地字符串（`split` / `fallback` / `partial`），迁移前新增 activity 状态必须同步 schema 与本表。
- 哪些 channel 阻塞运行由 `progress.CHANNEL_POLICIES` 决定；详见 [`sources.md`](sources.md)。

## 时间与状态语义

`state_time_basis` 在 claim 上必填，取 `observed_current` 或 `historical_reconstructed`；manifest 上缺省 `observed_current`。窗口结束后才发生的事件不能写成窗口内的历史事实；只能标为后续观察或放入 `context`，并说明依据。

生命周期把 capability 分成四个维度：

| 维度 | 取值 |
| --- | --- |
| `proposal_state` | `unknown` / `proposed` / `discussing` / `accepted` / `rejected` / `superseded` |
| `implementation_state` | `unknown` / `not_started` / `in_progress` / `partially_merged` / `merged` / `reverted` |
| `delivery_state` | `unknown` / `not_found` / `prerelease` / `released` |
| `availability` | `unknown` / `experimental` / `opt_in` / `default_on` / `deprecated` / `removed` |

提升规则：

- `merged` 必须同时有 `merged=true` 和非空 `merged_at`；closed unmerged 永不算 merged。有 merged PR 但 `requirements.remaining` 非空，或 PR files 覆盖不足 / truncated / diff unavailable 时降为 `partially_merged`。`diff=not_requested` 只记 gap，不单独降级。
- `reverted` 只来自显式 `reverts` 关系或后续 merged revert 证据；不按活动量或时间间隔推断。
- Issue/Discussion 的 closed 不等于 accepted/fixed；duplicate 视为 superseded，not planned 视为 rejected，closed completed 仍不自动升级为 accepted。
- `verified_in_release` + release tag 才产生 `delivery_state=released` 或 `prerelease`、`delivery_verification=verified`。只有 `release_note_mentions` 时为 `delivery_state=unknown` / `delivery_verification=note_only`；没有 Release 证据为 `unknown`。
- 时间线按 `verified_in_release` 的**关系对**逐条校验，不做 release 与 PR 的交叉乘积：某个 Release 的 `published_at` 必须不早于它所验证的每个实现 PR 的 `merged_at`，该 Release 才计入 `verified_versions` 并支持交付声明。不满足的关系对记 `release_before_implementation:<tag>:<pr>` 并从交付证据中剔除；部分 Release 失效只记 gap/warning，只有当没有任何有效 Release 时才阻断 `released` / `prerelease` 声明。缺 `published_at` 时不判时间线（按未验证处理，只保留关系）。
- `verified_in_release` 的目标只接受已合并的 PR，或可追溯到某个 merged PR `commits` / `merge_commit_sha` 的 commit；未合并、缺失或其它 kind 的目标会被剔除并记 `verified_target_not_merged` / `verified_target_missing`。commit 的时间线取包含它的 merged PR 中最早的 `merged_at`；无法解析时记 `release_timeline_unresolved` 并剔除该 Release。没有任何有效目标的 Release 不计入 `verified_versions`，并记 `verified_release_without_target`。
- 当前规则引擎不产生 `not_found`：输入不足以证明“已检查全部候选 Release”。schema 允许该值，但没有穷尽性证据时不要声明；优先写 `unknown`。
- `availability` 默认 `unknown`；`default_on` / `deprecated` / `removed` 必须有 doc/config 证据，否则回落 `unknown` 并记 `availability_without_document_evidence`。
- 当前 prepare 只生成 `mentions` / `closing_reference` / `contains_commit` / `release_note_mentions`；`verified_in_release` / `implements_part_of` / `documents` / `reverts` 需通过 `import-source` 的 `relations[]` 提供。导入关系必须显式写 `verification=verified` 且 `basis` 为 `api_explicit` / `text_explicit` 才参与状态推导；`verified_in_release` 还必须引用本次导入的 evidence，不能只凭已有 PR 正文或 release note 声称。`verification=verified` 的关系引用的所有 evidence 必须为 `verified`（`contains_commit` 允许空 evidence）；`disputed` 一旦出现即粘滞，后续导入不能恢复为 verified，本轮不提供撤销命令。claim 的 `scope.entity_ids` 只有在其 `evidence_ids` 全部为 `verified` 时才参与直接实体集。
- capability 先通过 evidence 的 `entity_id` 和相关 claim 的 `scope.entity_ids` 建立直接实体集，再沿 `implements_part_of` / `contains_commit` / `verified_in_release` / `release_note_mentions` / `reverts` / `documents` 做最多 2 跳扩展；release 只能是扩展终点，不能作为继续扩展的起点（避免一个 Release 把无关 PR 串起来）。不按标题或正文关键词猜实现对象。
- `check_state_assertions` 只做形式化门禁：merged 与 remaining 冲突、released 缺 verified_in_release、availability 缺文档证据、claim 的 capability 不存在、unknown 缺 reason/missing_evidence、claim 版本未验证、reverted/merged 冲突。语义判断仍由 Agent 自查与离线评测负责。
