# Changelog

## 0.2.9 - 2026-11-08

### 新增

- PR review thread resolution：通过 GraphQL 读取 `isResolved` / `isOutdated` / path / line 与关联 comment databaseId；线程列表分页，失败时明确标记 `partial` / `unavailable`。评论正文仍保存在 `review_comments`。
- Review thread 单线程评论嵌套分页：超过 100 条时用 `node(id)` 继续翻页，单线程最多 10 页、单 PR 最多 20 次后续请求；触顶、cursor 缺失或 GraphQL 错误时保留已取 ID，并明确标记截断原因。
- 文档提交历史：优先设计/RFC/架构/roadmap 类文档，最多 15 篇 × 20 条提交；阅读包展示 status/fetched/latest/truncated 摘要。固定 revision + path 可复用，`--force` 重新读取。
- Revert/backport 关系：从 PR 正文、评论和 commit message 中识别显式文本引用（如 `This reverts commit <sha>`、`Backports #123`、`Cherry-picked from <sha>`）。`reverts` 可解释实现被回退；`backports` 只记录后向关系，不提升 capability 状态。
- 可变单对象和分页 REST 列表的 ETag 条件请求：PR/Issue detail 与 `iter_rest_pages` 逐页命中 304 时复用本地 payload；列表页同时缓存 `link` / `has_next`，304 缺少 Link 时使用缓存或回退全量请求，避免静默少翻页。缓存 blob 缺失或损坏时回退全量请求。

### 修复

- 再次自动 `collect` 时保留已有 PR 深读：已有权威文档以 `already_read` 重新选中并刷新可变 section，不占新的 deep entity 名额；未变化记 `skipped_unchanged`，避免 `pr-selection` 清空、`pr_detail` coverage 被误记为 `not_requested`。
- 文档历史摘要按 `(revision, path)` 精确匹配，并解码 entity ID 中的 percent-encoding；避免多个 revision 同路径时展示旧历史，以及含空格/非 ASCII 路径时摘要丢失。
- `reverts` / `backports` 支持同一关键词后的多个相邻引用（如 `Reverts #12 and #13`），不再只记录第一个。
- `backports` 识别 Git 默认的 `(cherry picked from commit <sha>)` 文本，不再只识别带连字符的 `Cherry-picked from`。
- PR 部分读取即使关闭 comments/reviews，也会重读 review thread 元数据；缺失 comments connection 时明确标记 `partial` 与截断原因。
- 文档历史恰好 20 条时不再误报截断；读取时多请求 1 条用于判断是否还有更多提交；含畸形提交行时按原始响应条数判断截断。

### 变更

- 断点恢复：`files` / `commits` 仅在 head/base SHA 未变、section 完整且 text blob 校验通过时复用；可变 review thread 每轮重建都重读；旧文档完整而本次重建不完整时保留旧文档并标记 `refresh_incomplete`。
- 文档历史复用与 `--force` 语义写入 `sources.md`；阅读包新增 review thread 和文档历史摘要。
- README 与 workflow 版本号更新到 `0.2.9`，补充 review thread、文档历史、revert/backport 和 ETag 的能力边界。

### 兼容性

- 快照 schema 仍为 `1.3`，不需要迁移；已有 1.3 run 可继续读取和重新 prepare。
- `collect` 的兼容检查以 manifest `schema_version` 为准，不因工具版本从 0.2.8 升到 0.2.9 而拒绝。
- ETag 缓存 key 加入 page；升级后旧缓存条目会 miss 一次并重新获取，不影响快照或证据。
- 仍从源码运行，不提供 PyPI 包；报告固定中文。

### 已知限制

- ETag 304 仍会计入本地请求预算和子进程时间；缓存条目上限为 2000，超过时按 `validated_at` 淘汰。
- Review thread 单线程评论使用有界嵌套分页；超过 10 页或单 PR 20 次后续请求时仍会标记 `comments_truncated`。
- Backport/revert 只识别显式文本，不按 label、release branch 或分支名做启发式推断。
- 不采集完整 diff；不提供多语言报告或跨窗口比较。
- 本轮验证为离线测试加一次小窗口真实 e2e 报告；未完成独立领域评审或完整 vLLM 端到端重放。

### 验证

- `pytest -q`：916 passed。
- `python scripts/gen_assets.py --check`：通过。
- `bash scripts/lint.sh`：通过。
- `git diff --check`：通过。
- 真实 e2e：`vllm-project/router` 14 天窗口（2026-10-25 至 2026-11-08），run `20261108T035317Z-fae551e9`；4 个 PR 深读、57 个 review thread、2 篇文档历史、102 次请求（含 4 次 ETag 304）。
- e2e 结果：research 2 个主题、21 条 claim、3 个 capability、2 个 unknown；validate `ok=true`（0 errors / 13 warnings）；render 22,638 chars，sha256 `3da972002cc8b8117e6e66b4b5b9e4d4c013d5fcbbb6ea69673e78353caf8995`；audit `ok=true`。
- 该 run 的 manifest `tool_version` 为 0.2.8、schema 1.3；报告由 0.2.9 代码 validate/render，schema 未变化。
- 分页 ETag 由 `tests/test_github.py` 的 fake transport 覆盖；本轮没有为分页 ETag 重新 collect 真实 run。
