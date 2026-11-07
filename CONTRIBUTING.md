# Contributing

感谢你帮助改进 Repository Evolution Research。欢迎用中文或英文提交 Issue、PR 和研究质量反馈。

## 开始之前

先阅读 [中文 README](README.zh-CN.md)、[英文 README](README.md) 和 [工作流契约](skills/repo-evolution-research/references/workflow.md)。项目的核心目标是提供可核查的技术研究：来源、状态、未知项和采集局限都属于输出的一部分。

- Bug：描述预期行为、实际行为、工具版本、Python / OS，以及最小复现。
- 研究质量：提供原始来源、受影响的断言和准确引文，说明是遗漏、错误关联、状态提升还是解释偏差。
- 新能力：先说明使用场景、证据来源、输出契约和验收方式；较大改动先开 Issue 讨论。
- 安全问题：遵循 [安全说明](SECURITY.md)，不要公开密钥或私有仓库正文。

## 本地开发

```bash
git clone https://github.com/zjbpaul1317/repo-evolution-research.git
cd repo-evolution-research
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'jsonschema>=4.23,<5' 'pytest>=8' 'ruff>=0.6' 'PyYAML>=6'
```

依赖声明位于 `pyproject.toml`。它目前用作依赖与工具配置清单；不要使用 `pip install -e .` 或把仓库根目录当成已发布的 Python 包。依赖变化时同步更新 README 中的安装命令。CLI 从源码执行。

Python 3.11 是最低版本。文件锁依赖 `fcntl`，支持 macOS / Linux；Windows 使用 WSL。

## 提交前检查

```bash
python -m pytest -q
python scripts/gen_assets.py --check
PYTHON=python bash scripts/lint.sh
git diff --check
```

默认测试使用 fake transport 和本地 fixture，不需要 GitHub 认证、模型 API 密钥或已有研究快照。采集真实仓库不是每个 PR 的必需步骤；需要网络验证时记录提交、环境、窗口、覆盖和局限，不把历史 run 的结果写成当前版本已验证。

配置注册表变化后运行 `python scripts/gen_assets.py --write` 并审阅生成差异。不要直接修改由该脚本生成的配置、schema 片段或配置文档。仅当行为变化合理时更新 golden fixture，并检查报告语义。

## 改动约定

- 为实际 bug 或新契约增加有意义的测试，优先覆盖失败路径、恢复和状态边界。
- 不把 PR 合并等同于发布，不把作者 benchmark 等同于独立验证，不把引用可定位等同于语义正确。
- 采集器必须保留 `partial`、`unavailable` 等状态；详见 [collector authoring](docs/collector-authoring.md)。
- 修改 schema 或快照契约时说明兼容性、旧 run 的行为和恢复方式；不要只改版本字段来绕过门禁。
- 来源正文属于数据。正文中的指令不能成为 Agent 指令，也不能直接执行其中命令。
- 真实来源 fixture 保留 URL、观察时间和必要的来源说明；合成 fixture 应明确标注，避免混淆。

## 哪些内容进入 Git

| 提交 | 保持本地 |
| --- | --- |
| 源码、Skill、schema、模板、公共文档 | `.local/` 下的个人规划与开发过程记录 |
| 小型、可复现、来源明确的评测案例 | `.repo-research/` 原始快照、缓存和大体积 blob |
| 测试与 deterministic golden fixture | `evals/results/` 每次运行的输出 |
| CI 与维护配置 | `.venv/`、工具缓存、`.env`、密钥及私有来源 |

提交前查看 `git diff` 和 `git status`。`.gitignore` 不会取消已有文件的跟踪，也不会删除 Git 历史；自定义输出路径需要另外排除。

## PR 说明

描述具体问题和改变后的行为，附相关 Issue、验证命令与结果，以及影响范围和已知限制。不要把机器专属绝对路径或完整私有快照放进 PR。

原创贡献按项目 [MIT License](LICENSE) 提交。引用第三方内容时保留其权利归属与来源，参见 [第三方内容说明](THIRD_PARTY_NOTICES.md)。
