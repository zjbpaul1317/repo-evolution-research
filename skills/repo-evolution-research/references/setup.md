# 环境准备与运行入口

本文件只处理「装了 Skill 但跑不起来」这一类问题。研究流程、证据规则和报告契约见
[`workflow.md`](workflow.md) 等其他参考文件。

## 入口

Skill 目录下的 `rer` 是唯一入口。它保持调用者的工作目录，解析自身路径，把参数、
stdout、stderr 和退出码原样交给 CLI。

```bash
SKILL_DIR=/absolute/path/to/repo-evolution-research
bash "$SKILL_DIR/rer" doctor --repo owner/repo
```

不要 `cd` 进 Skill 目录再运行：`--output-root` 默认 `.repo-research` 相对当前目录，
进去跑会把快照写进安装目录，重装或升级后丢失。

## 前置条件

| 条件 | 说明 |
| --- | --- |
| `uv >= 0.9` | 负责解释器选择、依赖解析、环境缓存和并发安装 |
| `gh` 已认证 | `gh auth login --hostname github.com --web`，CI 用 `GH_TOKEN` |
| macOS / Linux / WSL | 存储锁依赖 `fcntl`，Windows 原生环境不受支持 |

`rer` 会先检查 uv 是否存在及版本，再检查环境能否准备。任一失败都以 exit 3 结束，
并给出 `recovery` 提示；这属于环境错误，不是研究结果。

## 首次运行做了什么

`scripts/research.py` 顶部声明了 PEP 723 内联元数据（Python 版本与依赖），
相邻的 `scripts/research.py.lock` 固定直接依赖和传递依赖。`rer` 执行：

```bash
uv run --locked --no-build --script scripts/research.py ...
```

uv 在自身缓存中创建隔离环境，不写入 Skill 目录，也不读取调用者项目的依赖。
`--locked` 要求锁文件与内联元数据一致；`--no-build` 拒绝现场源码构建，避免在缺少
wheel 的平台上意外触发本地编译。

## 离线与企业环境

两种下载来源要分别处理：Python 解释器，以及 Python 包。

```bash
# 只使用已安装的解释器，禁止 uv 自动下载 Python
export UV_PYTHON_DOWNLOADS=never

# 预热缓存后完全离线运行
export UV_OFFLINE=1
```

离线要求先在可联网环境完整跑过一次 `doctor`，让 uv 把解释器、wheel 和环境写入缓存。
缓存预热后，`UV_OFFLINE=1` 下缺少缓存会立即报错，而不是静默安装。

仓库提交的 `research.py.lock` 记录的是 PyPI 源。企业镜像优先选择对 PyPI 透明的缓存代理，
这样锁文件里的 URL 仍然有效；如果镜像使用不同的索引地址，需要单独重新锁定并评估是否提交，
不要把个人环境的锁文件混入发布版本。`UV_OFFLINE` 只约束 uv；CLI 自身对 GitHub 的请求仍按
原有预算与退避规则执行。

```bash
# 仅在需要针对不同索引重新锁定时使用
cd "$SKILL_DIR/scripts"
UV_DEFAULT_INDEX=https://your-mirror.example/simple uv lock --script research.py
```

## 认证

- 桌面：用户在自己的终端执行一次 `gh auth login --hostname github.com --web`。
- CI / 无界面：由外部注入 `GH_TOKEN`，不要写进配置、快照或对话。
- `doctor` 负责报告认证与仓库访问状态。`prepare`、`validate`、`render`、`audit`
  等本地子命令不需要 GitHub 认证。

## 逃生通道：RER_PYTHON

已经有受管 Python 环境的环境可以绕过 uv：

```bash
RER_PYTHON=/opt/python/bin/python3 bash "$SKILL_DIR/rer" doctor --repo owner/repo
```

该路径**不校验锁文件**，`rer` 会在 stderr 声明这一点。它用于无 uv 的受控环境，
不应用于日常运行；出现问题时先确认 `jsonschema` 版本满足 `>=4.23,<5`。

## 维护锁文件

改动 `research.py` 的内联依赖后必须重新生成锁：

```bash
cd "$SKILL_DIR/scripts"
uv lock --script research.py
```

CI 用只读方式校验是否漏提交：

```bash
uv lock --script scripts/research.py --check
```

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `需要 uv >= 0.9` | 安装或升级 uv；确认 `uv --version` 在 PATH 中 |
| `uv 无法准备运行环境` | 先看上一行 uv 原始错误：锁不一致 / 无网络 / 无 wheel |
| `environment_error: 需要 Python >= 3.11` | 绕过 uv 时用了旧解释器；改用 `rer`，或安装 3.11+ |
| `environment_error: 当前平台 ... 不受支持` | 改用 macOS、Linux 或 WSL |
| `gh_auth` 失败 | 桌面执行 `gh auth login`；CI 注入 `GH_TOKEN` |

不要为了让错误消失而修改 Skill 声明的依赖，也不要把包装好的依赖复制进
Skill 目录：那会让不同平台上的环境不可复现。
