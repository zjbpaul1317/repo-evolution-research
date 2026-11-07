# 采集来源与覆盖语义

本文件说明 `collect` 如何发现和读取材料，以及 coverage 为什么会出现 `partial` / `unavailable`。命令契约和恢复规则见 [`workflow.md`](workflow.md)。

## 共同规则

- 窗口是左闭右开 `[start, end)`；`end` 缺省为运行开始时刻。存储用 UTC，展示按配置时区。
- 窗口成员使用各来源自己的时间键（活动视图不同、Release 用 `published_at`）。当前 API 返回的正文、标签和状态可能已在窗口结束后修改；默认报告说“研究 X–Y 期间活动，状态核对截至 Z”，不声称恢复了 Y 当天的完整状态。
- 长文本一律写入 `blobs/`，权威文档只存 `text_ref`；不复制 GitHub 响应。
- 可变材料在 collector 重建文档时每轮重读。只有绑定代码修订键且 section 完整的 `files` / `commits` 可以复用；selection 对 section 已完整且未请求补读的对象复用快照（`already_read`），不重新请求。
- 窗口外材料可以作为背景或后续观察，但必须标明 `context` 或“后续观察”，不能计入窗口内活动。

## 仓库发现

- 解析 canonical `host/owner/repo`，读取默认分支和固定 `default_branch_sha`；后续文档快照使用该 SHA。
- 发现目录树、文档/RFC 候选路径、标签和协作能力；不假设 `main`、`docs/` 等固定路径。
- 目录树截断、发现路径缺失、Discussions 能力不可用都是独立 coverage channel。`tree` / `discovery_paths` / `discussions_capability` 的 `partial` / `unavailable` 会阻塞运行。
- 文档候选只接受文本扩展名（`.md`、`.markdown`、`.rst`、`.txt`、`.adoc`、`.ipynb`）；图片、PDF、压缩包记为 `non_text_paths_filtered`，不计入 `expected_count`。

## 活动发现

当前使用六个视图：

| 视图 | 时间键 | 查询语义 |
| --- | --- | --- |
| `pr_merged` | `merged_at` | 窗口内合并的 PR |
| `pr_created` | `created_at` | 窗口内创建的 PR |
| `pr_updated` | `updated_at` | 窗口内更新的 PR |
| `issue_created` | `created_at` | 窗口内创建的 Issue |
| `issue_updated` | `updated_at` | 窗口内更新的 Issue |
| `discussion_updated` | `updated_at` | 窗口内更新的 Discussion |

- 优先按日期分片做 GitHub search；单个分片达到搜索上限或返回 `incomplete_results` 时，降级为分页列表回退。`strategy` 和 `fallback_reason` 记在 `analysis/activity-overview.json`。
- 全局 `max_discovery_entities` 按视图分配基础配额；前面视图未用完的配额回流给后续视图。达到自身配额记 `discovery_view_quota`，轮到时全局上限已耗尽记 `discovery_entity_limit_unstarted`。这样 PR 活动量再大也不能让 Issue/Discussion 完全饥饿；局限必须同时记录“被截断”和“未开始”的区别。
- “search 完整”只表示搜索索引内完整，不是事务快照。窗口成员使用当前对象元数据；历史 `updated` 视图使用当前 `updated_at`，窗口内更新过、之后又更新的对象可能被漏掉。窗口结束早于采集超过 10 分钟时记 `historical_updated_window`。
- 多视图合并后 `expected_count` 为 null；不要拿单视图总数与去重后的候选数直接比较，使用 overview 中的逐视图 totals。
- 客户端分页上限为 100 页。触顶是预算中断：写出部分 overview，视图标 `pagination_max_pages`，`collect` 退出 4。
- activity 刷新是 raw/entity 的当前元数据刷新；保留深读文档产生的 `files`、`commits`、`reviews`、`diff`、`timeline`、角色和 evidence 指针。重跑 activity 不会抹掉深读结果。

## PR 深读

- 从活动候选中按 `min(max_deep_prs, max_deep_entities) - agent_pr_reserve` 选择初始深读 PR：quick 6 / standard 18 / deep 36；Agent selection 可继续使用预留名额，最多到 `min(max_deep_prs, max_deep_entities)`。standard 初始约 11 个 merged + 7 个 open。所有决策写入 `analysis/pr-selection.json`。
- 每个选中 PR 读取 detail、files、commits、issue comments、reviews、review comments，写一份权威文档 `raw/pr-details/<number>.json`。长文本和 patch 进 `blobs/`；entity 的 `raw_state.deep_read` 只存 `detail_path`、`head_sha`、`observed_at` 指针。
- `head.sha` 在读取开始和结束之间变化时置 `head_changed`，PR 标 `partial`；不能当作一次一致观察。`squash_rebase_unverified` 表示未重建最终合并补丁。
- 当前版本不抓完整 diff：`pr_detail.diff.status` 恒为 `not_requested`，schema 字段保留给未来选项。
- `files` 上限 3000、`commits` 上限 250；二进制或超大文件可能没有 patch。这些是固有缺口，记录在 coverage/limitations，但 `pr_detail` 只在 `unavailable` 时阻塞运行。
- 全部选中 PR 都没有文档时 `pr_detail=unavailable`，退出 4；部分失败或任一文档不完整只记 `partial`。fatal 中断时当前 candidate 保持 `not_attempted`，不伪装成 PR 级失败；运行级错误由 checkpoint 和 manifest 记录。进度写在 `checkpoints/collect.json` 的 `stages.pr_detail`，恢复时以文档为准。
- 刷新失败或旧文档更完整时保留旧文档；`fetched_count` 只统计本轮成功重建或确认未变的 PR（`selected` / `skipped_unchanged`），保留的旧文档不计入 fetched，但参与状态派生。

## Release

- 权威文档写 `raw/releases/<release_id>.json`；entity ID 用 tag（`host/owner/repo/release/<tag>`）。
- `tag_name` 只是 Release 声明的标签；`tag_ref.sha` 是 ref 解析结果；annotated tag 必须继续解引用到 `tag_ref.commit_sha`。`target_commitish` 只保留为 API 原始字段，不能当成发布提交。
- 窗口按 `published_at` 过滤；draft/prerelease 只作为标志记录，不能用 `created_at` 冒充发布时间。draft 或 tag 无法解析都必须在 coverage/limitations 中可见。
- Release 列表和 note 可变，每轮重读；重建比较忽略 `observed_at`，内容未变记 `skipped_unchanged`。
- 旧文档完整、本次重建不完整（如 tag 被删除或移动）时保留旧文档，计数 `retained`，coverage 记 `partial`，`truncation_reason=refresh_incomplete:<tag>`。tag 移动保留 `previous_commit_sha` 并记 `tag_moved`。
- 列表最多保留 `MAX_RELEASES=200` 条；翻页重复 `release_id` 按首次出现去重并记 `pagination_duplicates`。Release 通道另有 `MAX_RELEASE_REQUESTS=80`；超出后停止刷新剩余 Release，记 `release_request_limit:<n>`，channel 保持 `partial`。
- `releases=unavailable` 阻塞运行；单个 tag 解析失败、刷新降级、分页漂移只记 `partial`。

## 文档快照

- 文档写 `raw/docs/<revision>/<path>.json`；`revision` 必须是完整 40 位 commit SHA，`path` 是仓库相对路径。旧 revision 快照保留归档，但只有当前 `default_branch_sha` 的 doc 进入实体投影；被 imported evidence/relation 引用的旧 doc entity 在 revision 切换时保留，避免悬空。`import-source` 只允许引用当前 revision 的 doc entity。
- collect 只读取默认分支固定 revision 的文档；`latest` / `stable` 等移动 URL 只作为来源观察，不能作为版本证明。外部 envelope 通过 `import-source` 导入，再重跑 `prepare`；不混入 collect 主流程。
- imported source 的正文按内容寻址 `blobs/<sha256>`；来源元数据写 `imported/<source_key>/<sha256>.json`，`source_key` 由 `source_kind` / `entity_id` / `source_url` / `source_revision` 决定。不同 URL/entity 或不同 revision 不共享来源记录；重复检索追加 `observations`，不覆盖首次观察。
- 非 UTF-8、过大、缺失或不可访问只记录 status/limitation，不能伪造文本。固定 revision + path 且 blob/hash/bytes 校验通过时可复用；revision 或 path 变化必须重读。
- 候选上限 `MAX_DOCS=40`。`docs=unavailable` 只在全部候选请求失败时成立；`not_found`、`too_large`、`unsupported_encoding`、`decode_error` 只记 `partial`；`include_docs=false` 记 `not_requested`；没有文本候选记 `not_applicable`。
- Doc entity 只投影当前 `default_branch_sha` 的快照；旧 revision 只作为归档，不进入 `normalized/entities.jsonl`。

## Issue / Discussion 深读

- 候选来自窗口活动，以及已选 PR 正文中的窗口外引用。`scope` 区分 `in_window`、`context`、`current_backlog`；`context` 只来自 PR 正文的 `#number` 或 `/discussions/<number>` 引用。`current_backlog` 是保留值，当前采集器不产生，也不主动搜索长期未更新的开放 Issue。
- 关键词命中只产生 `role_hints`，不是已确认角色；`entity.roles` 不承载 hints，确认后才由 prepare 提升。
- 配额为 `min(8, max_deep_entities - deep_entities_used)`，其中 Issue 最多 6、Discussion 最多 2；未用满的槽位按全局分数回填。配额不足记 `quota_limited_selection`。
- Issue 用 REST 读取 body/comments/timeline；Discussion 用 GraphQL 读取 body/comments/replies/category/upvotes。GraphQL partial data 必须保留已取到的节点并把 comments/detail 标 `partial`，查询失败不得写成空数组成功。
- Issue/Discussion 没有 head 语义：body、comments、timeline、replies、state 每轮重读，不套用 PR 的 head 跳过规则。旧文档完整、本次重建不完整时保留旧文档并记 `refresh_incomplete`。
- 分页漂移产生的重复项必须去重：PR files/commits/comments/reviews 分别按 `filename`/`sha`/`id`，Issue comments/timeline 按 `id`，Discussion comments/replies 按 node `id`。重复计数写入 section 的 `duplicates`，并在 limitations 与 coverage `truncation_reason` 记 `pagination_duplicates`。
- `state_reason=not_planned` / `duplicate` 记 `closed_not_planned` / `closed_as_duplicate`；`closed` 不自动等于已修复。对象不存在（删除、私有、无权）是持久事实，候选记 `skipped_reason` 并移出 selected，不把通道推成 `unavailable`；访问失败记 `failed`。
- replies 截断必须区分 `replies_continuation_limit`、`replies_page_limit`、`replies_graphql_errors`、`replies_pagination_incomplete`。
- 通道另有 `MAX_PROPOSAL_REQUESTS=80`；耗尽时停止剩余候选，记 `budget_exceeded` / `proposal_request_limit`，channel 保持 `partial`，不终止整轮 collect。

## 覆盖通道与阻塞

`collectors/progress.py` 的 `CHANNEL_POLICIES` 是唯一判定来源；不要在 CLI 或报告里硬编码“哪个通道阻塞”。

| 通道 | 阻塞状态 |
| --- | --- |
| `tree`、`discovery_paths`、`discussions_capability` | `partial`、`unavailable` |
| `activity_prs`、`activity_issues`、`activity_discussions` | `partial`、`unavailable` |
| `pr_detail`、`issue_detail`、`discussion_detail`、`releases`、`docs` | `unavailable` |

- 阻塞通道命中且未在 `limitations` 说明时，`validate` 阻断；非阻塞降级（如 `pr_detail=partial`）只记 `coverage_degraded` warning。
- `expected_count` / `fetched_count` 必须由同一份选择记录派生：PR/提案的 `fetched_count` 只统计 `selected` + `skipped_unchanged`；Release 只统计本轮 written + skipped；docs 只统计 `read`。保留旧文档会造成 `fetched_count < expected_count` 且 `coverage_status=partial`，这是合法状态。
- `not_requested` / `not_applicable` 不阻塞，但要在局限中说明未采集。

## 预算与请求上限

预算字段和默认值见 [`configuration.md`](configuration.md)。要点：

- `max_requests`、`max_runtime_minutes`、`max_total_bytes` 跨同一 run 的多次尝试累计；`--force` 不重置。`max_response_bytes` 是单次响应上限，`response_bytes` 只用于审计。
- 单通道另有硬上限：PR files 3000、commits 250；Release 200 条/80 请求；提案 80 请求；docs 40 篇；客户端分页 100 页。
- 全局请求/响应/时间预算耗尽会置 run-level `budget_exhausted`，`collect` 以退出 4 结束并保留部分结果；`pr_detail` / `releases` / `docs` / 提案通道的内部限额只降级对应 channel，不升级为 run-level 失败。
