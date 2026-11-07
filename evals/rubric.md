# 研究报告评分规则

本规则用于人工标注与自动评测。自动指标只覆盖可机械核对的结构、状态和关系；技术深度与阅读价值由人工评分。评测对象是 `analysis/research.json` 及其派生的 `report.md`，不是运行耗时或请求数。

## 评分维度

| 维度 | 分值 | 评分方式 | 说明 |
| --- | --- | --- | --- |
| 技术深度 | 0–2 | 人工 | 0：只有标题/PR 摘要；1：解释动机与实现轮廓；2：说明设计取舍、替代方案和边界 |
| 推荐阅读价值 | 0–2 | 人工 | 0：链接与问题无关；1：覆盖主要材料；2：按问题组织且包含反例/背景 |
| 证据支撑精度 | 0–1 | 自动 + 人工复核 | 关键 claim 是否都有 evidence，quote 是否可定位；自动算比例，人工判断是否支撑结论 |
| 状态准确率 | 0–1 | 自动 | capability 四维状态与标注一致的比例 |
| 主题召回 | 0–1 | 自动 | 标注主题中被报告覆盖的比例 |
| 关系准确率 | 0–1 | 自动 | 标注关键关系的 precision / recall，分子分母必须同时报告 |

总分只用于同一标注版本内的横向比较，不把不同项目、不同窗口的分数直接比较。

## 自动指标

`evaluate.py` 输出以下指标，每项同时给出分子和分母：

- 结构覆盖：observed/inferred claim 带 evidence 的比例；unknown 同时有 `reason` 和 `missing_evidence` 的比例。
- 主题召回：先按 `title` / `title_aliases`（规范化后）精确匹配；未命中时用 `key_evidence_ids` 与报告 topic 引用 claim 的 evidence 做集合重叠匹配（重叠为 0 才判未命中）。两种匹配都是确定性的，不做语义相似度。仍未匹配的条目进入 `mismatches` 供人工裁定。
- 能力状态：先在已匹配 topic 内按 capability 的 `title` / `title_aliases` 匹配；匹配后逐维比较四个状态，输出正确维度数 / 已匹配维度数，另列 mismatch。
- 未知项：标注 unknown 按 topic 标题匹配；报告 unknown 的完整性单独统计。
- 关键关系：标注 `(from_id, to_id, type)` 与 `normalized/relations.jsonl` 精确匹配；precision 的分母只统计**标注涉及的 relation type**（`reported`），同时给出全量计数 `reported_all`，避免 `mentions` 等噪声关系把 precision 压到无意义；recall 分母为标注关系数。
- 预算与覆盖：manifest 的 `budget_consumed` / `budget` 比率，以及 coverage channel 的降级/阻塞数量。
- 门禁：`evaluate.py` 先校验 `analysis/validation.json` 存在、`ok=true`、`research_sha256` 与当前 research 一致、`run_id` 一致；任一不满足直接拒绝评分（退出 2），不产出分数。
- 分母为 0 的指标（例如没有标注关系）记为 `null`，不记满分。

自动评测不做关键词命中、embedding 相似度或 LLM 语义打分。标注中的自由文本只用于人工复核，不进入自动分数。窗口外事件是否被误写成历史截面，当前只作为人工零容忍项，不进入自动指标。

## 零容忍混淆

以下任一情况直接判该维度不合格，即使总分较高：

- 开放/未合并 PR 被写成已合并。
- Release note 提及或 `note_only` 被写成已发布；`merged` 被写成 `released`。
- Release 早于实现 PR 合并，却被当作交付证明。
- 推断被写成官方承诺，或 unknown 被写成 observed。
- 窗口结束后的状态被当作窗口内历史截面。
- 引用不存在、quote 与保存的 blob 不一致，或 evidence 与 claim 无关。
- 报告遗漏 `limitations`，或用“数据不足”掩盖未执行的采集。

## 标注与版本

每个 case 一个目录，包含 `annotation.json` 和可选的 `notes.md`。`annotation.json` 必须通过 `evals/cases/schema/annotation.schema.json` 校验，并记录：

- `source.annotation_version`：标注规则版本，规则变化必须递增。
- `source.annotated_by` / `annotated_at`。
- `source.default_branch_sha`：标注针对的代码修订；`snapshot` 可记录快照或 run_id。
- `window`：标注使用的窗口，必须与 run 的窗口一致。
- `title_aliases`（可选）：topic / capability 的别名，用于与报告标题对齐；`key_evidence_ids` 用于别名也不命中时的证据重叠匹配。

标注不得从 `research.json` 反向抄写。先独立阅读原始材料形成标注，再运行评测。假设报告会用不同措辞描述同一主题，因此用别名与 `key_evidence_ids` 建立可机械核对的对应关系，而不是要求两边标题一致。发现标注错误时递增 annotation_version，不静默改分。

## 人工评分流程

1. 先读 `report.md` 的速览、方向、功能进展表和方法与局限。
2. 对照 `research.json` 与 evidence 链接抽查关键 claim，不逐条重读全部材料。
3. 按维度打分并在 `evaluation.md` 的备注区写明理由；自动指标不能替代人工判断。
4. 对零容忍项逐条确认；命中即记录，不因其他维度高分而抵消。
