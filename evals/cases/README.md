# Evaluation Cases

每个 case 一个目录：

```text
evals/cases/<case>/
├── annotation.json
└── notes.md          # 可选：人工标注理由、争议和复核记录
```

`annotation.json` 必须通过 [`schema/annotation.schema.json`](schema/annotation.schema.json) 校验。字段含义和评分规则见 [`../rubric.md`](../rubric.md)。

约束：

- 标注必须独立于 `research.json`，不得从待评测 run 反向抄写。
- `window` 和 `source.default_branch_sha` 必须与被评测 run 一致，否则 `evaluate.py` 拒绝评分。
- run 必须已通过 `validate`：`validation.json` 存在、`ok=true`、`research_sha256` 与当前 research 一致，否则拒绝评分。
- 主题/能力用 `title_aliases` 对齐报告措辞；仍未命中时用 `key_evidence_ids` 做证据重叠匹配，剩余差异进入 `mismatches` 供人工裁定。
- 标注内容变化时递增 `source.annotation_version`。
- 标注记录执行者与方法。Agent 从原始来源建立的预期结果必须明确标为 Agent 评审，
  不能声称已完成独立人工盲评；人工裁定后递增 annotation_version。

现有真实案例：[`vllm-20261104-kv-correctness`](vllm-20261104-kv-correctness/notes.md)。
它在无研究报告的真实快照上先读原始 PR、再固化标注，覆盖 closed-unmerged、merged、
open 三种状态、作者基准边界与三组主题。附可离线检查的来源正文、证据和后续编写的
baseline。主题召回只适用于这四份材料，不能作为完整仓库研究分数。
evaluator 会把 annotation.notes 同时带入 JSON 与 Markdown，保留范围和评审限制。

运行：

```bash
python evals/evaluate.py --run <RUN> --case evals/cases/<case>
```

结果默认写入 `evals/results/`，不写入 run 目录。
