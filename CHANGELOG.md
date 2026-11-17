# Changelog

## Unreleased

### 改进

- 新增 `tests/test_install_boundary.py` 安装边界验收：只复制 Skill 目录到含空格与中文的路径，将安装目录设为只读，从无关的项目目录运行 `doctor`、`init`、`audit`，并逐文件比对安装目录在运行前后一致；同时覆盖 uv 缺失时的可操作错误。默认以 `RER_PYTHON` 执行，`RER_TEST_REAL_UV=1` 时走真实 uv 与锁文件。
- CI 的 `lockfile` job 改名 `install-boundary`，在锁新鲜度检查之外，用真实 uv 执行一次复制安装验收。
- 启动器的失败路径改用 shell 内建（`printf`、参数展开）完成路径解析、版本解析和错误输出，在没有 `dirname`、`sed`、`head`、`cat` 的最小环境中也能给出可操作错误；依赖检测仍只要求 `bash` 与 `uv`。
- `doctor` 结果新增 `runtime` 环境溯源：启动方式（`direct` / `uv-locked` / `python-override`）、解释器路径与版本、平台、uv 路径与版本、`offline` 和 `UV_CACHE_DIR`；text 模式追加同名 `runtime:` 行。`rer` 通过 `RER_LAUNCHER` 标记实际入口，`dependencies_locked` 只表示是否走 `--locked` 路径。
- 版本号以 `rer/__init__.py` 的 `TOOL_VERSION` 为运行时唯一来源；新增 `tests/test_version_sources.py` 校验 `pyproject.toml`、中英文 README 和 `references/workflow.md` 一致，并移除 `tests/test_schemas.py` 中的重复硬编码。
- 新增 Skill 目录下的 `rer` 启动器：依据 `scripts/research.py` 的 PEP 723 内联元数据与相邻的 `research.py.lock`，通过 `uv run --locked --no-build --script` 准备隔离运行环境；保留调用者工作目录，stdout、stderr 与退出码原样透传。uv 缺失、版本低于 0.9 或环境准备失败时以 exit 3 报告原因与恢复方式。
- `research.py` 在导入 CLI 前校验解释器与平台：Python < 3.11 或非 macOS/Linux/WSL 时输出结构化 `environment_error`（`source=bootstrap`）并以 exit 3 结束，不再抛出 traceback。`RER_PYTHON` 可绕过 uv，但会在 stderr 声明依赖未经锁文件校验。
- `SKILL.md` 的命令改为 `bash "$SKILL_DIR/rer"`，不再假设仓库根目录；frontmatter 新增 `license` 与 `compatibility`，正文新增 Setup 小节。新增 `references/setup.md`，覆盖 uv 安装、企业镜像、`UV_PYTHON_DOWNLOADS`/`UV_OFFLINE`、`GH_TOKEN` 与锁文件维护。
- README 中英文快速开始改为启动器入口，运行时依赖不再需要手工 `pip install`；开发检查新增脚本锁文件校验。
- 完整 collect 现在逐渠道向 stderr 输出 coverage 状态、条目/预期数、页数、已用请求、响应字节和耗时；失败时补充结构化错误码及已消耗预算。stdout JSON 契约保持不变。
- 默认 GitHub 子进程使用 selector 分段读取 stdout，将捕获的子进程输出限制在单响应上限加 64 KiB HTTP 头余量，并在超限时终止子进程；stdout 以外的响应解析仍会产生有界的正文副本。请求超时取 120 秒与 run 剩余时长中的较小值；stderr 仅保留 64 KiB 诊断并继续排空管道。

### 验证

- 2026-11-17，macOS arm64 / Python 3.13.3：`pytest -q` 1065 passed；`RER_TEST_REAL_UV=1 pytest tests/test_install_boundary.py` 在真实 uv 0.9.18 下通过，只读安装目录在 `doctor`、`init`、`audit` 前后逐文件一致。lint、生成资产与 `git diff --check` 通过。未在 Linux 上执行；CI job 定义不等于已观测通过。
- 2026-11-17，macOS arm64 / Python 3.13.3：`pytest -q` 1063 passed；lint、生成资产与`git diff --check` 通过。经 `rer` 运行时 doctor 的 `runtime` 显示 `launcher=uv-locked`、uv 缓存解释器与 uv 0.9.18；直接以 `.venv/bin/python` 运行时显示 `launcher=direct`。未在 Linux 验证。
- 2026-11-17，macOS arm64 / Python 3.13.3：`pytest -q` 1059 passed（新增 7 个 bootstrap 用例）；`scripts/gen_assets.py --check`、lint 与 `git diff --check` 通过；uv 0.9.18 下 `uv lock --script ... --check` 通过。启动器在临时目录及 `RER_PYTHON` 入口运行 `--help` 均成功，`/usr/bin/python3`（3.9.6）直接运行脚本返回 exit 3 与结构化 bootstrap 错误。本轮未验证 Linux/Windows，也未运行新的真实 GitHub 采集。
- 2026-11-14，macOS arm64 / Python 3.13.3：公开 `vllm-project/router` 14 天窗口真实采集、强制重采和 selection 深读均无阻塞 coverage；最终 104 次请求、10,902,865 字节、4 个深读实体。最终 audit 核验 1,535 条引文、109 条关系、419 个文本 blob，通过。详见 [`real workflow acceptance`](docs/acceptance/2026-11-14-real-workflow.md)。
- 真实采集发生在进度输出与有界子进程 runner 修改之前；新版 runner 单独完成一条公开 GitHub API 请求冒烟。完整离线回归、runner 限流/超时和 CLI 进度测试在修改后运行。

### 修复

- 仓库 URL 的畸形 authority / 端口返回 usage_error（exit 2），拒绝内部控制字符与空 userinfo，避免 URL 解析器静默移除制表符后误接收地址。畸形分页 URL 返回结构化 GitHubError；错误诊断不回显无法安全解析的凭据。
- GraphQL 通用分页校验 connection、nodes 和 pageInfo，缺失或畸形字段保留可用节点并标记 partial；无有效 data/errors 的响应返回结构化错误，避免 Discussion 发现阶段把协议异常记成完整空结果。畸形 errors 列表保留有效错误并补充诊断。
- HTTP 响应只从开头解析状态和响应头，避免文档或 diff 正文中的 `HTTP/1.1 404` 等示例被误判为请求失败。支持前置的 informational 响应头；子进程以字节接收后显式 UTF-8 解码，保留正文中的 CRLF。
- REST / GraphQL 分页发现重复 Link / cursor 时及时停止，按采集错误保留部分结果，避免循环消耗整轮请求预算。
- Review Thread 外层列表遵循客户端页数上限；重复游标、后续页错误、嵌套评论请求失败与 GraphQL partial data 都保留已取得的线程和 comment ID，并明确标记部分采集。失败的评论后续请求也计入单 PR 的 20 次限额。
- Discussion comments / replies 遇到重复游标时停止；回复数量不足 `totalCount` 或存在畸形节点时保持 `partial`，避免后续分页把已有缺口重置为 `complete`。
- JSONL 去重索引在锁内核对文件身份、大小和修改时间；其他存储实例写入、替换、删除或同大小覆盖后重新加载，避免重复或漏写。写入后同步失效同一文件其他去重键的缓存。
- JSON Pointer 按 RFC 6901 核验数组下标和 `~0` / `~1` 转义，拒绝负数、前导零、带符号/空白或非 ASCII 的数组下标，避免非法 locator 被误记为 verified；合法的对象键与空指针仍可读取。
- URI 格式校验将畸形 IPv6 / netloc 的解析失败转为 schema 诊断；配置、来源 envelope、selection 与快照 JSON 的非法 UTF-8 保持各自的输入/产物错误类别。
- 共享 REST section 遇到非对象条目保留可用内容并标记缺口；截断和总数不匹配即使未配置自定义原因也保持 partial。窗口停止原因与畸形条目原因并存，避免 Release 采集把已有缺口恢复为 complete。
- Review Thread 初始或后续评论页的畸形节点保持部分状态，即使后续页已补齐数量；外层 nodes 缺失也不能当成完整空列表。Discussion 后续非致命评论/回复请求失败保留已取内容，失败的回复请求计入后续请求限额。
- audit 隔离 schema 无效的记录，继续输出诊断；非法 UTF-8 manifest、畸形 candidates 和不可读阅读包不会中断问题清单。validate 分流加载证据账本并记录具体 schema 失败，避免把畸形数据交给引用/状态推导。
- 只读 SnapshotStore 拒绝写入 blob；blob API 的非法 hash 类型与读取错误返回结构化 StoreError。

### 性能

- 阅读包超预算时按既有优先级二分查找最短淘汰前缀，替代逐条删除后重复渲染；保留候选顺序、Agent 选择优先级、淘汰原因和精确字符预算。与 `1cf3cf1` 对比，macOS / Python 3.13.3，单组 1,000 个合成候选、8,000 字符预算、预热后 5 轮：预算裁剪中位数由 4.427 秒降至 0.0397 秒（约 112 倍），保留/淘汰结果完全一致。该结果仅覆盖裁剪步骤，非完整 prepare 或真实采集。
- JSONL 整体替换、upsert 与冲突覆盖逐条编码到临时文件，取消整文件字节拼接；序列化失败或中断时清理临时文件，保留原件。新增 `scripts/benchmark_jsonl.py` 对比旧编码路径和当前完整 write_jsonl（含去重、fsync、索引重建）：macOS arm64 / Python 3.13.3，10,000 条、10,508,890 B、5 轮，分配峰值中位数从 22,401,818 B 降至 11,257,777 B（约 49.7%）；耗时从 0.355 秒到 0.372 秒，未观察到提速，输出 SHA-256 一致。记录对象和索引仍驻留内存。
- JSONL 读取用文件末尾单字节检查替代整文件预读，减少解析时额外的内存副本。macOS / Python 3.13.3 的合成快照（20,000 条记录，10,788,890 字节）三轮读取中位数：`tracemalloc` 峰值由 28,364,795 降至 17,575,872 字节（约 38.0%）；耗时 0.134 秒与 0.136 秒，未观察到速度提升。该结果只覆盖本地合成读取，不代表真实 GitHub 端到端性能。
- ledger/research 摘要、报告产物审计、已有 blob 核对和 verify_blob 使用流式 SHA-256。新增 `scripts/benchmark_hashing.py`：macOS arm64 / Python 3.13.3，32 MiB 文件、5 轮、预热缓存，Python 分配峰值中位数从 33,559,442 B 降到 267,945 B（约 99.2%），耗时中位数从 17.20 ms 降到 15.52 ms；摘要一致性通过。该结果只覆盖本地文件哈希，不代表端到端性能。

### 开发检查

- 安装边界用例将只读安装目录、路径含空格/中文、`__pycache__` 抑制和安装目录零写入合并为一个可重复场景；CI 在真实 uv 下复跑。
- 新增 doctor 环境溯源用例，覆盖 `uv-locked`/`direct` 两种启动方式、uv 缺失和 `offline` 标记；新增版本来源一致性用例。
- 新增 `tests/test_bootstrap.py`：断言 PEP 723 内联元数据与 `pyproject.toml` 的 `requires-python`/`dependencies` 一致、`research.py.lock` 存在且覆盖声明的依赖、启动器可从任意目录运行并透传退出码，以及旧解释器返回结构化 `environment_error`。
- CI 新增 `lockfile` job，使用 `uv>=0.9,<0.10` 执行 `uv lock --script ... --check`，防止内联依赖改动后漏提交锁文件。
- lint 与编译检查覆盖 `evals/*.py` 和维护脚本，保留对本地 `evals/results/` 的排除；中英文 README 与维护指南补充性能对比命令和测量边界。

### 兼容性

- 工具版本仍为 `0.2.9`，快照 schema 仍为 `1.3`。新增的诊断原因沿用现有字符串字段，无新字段或枚举，不需要迁移已有 run；已有证据与 blob 不会被批量重写。
- 非法 JSON Pointer 曾被旧工具核验通过的证据，在本次重新 validate/audit 时会报告定位失败；应修正 locator，不能依赖旧 verification 标记绕过核验。

### 验证

- 2026-11-13，基于 `0a1dd41` 的工作区改动，本机 macOS / Python 3.13.3：`pytest -q` 944 passed（基线 917）；生成资产一致性、lint（含 Ruff）和 `git diff --check` 通过。
- 新增 27 个回归与边界用例，覆盖响应正文保留、分页循环/限额/partial data、Discussion 回复完整性与存储索引失效。完整离线工作流仍通过；本轮未运行新的真实 GitHub 采集或 Linux 验证。
- 同日继续整体优化，在上述 944 测试工作区上新增 48 个回归与边界用例；最终 `pytest -q` 为 992 passed（18.65 秒），生成资产一致性、扩展后的 lint 和 `git diff --check` 均通过。新增覆盖 RFC 6901、畸形 URI/UTF-8、只读 blob、损坏快照审计、REST section 缺口、GraphQL 内容保留以及哈希契约/同大小篡改；验证环境仍为本机 macOS / Python 3.13.3。

## 0.2.9 - 2026-11-08

### 新增

- PR review thread resolution：通过 GraphQL 读取 `isResolved` / `isOutdated` / path / line 与关联 comment databaseId；线程列表分页，失败时明确标记 `partial` / `unavailable`。评论正文仍保存在 `review_comments`。
- Review thread 单线程评论嵌套分页：超过 100 条时用 `node(id)` 继续翻页，单线程最多 10 页、单 PR 最多 20 次后续请求；触顶、cursor 缺失或 GraphQL 错误时保留已取 ID，并明确标记截断原因。
- 文档提交历史：优先设计/RFC/架构/roadmap 类文档，最多 15 篇 × 20 条提交；阅读包展示 status/fetched/latest/truncated 摘要。固定 revision + path 可复用，`--force` 重新读取。
- Revert/backport 关系：从 PR 正文、评论和 commit message 中识别显式文本引用（如 `This reverts commit <sha>`、`Backports #123`、`Cherry-picked from <sha>`）。`reverts` 可解释实现被回退；`backports` 只记录后向关系，不提升 capability 状态。
- 可变单对象和分页 REST 列表的 ETag 条件请求：PR/Issue detail 与 `iter_rest_pages` 逐页命中 304 时复用本地 payload；列表页同时缓存 `link` / `has_next`，304 缺少 Link 时使用缓存或回退全量请求，避免静默少翻页。缓存 blob 缺失或损坏时回退全量请求。

### 修复

- 再次自动 `collect` 时保留已有 PR 深读：已有权威文档以 `already_read` 重新选中并刷新可变 section，不占新的 deep entity 名额；未变化记 `skipped_unchanged`，避免 `pr-selection` 清空、`pr_detail` coverage 被误记为 `not_requested`。
- 同一恢复逻辑覆盖 Issue/Discussion 深读：已有提案文档保留在 `proposal-selection` 中，避免 `issue_detail` / `discussion_detail` coverage 被误记为 `not_requested`。
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
- 1.3 内新增的 review thread、文档历史、pack 摘要和 `backports` 是增量字段；新工具可读取旧 1.3 快照，但 0.2.8 旧工具不保证读取含这些字段/枚举的新快照。若需要双向兼容，应单独升级 schema 版本并说明迁移方式。
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

- `pytest -q`：917 passed。
- `python scripts/gen_assets.py --check`：通过。
- `bash scripts/lint.sh`：通过。
- `git diff --check`：通过。
- 真实 e2e：`vllm-project/router` 14 天窗口（2026-10-25 至 2026-11-08），run `20261108T035317Z-fae551e9`；4 个 PR 深读、57 个 review thread、2 篇文档历史、102 次请求（含 4 次 ETag 304）。
- e2e 结果：research 2 个主题、21 条 claim、3 个 capability、2 个 unknown；validate `ok=true`（0 errors / 13 warnings）；render 22,638 chars，sha256 `3da972002cc8b8117e6e66b4b5b9e4d4c013d5fcbbb6ea69673e78353caf8995`；audit `ok=true`。
- 该 run 的 manifest `tool_version` 为 0.2.8、schema 1.3；报告由 0.2.9 代码 validate/render，schema 未变化。
- 分页 ETag 由 `tests/test_github.py` 的 fake transport 覆盖；本轮没有为分页 ETag 重新 collect 真实 run。
- 两轮真实 collect 冒烟（2026-11-09）：`vllm-project/router` 14 天窗口，run `20261109T023902Z-dbae697a`；首轮 49 次请求、深读 1 个 PR，第二轮 `--force` 后 84 次请求，`deep_entities_used=2`、`pr_quota=0`，已有 PR #410 仍为 `selected`/`already_read`，`pr_detail` coverage 为 `complete`（expected/fetched 1），`audit ok=true`。
- 完整 e2e（2026-11-09）：`vllm-project/router` 14 天窗口，run `20261109T025145Z-17190672`；49 次请求、1 个 PR、1 个 RFC、1 个 release 和 4 篇文档，research 2 个主题、15 条 claim、2 个 capability、3 个 unknown；validate `ok=true`（0 errors / 9 warnings）；render 14,617 chars，sha256 `a4142bf89160062f6e6ce16ae385b14f6178ea978026f1ee84c83f10d1103545`；audit `ok=true`。
