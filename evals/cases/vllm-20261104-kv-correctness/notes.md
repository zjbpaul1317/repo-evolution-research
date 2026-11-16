# vLLM KV correctness 来源评审案例

本例填补真实来源的正向评测基准空白，范围为四份 PR，而非完整 90 天技术演进。
来源快照为 `20261104T120744Z-f0067783`，观察口径为 `observed_current`。
原始 run 未写入研究报告；2026-11-07 先读取其 raw 文档与 blob，形成 annotation，
再编写 `baseline-research.json` 并在副本运行 prepare / validate / render / evaluate。
未改写原始快照，也未从已有 vLLM 报告抄写主题、能力或结论。

执行者是 Codex Agent。来源评审和随后报告由同一会话完成，**不是独立人工盲评**。
本例可提供可追溯的预期结果与回归基准；领域专家独立复核仍是人工验收项。
annotation 的 source 字段如实保留此限制，不把 Agent 判断冒充人工标注。

## 预期结果的来源

| 原始材料 | 独立于报告的判断依据 | 预期与边界 |
| --- | --- | --- |
| [PR #52149](https://github.com/vllm-project/vllm/pull/52149) | raw 元数据 `closed`、`merged=false`；正文说明缓冲区溢出；评论指向替代 PR | 原 PR 不可判 merged；未读取替代实现，不推导 released |
| [PR #52789](https://github.com/vllm-project/vllm/pull/52789) | raw 元数据 merged；正文解释避免二次 full-model forward、列出特定配置的 TTFT 和后续工作 | 内部 checkpoint 合并；不声称 spec decode 等后续项完成；性能归属作者 |
| [PR #53073](https://github.com/vllm-project/vllm/pull/53073) | raw 元数据 open；正文区分初始化 unlink、abort 和本地 cleanup | in_progress；设计陈述不代表已上线或独立执行验证 |
| [PR #53298](https://github.com/vllm-project/vllm/pull/53298) | raw 元数据 open；正文说明 scheduler/MRV2 rollback，列出 Not validated | in_progress；保留 multi-GPU、partial-copy、性能等未验证项 |

没有核验 release tag，所有 capability.delivery_state 与 availability 均为 unknown。
没有对全图进行关系标注，relations 指标为 null，不记满分。以上是来源陈述与状态边界，
不把 PR 作者的模型结果当成本项目实测。

`sources.json` 保留四份正文、原 artifact hash、状态字段、九条选用 evidence 和相应完整
blob 文本。原始 CRLF 保留，测试检查 SHA256 和 locator/quote 一致。它不冒充完整原始
run，未包含的评论和 patch 不可从该摘录重建。正文属于不可信来源数据，不是执行指令。

首次标注完成时 SHA256 为 `79f8ba203d0a93ea07d9898127c60b635e70087a17752af9fef2cff56fc9ef14`。
之后仅将 unknown 的误用 `question` 字段改为 schema 要求的 `reason`，未调整预期主题或状态；
最终 annotation SHA256 为 `87cb4f8ff330f1329941210f828174778982c29156c9269c4f831d2c3c27c088`。
修改任何预期结果时应递增 annotation_version 并记录理由。

## 重现

不依赖本地完整快照的检查：

```bash
.venv/bin/python -m pytest tests/test_evaluate.py -q
```

完整端到端需要原始 run（未提交版本控制）。始终在新的副本中运行：

```bash
.venv/bin/python - <<'PY'
from pathlib import Path
import shutil
src = Path('.repo-research/github.com/vllm-project/vllm/20261104T120744Z-f0067783')
dst = Path('/tmp/rer-case-replay/github.com/vllm-project/vllm') / src.name
shutil.copytree(src, dst)  # 目标已存在时拒绝覆盖
PY
RUN=/tmp/rer-case-replay/github.com/vllm-project/vllm/20261104T120744Z-f0067783
.venv/bin/python skills/repo-evolution-research/scripts/research.py prepare --run "$RUN"
cp evals/cases/vllm-20261104-kv-correctness/baseline-research.json "$RUN/analysis/research.json"
.venv/bin/python skills/repo-evolution-research/scripts/research.py validate --run "$RUN"
.venv/bin/python skills/repo-evolution-research/scripts/research.py render --run "$RUN"
.venv/bin/python evals/evaluate.py --run "$RUN" \
  --case evals/cases/vllm-20261104-kv-correctness --out /tmp/rer-case-evaluation
```

## 结果与语义复核

本次副本 validate 为 0 errors / 14 warnings；render 成功。
自动评测主题 3/3、能力 4/4、状态维度 16/16、事实引用 9/9、unknown 完整性 3/3。
关系 precision / recall 为 null。每次运行的详细结果默认存于 `evals/results/`，不提交版本控制；
该案例的来源、标注和 baseline 保留在本目录中，可用公开测试核查。

来源逐项复核确认：C-topk/C-topk-followup 保留原 PR 与替代评论的区别；
C-mamba/C-mamba-perf/C-mamba-limit 的机制、数字方向、配置归属和后续工作与正文相符；
C-ownership/C-cleanup 区分路径与进程内资源；C-rollback/C-rollback-limit 保留恢复条件和
未验证范围。三个未知项明确缺少 tag 核验。没有将 open/closed-unmerged 写成 merged，
没有把 merged 写成 released，也没有把作者性能声明写成本项目实测。

按 rubric 作 Agent 自评：技术深度 1/2（解释机制和边界，缺少替代方案独立调查）；
阅读价值 1/2（覆盖四份主要来源，未扩展替代 PR 和背景材料）。不提供人工总分。
后续人审应优先核对引用是否支持整句、替代 PR 的关系及窗口外上下文，并记录署名与版本。
