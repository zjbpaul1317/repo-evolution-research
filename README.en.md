# Repository Evolution Research

**Understand how a GitHub project evolved, with evidence you can inspect.**

[简体中文](README.md) · [Workflow reference](skills/repo-evolution-research/references/workflow.md) · [Contributing](CONTRIBUTING.md) · [MIT License](LICENSE)

A research Skill for coding agents, backed by a local Python CLI. It collects PRs, issues, RFCs, discussions, releases, and documentation, saves an evidence ledger, and helps an agent explain technical direction, design decisions, implementation progress, and unresolved questions.

Use it when joining an unfamiliar project, preparing a contribution, or investigating a project's recent technical evolution.

> Tool version: `0.2.8`. Snapshot schema: `1.3`. Early development. Reports currently use Chinese. Run the CLI from source; a standalone pip package is not provided.

## What it helps answer

- What technical problems have maintainers focused on over the last three months?
- Why was an RFC proposed, and which parts have implementations?
- How have architecture, scheduling, caching, or inference paths changed?
- Which changes are merged, which have release evidence, and what remains unverified?
- Which primary sources should a new contributor read, and in what order?

Example request in an agent that has loaded the Skill:

```text
Use $repo-evolution-research to study vllm-project/vllm over the last 90 days.
Focus on scheduling and KV cache. Explain motivations, implementation progress,
unresolved questions, and recommended reading. Distinguish merged changes from
verified releases, cite primary sources, and produce the report in Chinese.
```

## How it works

The CLI handles collection, storage, normalization, validation, and rendering. The agent reads evidence, chooses follow-up material, identifies themes, and writes structured research. The CLI itself does not call a language model.

```text
GitHub sources → collect → prepare → evidence ledger and reading packs
                                      ↓
                                agent research
                                      ↓
                                research.json
                                      ↓
                              validate → render
                                      ↓
                                  report.md
```

An agent can request another reading round through `analysis/selection.json`. Claims retain source URLs, text hashes, and quote locations. Proposal, implementation, delivery, and availability are tracked separately. Coverage gaps and unknowns stay visible in the report.

**Validation checks references and state rules; it does not establish that an interpretation is semantically correct.** Human review remains necessary for consequential conclusions.

## Quick start

### Requirements

- Python **3.11+**.
- **macOS or Linux**. Storage locking uses `fcntl`; native Windows is unsupported. Use WSL on Windows.
- [GitHub CLI `gh`](https://cli.github.com/), authenticated with access to the target repository.
- An agent that can read local files and run commands to write the research, or a person following the JSON schemas.

```bash
git clone https://github.com/zjbpaul1317/repo-evolution-research.git
cd repo-evolution-research
python3 -m venv .venv
source .venv/bin/activate
python -m pip install 'jsonschema>=4.23,<5'
gh auth login
python skills/repo-evolution-research/scripts/research.py doctor --repo vllm-project/vllm
```

Read every `doctor` check. Exit code `3` means a required environment, authentication, or access check failed.

### Load the Skill

The complete Skill lives in [`skills/repo-evolution-research/`](skills/repo-evolution-research/), with [`SKILL.md`](skills/repo-evolution-research/SKILL.md) as its entry point. Load the whole directory in a host that supports local Skills, including its scripts, assets, references, and agent metadata.

In Codex, you can ask the built-in installer:

```text
Use $skill-installer to install the Skill at skills/repo-evolution-research
from https://github.com/zjbpaul1317/repo-evolution-research.
```

Discovery locations and reload behavior depend on the host; see the [official Codex Skill documentation](https://developers.openai.com/codex/skills/). Install dependencies in the Python environment the agent actually uses. To use this checkout's virtual environment, ask the agent to invoke `.venv/bin/python`.

### Understand the CLI flow

Run these commands from the repository root. Start with a small window:

```bash
python skills/repo-evolution-research/scripts/research.py init \
  --repo vllm-project/vllm --days 30 --mode quick \
  --timezone Asia/Shanghai --focus scheduling --focus 'KV cache'
```

Copy the actual path from `result.run` in the JSON output:

```bash
RUN='.repo-research/github.com/vllm-project/vllm/<run_id>'
python skills/repo-evolution-research/scripts/research.py collect --run "$RUN"
python skills/repo-evolution-research/scripts/research.py prepare --run "$RUN"
```

The agent must now read `analysis/packs/*.md` and the candidate material. If needed, it writes `analysis/selection.json` using the [selection protocol](skills/repo-evolution-research/references/workflow.md#selection-协议), then runs:

```bash
python skills/repo-evolution-research/scripts/research.py collect \
  --run "$RUN" --selection "$RUN/analysis/selection.json"
python skills/repo-evolution-research/scripts/research.py prepare --run "$RUN"
```

Once the agent has written `analysis/research.json`, run:

```bash
python skills/repo-evolution-research/scripts/research.py validate --run "$RUN"
python skills/repo-evolution-research/scripts/research.py render --run "$RUN"
```

The result is `$RUN/report.md`. Supporting files include `analysis/validation.json`, `analysis/evidence-review.md`, and `analysis/render-report.json`. Use `audit --run "$RUN"` for a read-only snapshot integrity diagnosis.

| Mode | Default window | Default deep-read entity budget |
| --- | --- | --- |
| `quick` | 30 days | 12 |
| `standard` | 90 days | 40 |
| `deep` | 180 days | 80 |

Explicit dates, durations, and budgets override mode defaults. See [configuration](skills/repo-evolution-research/references/configuration.md). Budgets do not guarantee whole-repository coverage.

## Examples and offline checks

Read the [sample report](tests/fixtures/loop/report.golden.md) for themes, capability states, evidence footnotes, reading order, and limitations. It uses a fictional `acme/demo` repository and fake GitHub responses; its links are illustrative.

Run the deterministic workflow test without GitHub credentials:

```bash
python -m pip install 'pytest>=8'
python -m pytest tests/test_offline_loop.py -q
```

It exercises collection, preparation, authored agent data, validation, rendering, and golden report comparison with a fake GitHub transport.

The [vLLM KV correctness case](evals/cases/vllm-20261104-kv-correctness/notes.md) includes real source excerpts and offline checks. A full snapshot replay requires the original run, which is not distributed. The case covers four PRs; its annotation and baseline were produced in the same agent session. It is not an independent human blind review or a whole-repository quality benchmark.

## Current limits

- Reports are fixed to Chinese. `--language` and `--compare-previous` are reserved; multilingual reports and window comparisons are not implemented.
- File lists and patch excerpts may be truncated. Full diffs are not collected. The tool does not execute target-project tests or independently reproduce deployment or benchmark claims.
- Merged changes, release evidence, and availability are separate. Release-note mentions do not automatically prove delivery.
- Permissions, pagination, rate limits, and budgets can leave partial coverage. Inspect exit code `4` and the recorded gaps before continuing.
- Validation or rendering failures use exit code `5`. Empty-evidence snapshots cannot produce a formal report.
- Snapshot compatibility is limited. Preserve original runs, use `audit`, and follow the [copy-based recovery workflow](skills/repo-evolution-research/references/workflow.md#只读升级诊断与副本恢复).

## Repository layout

```text
skills/repo-evolution-research/
  SKILL.md           Agent instructions and research boundaries
  scripts/           Python CLI, collectors, evidence, and reporting
  assets/            JSON schemas, configuration, report template
  references/        Workflow, sources, state rules, evaluation
tests/               Offline regression tests, fixtures, golden reports
evals/               Evaluator, annotation schema, public cases
docs/                Contributor and maintainer documentation
scripts/             Asset generation and lint tools
```

Research snapshots (`.repo-research/`), generated evaluation results (`evals/results/`), and local development archives (`.local/`) are ignored by Git. Check your own ignore rules when using a custom `--output-root`.

## Development

```bash
python -m pip install 'jsonschema>=4.23,<5' 'pytest>=8' 'ruff>=0.6' 'PyYAML>=6'
python -m pytest -q
python scripts/gen_assets.py --check
PYTHON=python bash scripts/lint.sh
```

The CI workflow runs these offline checks on Linux and macOS with Python 3.11 and 3.13, without repository secrets or live GitHub API calls. A configured matrix is separate from an observed successful CI run.

See [contributing](CONTRIBUTING.md), [collector contracts](docs/collector-authoring.md), [security](SECURITY.md), and the [maintainer guide](docs/maintaining.md). Some detailed references and diagnostic messages currently use Chinese.

## License

Original project code and documentation are available under the [MIT License](LICENSE). Third-party source excerpts retain their original rights; see [third-party notices](THIRD_PARTY_NOTICES.md).
