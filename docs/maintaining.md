# 维护与发布

## 项目定位

Repository Evolution Research 以 Agent Skill 为交付入口，Python CLI 为证据工具链。维护首页时优先说明：它回答什么问题、需要什么环境、如何让 Agent 使用、报告长什么样，以及结果的验证边界。

当前版本从源码运行；`pyproject.toml` 中的版本并不表示已有同名 PyPI 发布或 GitHub Release。新增包装层、Web UI 或服务部署应单独定义范围，避免安装说明超出实际能力。

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

## 版本与发布

首次正式发布前建议完成：

1. 从干净 clone 按 README 配置环境、加载 Skill，确认实际 Agent 使用正确的 Python。
2. 在可控的小窗口完成一次新的真实 `collect → prepare → Agent research → validate → render`，记录准确提交和环境，保留 coverage 与局限。
3. 由未参与报告写作的评审者核对部分主题与关键断言，明确评测样本和未覆盖范围。
4. 确认 CI 实际通过，首页链接、示例、许可证和第三方来源说明完整。
5. 确定正式版本，再更新 `pyproject.toml`、`rer/__init__.py` 及相关版本文档。写发布说明，说明新能力、兼容性和已知限制，然后创建 tag / GitHub Release。

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
| 后续 | 可重复的依赖环境 | 按支持平台验证约束或锁文件的维护方案 |
| 按需 | 多语言报告、跨窗口比较、完整 diff | 先实现与验证，再调整首页能力声明 |
