# Collector Authoring

本文件面向维护者，不属于 Skill 运行时文档。目标不是做通用工作流引擎，而是让新 collector 只声明 section、解析函数和通道策略，不复制分页、状态、checkpoint 与 coverage 聚合。

## 最小骨架

```python
from collections.abc import Mapping
from typing import Any

from rer.collectors.base import CollectContext
from rer.collectors.pages import SectionSpec, fetch_section
from rer.collectors.status import derive_detail_status
from rer.collectors.texts import text_ref


def collect_issue_comments(
    context: CollectContext,
    number: int,
    parent: Mapping[str, Any],
) -> dict[str, Any]:
    spec = SectionSpec(
        name="issue_comments",
        endpoint="repos/{owner}/{name}/issues/{number}/comments",
        parse_item=lambda raw: {
            "id": raw.get("id"),
            "body": text_ref(context.store, raw.get("body")),
        },
        total_known_key="comments",
        mismatch_reason="issue_comments_total_mismatch",
    )
    section, pages = fetch_section(
        context.client,
        spec,
        owner=str(context.repository["owner"]),
        name=str(context.repository["name"]),
        number=number,
        parent=parent,
    )
    return section
```

完整可运行版本见 `tests/test_collector_authoring.py`；测试同时约束编排样板不超过 150 行。

## 1. 先确定所有权

- 一个采集事务只写一个权威文档；派生索引由投影函数生成。
- 不新增 checkpoint 文件，不新增 coverage 聚合层；运行状态统一走 `progress.py`。
- 新通道是否需要阻塞运行，由 `progress.CHANNEL_POLICIES` 注册决定，不在 CLI 中硬编码。
- 新文档需要 schema、`verify_integrity` 和恢复规则；不要复用不相关的旧文档。

## 2. 使用 `CollectContext`

新 collector 接收一个 `CollectContext`，不要重复传 `client` / `store` / `repository` / `clock`：

| 字段 | 用途 |
|---|---|
| `client` | GitHub 传输、预算计量 |
| `store` | blob、权威文档、快照路径 |
| `repository` | `host` / `owner` / `name` |
| `clock` | 可注入时间；用 `context.now()` 取当前时间 |
| `progress` | 可选；写 coverage 与阶段进度 |

## 3. 用 `SectionSpec` 声明 section

`SectionSpec` / `fetch_section()` 只覆盖 **REST list section**，底层是 `client.iter_rest_pages`。GraphQL、Discussion 深读、多载体 RFC 等非 REST 场景需要单独 adapter；它们复用 `CollectContext`、`text_ref`、状态/coverage 契约和 section 级分层，但不能硬套 `fetch_section()`。

`SectionSpec` 是 section 级声明，不是流程引擎：

| 字段 | 含义 |
|---|---|
| `name` | section 名；同时作为错误 source 前缀（`<name>.response`） |
| `endpoint` | REST 端点模板，支持 `{owner}` / `{name}` / `{number}`；调用参数 `name` 指仓库名 |
| `parse_item` | 把单个原始 item 映射为文档片段；非 `Mapping` 项会被跳过 |
| `cap` | 最多保留多少条；`None` 表示不设上限 |
| `cap_reason` | 达到 cap 时的 `truncation_reason` |
| `total_known_key` | 从父详情对象读取 `total_known` 的字段名 |
| `mismatch_reason` | 父详情总数与抓取数不一致时的 `truncation_reason` |

`fetch_section()` 负责分页、cap、`total_known`、`build_section()` 和页数统计，返回 `(section, pages)`。分页计数只来自 `FetchResult.pages`；请求预算仍由 `client.usage` 前后差值统计。

PR collector 的 5 个 section 已改为声明式实现，见 `collectors/pull_requests.py` 的 `_section_specs()`。Release 列表也通过 `SectionSpec` + `fetch_section_result()` 抓取；Issue comments/timeline 使用 `SectionSpec` + `fetch_section()`。Discussion、文档快照和外部 envelope 不套用 `fetch_section()`，只复用 `CollectContext`、`text_ref`、状态/coverage 契约和 section 级分层。

`fetch_rest_list()` 的可选参数：

- `stop_after(page_items)` / `stop_reason`：整页都早于窗口起点时提前结束分页。
- `dedupe_key(item)`：翻页漂移时按稳定键去重，返回 `None` 表示该条不去重；
  重复条数通过 `FetchResult.duplicates` 暴露。

错误 source 按 section 名生成（`<name>.response`），不再统一为 `pr_detail.response`；这是有意行为，便于定位失败 section。若外部监控依赖旧 source，需要同步调整。

## 4. 状态只用共享代数

- 单个 section 的状态由 `build_section()` 给出：`complete` / `partial` / `unavailable`（必要时 `not_requested` / `not_applicable`）。
- 多 section 文档用 `status.derive_detail_status()`。
- 通道状态用现有的 `derive_*` 函数或 `combine_status()`；不要手写状态字符串。
- 如果必须新增状态值，先改 `status.py`、`tests/test_status_truth_table.py` 和 [`../skills/repo-evolution-research/references/evidence-and-state.md`](../skills/repo-evolution-research/references/evidence-and-state.md)，并把该变更当作契约评审，而不是在 collector 里私自定义。

状态与 coverage 的写入：

```python
entry = coverage_entry(
    channel="issue_detail",
    query_scope="deep_read_selection",
    query=f"#{number}",
    expected=1,
    fetched=1,
    pages=pages,
    coverage_status=status,
    observed_start=started,
    observed_end=context.now(),
)
if context.progress is not None:
    context.progress.record(entry)
```

结束时调用 `progress.finish_channel(...)`，不要另写 checkpoint 或 coverage 聚合。

`expected_count` / `fetched_count` 必须由同一份选择记录派生。PR 深读的
`fetched_count` 只统计本轮成功重建或确认未变的文档；保留旧文档会让 channel
保持 `partial`，但不计入 fetched。新增 collector 若采用不同口径，先写进
[`../skills/repo-evolution-research/references/sources.md`](../skills/repo-evolution-research/references/sources.md)
并补 `verify_integrity` 检查。

## 5. 文本、时间与数值

- 长文本一律 `text_ref(context.store, value)`，不要把全文直接放进文档。
- 时间、整数、可选字符串、labels、reactions 使用 `collectors/values.py`。
- 解析函数可以调用 `text_ref` 写内容寻址 blob，但不得发请求、不得写权威文档、不得推导 channel 状态。

## 6. 提交与恢复

1. 读取所有 section。
2. 保存全部长文本 blob。
3. 构造文档并通过 `validate_document`。
4. 原子写权威文档：这是提交点。
5. 更新投影/selection/coverage。
6. best-effort 写 checkpoint；失败不能覆盖原始错误。

恢复时以权威文档为准，但必须区分代码修订与可变研究材料：

- 代码修订键（例如 PR 的 `head_sha + base_sha`）不变，且对应 section 完整、
  计数一致、全部 `text_ref` 的 blob 存在且 hash/bytes 匹配时，才允许复用
  绑定该修订的 section（PR 为 `files` / `commits`）。
- 合并状态、正文、评论、review 等可变材料没有可靠的单值版本键，必须重读。
  PR 的 `issue_comments` / `reviews` / `review_comments` 在 V0.1 每次选中都
  重读；不要只用 `updated_at` 判断可变材料未变。
- 重建文档后与旧文档比较（忽略 `observed_at` 和 `selection_reason`；选择
  理由以 selection 文档为准）：内容变化才覆盖写；未变化标记
  `skipped_unchanged`，不刷新快照时间，也不重写 entity 投影。Release 文档
  同样适用，忽略 `observed_at` 后比较。
- 旧文档完整、本次重建不完整时不得覆盖：PR 的 candidate 记
  `failed` / `refresh_incomplete`，Release 计数 `retained` 并把
  `refresh_incomplete:<tag>` 写进 `truncation_reason`，coverage 记
  `partial`，下次重试。限流、认证、5xx 等 fatal 错误按错误契约中断，但同样
  不覆盖旧文档。
- 本地 store、schema 或 validation 错误先标记当前 candidate `failed`，
  best-effort 写 partial selection/checkpoint，再原样抛出；不要吞掉或
  `continue`。
- Issue/Discussion 没有 head 语义，不得套用 PR 的 head 跳过规则；body、
  comments、timeline、replies、state 等可变材料每轮重读。关键词命中的
  `role_hints` 只进文档与 `normalized_state`，不得写入 `entity.roles`。
- Discussion 的 GraphQL partial data 必须保留已取到的节点并把 comments/detail
  标 `partial`；查询失败不得写成空数组成功。
- 投影缺失用 `reconcile_*` 或 `verify_integrity(repair=True)` 重建；丢失的
  权威 blob 不能由 repair 恢复。

## 7. 测试清单

- 一个 fake collector：只实现 1 个 section，覆盖 `complete` / `partial` / `unavailable`；
  同时覆盖 `context.progress.record` / `finish_channel` 写入 coverage 和 stage，且不产生额外 checkpoint 文件。
- 失败注入：section 404、分页中途失败、文档写后崩溃、checkpoint 写后崩溃。
- resume：删除派生视图后能从权威文档重建。
- 契约：section 名与 `status.py` 中的常量一致（PR 已有 `test_pr_section_specs_match_status_contract`）。
- 不改 `status.py` 就能表达三种基本状态；否则按第 4 节走契约评审。

## 8. 准入标准

- 新 collector 的编排样板 ≤150 行；解析函数可单独放模块。当前 fake collector 测试只是最低门槛。`collect_documents` 已拆成
  `collect_documents` + `_snapshot_for` + `_finalize`；`collect_proposals`
  拆成候选选择、`_read_issue` / `read_discussion`、`_finalize`，编排入口保持在
  准入线内。
- Release / docs 使用独立权威文档（`raw/releases/<id>.json`、
  `raw/docs/<revision>/<path>.json`）和 `releases` / `docs` coverage channel；
  Release 列表与 note 每次重读，固定 revision 文档才允许复用。详见
  [`../skills/repo-evolution-research/references/sources.md`](../skills/repo-evolution-research/references/sources.md)。
- Issue / Discussion 使用 `raw/issues/<number>.json`、
  `raw/discussions/<number>.json` 和 `issue_detail` / `discussion_detail`
  channel；候选决策写入 `analysis/proposal-selection.json`，角色关键词只作
  `role_hints`。
- 不新增 checkpoint 文件、coverage 聚合层或状态字符串。
- 状态可从 section 真值推导，且 partial 不会被写成 complete。
- 新增或修改 schema 必须 bump `SCHEMA_VERSION` 并更新 fixtures。

## 9. 契约清单

- REST list section 用 `pages.SectionSpec` 声明，抓取与构造统一走
  `pages.fetch_section`；不足一个完整 section 的场景才直接用
  `fetch_rest_list` / `build_section`。GraphQL 与多载体 RFC 需要单独 adapter，
  只复用 `CollectContext`、`text_ref` 和状态契约。
- 新 collector 接收 `base.CollectContext`，用 `context.now()` 取时间，不重复
  传递 client/store/repository/clock。
- 长文本使用 `texts.text_ref`；时间/数值/标签使用 `values.py`。
- 状态只调用 `status.py` 的 `derive_*`；聚合 coverage 只调用
  `progress.coverage_entry` 并通过 `progress.record` / `finish_channel` 写入。
- 一个采集事务只写一个权威文档；派生视图由投影函数生成。
- 不新增 checkpoint 文件，不新增 coverage 聚合层。
- 写盘前通过 `validate_document` 校验；跨产物一致性由 `verify_integrity` 复核。
