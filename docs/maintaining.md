# 维护与发布

## 项目定位

Repository Evolution Research 以 Agent Skill 为交付入口，Python CLI 为证据工具链。维护首页时优先说明：它回答什么问题、需要什么环境、如何让 Agent 使用、报告长什么样，以及结果的验证边界。

当前版本通过 Skill 目录下的 `rer` 启动器运行，依赖由 PEP 723 元数据与 `scripts/research.py.lock` 锁定；`pyproject.toml` 中的版本并不表示已有同名 PyPI 发布或 GitHub Release。新增包装层、Web UI 或服务部署应单独定义范围，避免安装说明超出实际能力。

## GitHub 页面配置

建议 About description：

```text
An agent Skill and Python CLI for evidence-linked research into GitHub repository evolution.
```

建议 Topics：`agent-skills`、`codex`、`github`、`repository-analysis`、`research`、`evidence`、`python`。

README 已作为项目首页，无需为介绍项目额外部署 GitHub Pages。需要独立站点时，先确定持续维护的内容与访问需求。仓库 Settings 中可启用 private vulnerability reporting，并检查 Actions、Issues 和分支保护配置。

这些设置与 Git 文件不同，需要在 GitHub 上单独配置。不要在未确认设置生效时承诺已提供私密安全报告入口或强制检查。

## 合并与检查

[`CI`](../.github/workflows/ci.yml) 在 Linux / macOS 与 Python 3.11 / 3.13 的组合上执行离线测试、资产一致性和 lint。工作流定义不等于实际通过；首次推送后检查各项结果，再配置 required status checks。工作流采用只读仓库权限，不读取项目密钥，不运行真实目标仓库采集。

涉及外部来源、快照、引文和状态规则的变更，审阅时核对数据兼容性和语义边界。独立领域评审仍是研究质量验收的一部分，不能用测试数量替代。

## 本地性能对比

`scripts/benchmark_hashing.py` 比较原先的整文件读取哈希与当前的流式 SHA-256；合成文件放在临时目录，退出后自动删除，不读取已有研究快照，也不调用 GitHub。

```bash
python scripts/benchmark_hashing.py --size-mib 32 --repeats 5
```

输出包含平台、Python 版本、文件大小、重复次数、摘要一致性、耗时和 Python 分配峰值的中位数。方法交替运行，并预热文件缓存。`tracemalloc` 的分配峰值不等于进程 RSS，也不包含系统文件缓存。

2026-11-13，macOS arm64 / Python 3.13.3，5 轮本地结果：

| 文件大小 | 整文件读取峰值 | 流式读取峰值 | 整文件耗时中位数 | 流式耗时中位数 |
| --- | --- | --- | --- | --- |
| 8 MiB | 8,393,618 B | 267,945 B | 4.16 ms | 3.74 ms |
| 32 MiB | 33,559,442 B | 267,945 B | 17.20 ms | 15.52 ms |

32 MiB 案例的 Python 分配峰值降低约 99.2%。流式哈希用于 ledger/research 摘要、报告产物审计、已有 blob 去重核对和 `verify_blob`；需要解析正文或定位引文的路径仍读取实际内容。摘要算法和 ledger 排序契约保持一致，不能把上述局部结果解释为真实采集、prepare 或报告生成的端到端提速。

JSONL 写入可单独运行 `python scripts/benchmark_jsonl.py --records 10000 --repeats 5`。
它对比旧的整文件编码和当前逐条编码，包含 `write_jsonl` 的去重、原子替换、fsync 与索引重建，
使用临时合成快照、交替执行并排除首次预热，核对输出 SHA-256。
输入记录的创建不计入分配峰值；记录对象和去重索引仍随记录数增长。
2026-11-13 本机 Python 3.13.3 的 10,000 条记录（约 10 MiB）五轮中位数显示，
Python 分配峰值约降低 49.7%，耗时 0.355 秒与 0.372 秒；这是内存优化，不是端到端提速结论。

阅读包预算裁剪可与自行保存的旧源码比较（baseline 会作为 Python 模块导入，使用可信仓库源码）。公开仓库已合并为单次初始提交，不包含历史 baseline；先准备旧版 `packs.py`，再将下面的路径替换为实际路径：

```bash
python scripts/benchmark_packs.py --baseline-file /path/to/trusted/baseline/packs.py --repeats 5
```

默认单组 1,000 个合成候选、8,000 字符预算，排除输入构造和首次预热，交替运行两种实现。
脚本逐轮比较完整裁剪结果，结果不一致立即失败；中位数仅代表裁剪步骤。
2026-11-13 本机 Python 3.13.3 五轮结果：原实现 4.427 秒，二分裁剪 0.0397 秒，
均淘汰 987 个候选，输出一致。此案例刻意覆盖大量候选集中在同一组且预算紧张的场景；
不能把约 112 倍局部加速推广到完整 prepare 或真实采集。

## 运行入口与依赖锁

`rer` 是唯一对外入口，`scripts/research.py` 是它执行的脚本。运行时依赖只在这两处约定：

- `scripts/research.py` 顶部 PEP 723 内联元数据声明 `requires-python` 与 `dependencies`；
- `scripts/research.py.lock` 由 `uv lock --script scripts/research.py` 生成并提交。

`rer` 以 `uv run --locked --no-build --script` 执行，因此锁文件缺失或过期会立即失败，而不是静默解析新版本。`--no-build` 拒绝现场源码构建，避免在缺少 wheel 的平台触发本地编译。改动依赖时同时更新 `pyproject.toml` 与内联元数据，再重新生成锁；`tests/test_bootstrap.py` 断言两处一致，CI 的 `lockfile` job 校验锁是否最新。

启动器保持调用者工作目录，不读取调用方项目的依赖，也不在 Skill 目录内写环境。安装边界由 `tests/test_install_boundary.py` 覆盖：只复制 Skill 目录、把安装目录设为只读、项目路径含空格与中文，然后运行 `doctor`、`init`、`audit`，并比对安装目录在运行前后完全一致；uv 缺失时要求给出可操作错误。默认用 `RER_PYTHON` 跑，设置 `RER_TEST_REAL_UV=1` 时走真实 uv。CI 的 `install-boundary` job 使用真实 uv 执行一次，并校验锁文件最新。排障与离线/企业配置见 [`references/setup.md`](../skills/repo-evolution-research/references/setup.md)。

## 版本与发布

首次正式发布前建议完成：

1. 从干净 clone 按 README 配置环境、加载 Skill，确认实际 Agent 使用正确的 Python。
2. 在可控的小窗口完成一次新的真实 `collect → prepare → Agent research → validate → render`，记录准确提交和环境，保留 coverage 与局限。
3. 由未参与报告写作的评审者核对部分主题与关键断言，明确评测样本和未覆盖范围。
4. 确认 CI 实际通过，首页链接、示例、许可证和第三方来源说明完整。
5. 确定正式版本。`rer/__init__.py` 的 `TOOL_VERSION` 是运行时唯一来源；`pyproject.toml`、README（中英）与 `references/workflow.md` 必须同步，`tests/test_version_sources.py` 会拒绝不一致。写发布说明（当前维护在 [`CHANGELOG.md`](../CHANGELOG.md)），说明新能力、兼容性和已知限制，然后创建 tag / GitHub Release。

尚未完成的检查应如实保留，不应把历史快照重放或有限来源案例写成完整项目验收。发布 Skill 压缩包时保留完整 Skill 目录，并附项目许可证和适用的第三方说明。

## 本地档案与仓库历史

个人设计计划、草稿、修复过程和运行验收记录保存在被忽略的 `.local/`。原始快照保持 `.repo-research/` 结构，评测器默认继续写入被忽略的 `evals/results/`。测试、公共评测案例和长期使用契约保留在版本控制中。

从当前目录移走一个已提交文件，会在下一次提交中删除它；历史提交仍然包含旧内容。如果发现历史中有真正的凭据，先撤销凭据，再单独规划历史清理和协作者同步。普通规划材料不需要为了目录整洁而重写历史。

## 后续优先级

| 优先级 | 建议 | 验收方式 |
| --- | --- | --- |
| 近期 | 干净环境安装与宿主加载检查 | 按 README 独立复现，记录宿主与提交 |
| 近期 | 真实小窗口公开示例 | 来源可追溯、覆盖与局限明确、报告可读 |
| 近期 | 扩充独立质量评审 | 标注者与报告作者分离，保留争议和样本边界 |
| 后续 | 快照兼容与恢复体验 | 旧 run 诊断准确，副本恢复不破坏原件 |
| 已完成 | 可重复的依赖环境 | `uv lock --script` + `--locked` + CI 锁校验；启动器与旧解释器错误路径已有测试 |
| 按需 | 多语言报告、跨窗口比较、完整 diff | 先实现与验证，再调整首页能力声明 |
