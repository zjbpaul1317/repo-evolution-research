<!-- template_version: 2 -->
# acme/demo 技术演进研究（2026-08-03 至 2026-11-01）

> run_id：`20261101T080000Z-aaaaaaaa` ｜ research：`e60d7a71a419f2427a9646ca6b482da4b6e31926a819d78047abb319ff8674fa` ｜ inputs：`2c37c4d9fce9adb3999a56a2c8ca52049fd4bdf5b8058efae8ba1a95471d8751` ｜ template：`2`
> 研究窗口：2026-08-03T08:00:00Z 至 2026-11-01T08:00:00Z（UTC）
> 观察时间：2026-11-01T08:00:00Z 至 2026-11-01T08:00:00Z ｜ 状态口径：`observed_current`
> research.created_at：2026-11-01T09:00:00Z

## 研究范围与结论速览

- 仓库：`acme/demo`（默认分支 `main` @ `bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`）
- 覆盖：16 个通道（complete 12、not_requested 4）
- 预算：requests 23/400、deep_entities 2/40、response_bytes 6569
- 结论速览：
  - **Cache lifecycle**：RFC 已提出，第一个实现 PR 已合并，发布尚未核验。

## 重点技术演进

### Cache lifecycle

**研究问题**：为什么以及如何引入显式缓存清理接口？

**问题与动机**

- RFC 提出增加显式缓存清理接口。[^C001]（事实，置信度：高）

**实现进展**

- PR 合并了清理接口骨架。[^C002]（事实，置信度：高）

## 功能进展表

| 子能力 | 提案 | 实现 | 发布（核验） | 可用性 | 分支@提交 | 适用版本 | 证据 |
|---|---|---|---|---|---|---|---|
| Explicit cache cleanup API（cleanup API skeleton） | 已提案（`proposed`） | 已合并（`merged`） | 未知（`unknown`） ｜ `unknown`（未核验） | 未知（`unknown`） | `main` @ `bbbbbbbbbbbb` | — | `ev-5ac39c2017588589`、`ev-1bda7470fee0b12a` |

## 未解决问题

本次结构化结果没有列出未解决问题。

## 推荐阅读

1. P0 [RFC: cache cleanup API](https://github.com/acme/demo/issues/40)（RFC）— 理解清理接口的动机。
2. P0 [Add cache cleanup API](https://github.com/acme/demo/pull/1)（PR）— 核对实现范围。

## 方法与局限

### 方法

- 研究窗口：2026-08-03T08:00:00Z 至 2026-11-01T08:00:00Z（UTC），状态口径 `observed_current`。
- 覆盖：16 个通道（complete 12、not_requested 4）。
- 预算消耗：requests 23/400、deep_entities 2/40、response_bytes 6569。
- 自动化校验：validation ok=true，errors 0，warnings 6。 自动校验只能证明引用与状态规则成立，不能证明语义解释正确。

### 局限

- budget: offline fixture uses a fixed budget.
- docs: only the design document was read; discussions were not requested.

### 校验警告（6 条）

- **lifecycle 缺口**（2 条）：按 capability 汇总
  - `cap-001`（Explicit cache cleanup API）：diff_not_requested、no_release_evidence
  - 说明：`diff_not_requested` 为 V0.1 统一限制，不单独降低生命周期状态。
- **coverage**（4 条）：区分降级、截断与未执行
  - `coverage:activity_discussions`：coverage channel activity_discussions 未执行（coverage_status=not_requested）；修复：说明主动未请求或未执行
  - `coverage:discussion_detail`：coverage channel discussion_detail 未执行（coverage_status=not_requested）；修复：说明主动未请求或未执行
  - `coverage:discussions_capability`：coverage channel discussions_capability 未执行（coverage_status=not_requested）；修复：说明主动未请求或未执行
  - `coverage:discussions_items`：coverage channel discussions_items 未执行（coverage_status=not_requested）；修复：说明主动未请求或未执行

<details>
<summary>展开证据附录：引文、定位与置信度</summary>

## 注释

[^C001]: `claim-001` 事实，置信度：高（来自 Issue 正文）。证据：[ev-5ac39c2017588589](https://github.com/acme/demo/issues/40)（Issue 正文，Issue #40，L1, chars 0-40，verification=verified）：“RFC: add an explicit cache cleanup API.”
[^C002]: `claim-002` 事实，置信度：高（来自 PR 正文）。证据：[ev-1bda7470fee0b12a](https://github.com/acme/demo/pull/1)（PR 正文，PR #1，L3, chars 80-147，verification=verified）：“It adds a cleanup entry point and keeps eviction policy unchanged.”

</details>
