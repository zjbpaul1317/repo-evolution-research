"""Command line interface for repo-evolution-research."""

from __future__ import annotations

import argparse
import copy
import functools
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import SCHEMA_VERSION, TOOL_VERSION
from .audit import audit_snapshot
from .collectors.activity import collect_activity
from .collectors.base import CollectContext
from .collectors.docs import collect_documents
from .collectors.progress import CollectProgress, has_blocking_gaps, merge_coverage
from .collectors.proposals import collect_proposals
from .collectors.pull_requests import collect_pull_requests
from .collectors.releases import collect_releases
from .collectors.repository import collect_repository
from .collectors.status import derive_stage_status
from .config import MODES, build_config, default_config, parse_repository, read_resolved_config
from .errors import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_PARTIAL,
    ArtifactError,
    BudgetExceededError,
    EnvironmentDegraded,
    InternalError,
    RERError,
    UsageError,
    ValidationFailure,
)
from .github import GitHubClient, probe_repository, redact_secrets
from .import_source import import_source, load_envelope
from .models import build_manifest, format_checker_status, to_iso_z
from .normalize import prepare_run
from .packs import PACK_INDEX_PATH
from .ranking import CANDIDATES_PATH
from .render import REPORT_PATH, discard_incomplete, discard_report, render_run, write_incomplete
from .selection_protocol import (
    REPORT_PATH as SELECTION_REPORT_PATH,
)
from .selection_protocol import (
    collect_selection,
    load_agent_selection,
)
from .store import SnapshotStore, create_run, new_run_id
from .validation import (
    EVIDENCE_REVIEW_PATH,
    RESEARCH_PATH,
    VALIDATION_PATH,
    validate_run,
    write_validation,
)

_DOWNSTREAM_STAGES = ("prepare", "research", "validate", "render")
_BUDGET_COUNTERS = (
    "requests",
    "response_bytes",
    "total_bytes",
    "deep_entities",
    "runtime_seconds",
)

_SUBCOMMAND_HELP = {
    "doctor": "检查 Python、jsonschema、gh 认证、git 与可选仓库访问",
    "init": "解析研究参数，创建 run 目录、resolved config 和 manifest",
    "collect": "采集仓库元数据、目录树、文档/RFC 位置、协作能力与窗口活动",
    "prepare": "离线归一化文本、生成 evidence/relations 与提交实体",
    "audit": "只读审计快照，区分旧版派生索引重建与完整性问题",
    "import-source": "导入外部来源 envelope 与人工确认关系",
    "validate": "校验 research.json 的结构、引用、证据、状态与覆盖门禁",
    "render": "只渲染通过当前输入校验的 research.json，生成 report.md",
}


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Any:
        raise UsageError(message, source="cli", recovery="运行 --help 查看用法")


def build_parser() -> argparse.ArgumentParser:
    parser = JSONArgumentParser(
        prog="repo-evolution-research",
        description="Research the recent technical evolution of a GitHub repository.",
        epilog=(
            "--format 可放在子命令之前或之后，"
            "例如：research.py init ... --format text"
        ),
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="stdout 输出格式（进度始终写入 stderr）",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {TOOL_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    doctor = subparsers.add_parser("doctor", help=_SUBCOMMAND_HELP["doctor"])
    doctor.add_argument(
        "--repo",
        help="可选仓库地址，用于校验解析结果并探测访问权限（只读）",
    )

    init = subparsers.add_parser("init", help=_SUBCOMMAND_HELP["init"])
    init.add_argument("--repo", required=True, help="GitHub 仓库 URL 或 owner/repo")
    init.add_argument("--mode", choices=MODES, help="研究模式，默认 standard")
    init.add_argument("--days", type=int, help="按时长回溯的天数")
    init.add_argument("--months", type=int, help="按日历月回溯（处理月末）")
    init.add_argument("--start", help="显式开始时间（ISO 8601，优先于 days/months）")
    init.add_argument("--end", help="显式结束时间（ISO 8601）；缺省为运行时刻")
    init.add_argument("--timezone", help="IANA 时区，默认 Asia/Shanghai")
    init.add_argument("--focus", action="append", help="关注词，可重复")
    init.add_argument(
        "--compare-previous",
        action="store_true",
        help="保留参数；V0.1 尚未实现与上一窗口比较",
    )
    init.add_argument("--no-discussions", action="store_true", help="本轮不采集 Discussions")
    init.add_argument("--no-docs", action="store_true", help="本轮不采集文档")
    init.add_argument("--language", help="报告语言；V0.1 固定 zh-CN，参数保留")
    init.add_argument("--output-root", help="快照根目录，默认 .repo-research")
    init.add_argument("--max-requests", type=int, help="覆盖预算 max_requests")
    init.add_argument(
        "--max-discovery-entities",
        type=int,
        help="覆盖预算 max_discovery_entities",
    )
    init.add_argument("--max-deep-entities", type=int, help="覆盖预算 max_deep_entities")
    init.add_argument("--max-deep-prs", type=int, help="覆盖预算 max_deep_prs")
    init.add_argument(
        "--agent-pr-reserve",
        type=int,
        help="覆盖预算 agent_pr_reserve",
    )
    init.add_argument("--max-total-bytes", type=int, help="覆盖预算 max_total_bytes")
    init.add_argument("--max-runtime-minutes", type=int, help="覆盖预算 max_runtime_minutes")

    collect = subparsers.add_parser("collect", help=_SUBCOMMAND_HELP["collect"])
    collect.add_argument("--run", required=True, help="init 返回的 run 目录")
    collect.add_argument("--force", action="store_true", help="collect 已完成时强制重跑")
    collect.add_argument(
        "--selection",
        help="Agent 的 analysis/selection.json；只做增量深读，不重跑完整 discovery",
    )

    prepare = subparsers.add_parser("prepare", help=_SUBCOMMAND_HELP["prepare"])
    prepare.add_argument("--run", required=True, help="init 返回的 run 目录")

    audit = subparsers.add_parser("audit", help=_SUBCOMMAND_HELP["audit"])
    audit.add_argument("--run", required=True, help="待审计的 run 目录（不修改任何产物）")

    import_source_parser = subparsers.add_parser(
        "import-source", help=_SUBCOMMAND_HELP["import-source"]
    )
    import_source_parser.add_argument("--run", required=True, help="init 返回的 run 目录")
    import_source_parser.add_argument(
        "--input", required=True, help="外部来源 envelope JSON 文件"
    )

    validate = subparsers.add_parser("validate", help=_SUBCOMMAND_HELP["validate"])
    validate.add_argument("--run", required=True, help="init 返回的 run 目录")

    render = subparsers.add_parser("render", help=_SUBCOMMAND_HELP["render"])
    render.add_argument("--run", required=True, help="init 返回的 run 目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _normalize_format_arguments(list(sys.argv[1:] if argv is None else argv))
    output_format = _peek_format(arguments)
    command: str | None = None
    try:
        args = build_parser().parse_args(arguments)
        command = args.command
        if command == "doctor":
            result, lines = command_doctor(args)
        elif command == "init":
            result, lines = command_init(args)
        elif command == "collect":
            result, lines = command_collect(args)
        elif command == "prepare":
            result, lines = command_prepare(args)
        elif command == "audit":
            result, lines = command_audit(args)
        elif command == "import-source":
            result, lines = command_import_source(args)
        elif command == "validate":
            result, lines = command_validate(args)
        elif command == "render":
            result, lines = command_render(args)
        else:
            raise UsageError(f"未知子命令：{command}", source="cli")
        if command == "doctor" and result["result"]["status"] != "ok":
            error = EnvironmentDegraded(
                "doctor 检测到必需能力缺失",
                source="cli.doctor",
                recovery="按 checks 中的 error 项修复后重新运行 doctor",
                details={"checks": result["result"]["checks"]},
            )
            result["ok"] = False
            result["error"] = error.to_dict()
            _emit_success(result, lines, output_format)
            if output_format == "text":
                print(f"error [{error.code}]: {error.message}", file=sys.stderr)
            return error.exit_code
        _emit_success(result, lines, output_format)
        if command == "audit":
            return result["result"]["exit_code"]
        if command in ("collect", "prepare") and result["result"].get("partial"):
            return EXIT_PARTIAL
        return EXIT_OK
    except RERError as exc:
        _emit_error(command, exc, output_format)
        return exc.exit_code
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - defensive boundary
        error = InternalError(
            f"未处理异常：{exc}",
            source="cli.main",
            details={"type": type(exc).__name__},
        )
        _emit_error(command, error, output_format)
        return error.exit_code


def _command_lock(func):
    """Serialize a whole command against other writers of the same run."""

    @functools.wraps(func)
    def wrapper(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
        run_dir = Path(args.run).expanduser().resolve()
        if not run_dir.is_dir():
            return func(args)
        store = SnapshotStore(run_dir)
        with store.run_lock():
            return func(args)

    return wrapper


def command_init(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    defaults = default_config()
    for name, value in (
        ("timezone", args.timezone),
        ("language", args.language),
        ("output-root", args.output_root),
    ):
        if value is not None:
            _require_nonempty(name, value)

    output_root_value = (
        args.output_root if args.output_root is not None else defaults["output_root"]
    )
    output_root = str(Path(output_root_value).expanduser().resolve())
    overrides = {
        "max_requests": args.max_requests,
        "max_discovery_entities": args.max_discovery_entities,
        "max_deep_entities": args.max_deep_entities,
        "max_deep_prs": args.max_deep_prs,
        "agent_pr_reserve": args.agent_pr_reserve,
        "max_total_bytes": args.max_total_bytes,
        "max_runtime_minutes": args.max_runtime_minutes,
    }
    now = datetime.now(UTC)
    config = build_config(
        repository=args.repo,
        mode=args.mode if args.mode is not None else defaults["mode"],
        days=args.days,
        months=args.months,
        start=args.start,
        end=args.end,
        timezone_name=(
            args.timezone if args.timezone is not None else defaults["window"]["timezone"]
        ),
        focus=args.focus if args.focus is not None else defaults["focus"],
        compare_previous=(
            args.compare_previous if args.compare_previous else defaults["compare_previous"]
        ),
        include_discussions=(
            False if args.no_discussions else defaults["include_discussions"]
        ),
        include_docs=False if args.no_docs else defaults["include_docs"],
        language=args.language if args.language is not None else defaults["language"],
        output_root=output_root,
        budget_overrides=overrides,
        now=now,
    )
    run_id = new_run_id(now)
    store = create_run(Path(output_root), config["repository"], run_id)
    store.begin_init()
    created_at = to_iso_z(now)
    warnings: list[str] = []
    if config["language"] != "zh-CN":
        warnings.append(
            "language is reserved in V0.1; output stays zh-CN and the parameter has no effect"
        )
    if config["compare_previous"]:
        warnings.append(
            "compare_previous is reserved in V0.1; window comparison is not implemented"
        )
    try:
        manifest = build_manifest(config, run_id, created_at)
        manifest["notes"].extend(f"reserved parameter: {item}" for item in warnings)
        manifest_path = store.write_manifest(manifest)
        config_path = store.write_json("config.resolved.json", config, schema_name="config")
        store.finish_init()
    except Exception:
        try:
            store.update_manifest(
                state="failed",
                notes=["init interrupted; run is not usable"],
            )
        except Exception:
            pass
        raise

    result = {
        "ok": True,
        "command": "init",
        "result": {
            "run": str(store.run_dir),
            "run_id": run_id,
            "config_path": str(config_path),
            "manifest_path": str(manifest_path),
            "window": config["window"],
            "mode": config["mode"],
        },
        "warnings": warnings,
    }
    window = config["window"]
    lines = [
        f"run: {store.run_dir}",
        f"window: {window['start']} -> {window['end']} ({window['timezone']})",
        f"config: {config_path}",
        f"manifest: {manifest_path}",
    ]
    lines.extend(f"warning: {item}" for item in warnings)
    print(f"initialized run {run_id}", file=sys.stderr)
    return result, lines


@_command_lock
def command_collect(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    run_dir = Path(args.run).expanduser().resolve()
    if not run_dir.is_dir():
        raise UsageError(
            f"run 目录不存在：{run_dir}",
            source="cli.collect",
            recovery="先运行 init 并将返回的 run 路径传给 --run",
        )
    store = SnapshotStore(run_dir)
    if store.initialization_pending():
        raise UsageError(
            "run 仍处于初始化未完成状态",
            source="cli.collect",
            recovery="重新运行 init 创建新的 run",
        )
    manifest, config = _load_run(store, require_current=True)
    if args.selection:
        return _command_collect_selection(store, manifest, config, args.selection)
    if manifest["stages"].get("collect") == "complete" and not args.force:
        raise UsageError(
            "collect 已完成",
            source="cli.collect",
            recovery="需要重新采集时使用 --force",
        )

    previous_checkpoint = _read_checkpoint_or_none(store)
    prior_budget_exhausted = False
    if (
        isinstance(previous_checkpoint, dict)
        and previous_checkpoint.get("config_hash") == manifest["config_hash"]
    ):
        checkpoint_usage = previous_checkpoint.get("budget_consumed")
        if isinstance(checkpoint_usage, dict):
            manifest["budget_consumed"] = _max_usage(
                manifest["budget_consumed"], checkpoint_usage
            )
        prior_budget_exhausted = bool(previous_checkpoint.get("budget_exhausted"))
    _preflight_run_budget(manifest, config)
    previous_manifest = copy.deepcopy(manifest)
    invalidated = _invalidate_downstream(manifest)
    client = GitHubClient(
        store,
        config["budget"],
        initial_usage=manifest["budget_consumed"],
    )
    initial_budget = dict(manifest["budget_consumed"])

    def collect_usage() -> dict[str, Any]:
        return _merge_usage(initial_budget, client.usage_snapshot())

    progress = CollectProgress(
        store,
        config["config_hash"],
        usage_provider=collect_usage,
    )
    started_at = to_iso_z(datetime.now(UTC))
    manifest["state"] = "collecting"
    manifest["stages"]["collect"] = "in_progress"
    manifest["collection_started_at"] = started_at
    manifest["collection_finished_at"] = None
    manifest["observed"]["started_at"] = started_at
    manifest["observed"]["finished_at"] = None
    store.write_manifest(manifest)
    progress.flush(observed_at=started_at)

    try:
        collection = collect_repository(
            client,
            store,
            config["repository"],
            started_at,
            include_docs=config["include_docs"],
            include_discussions=config["include_discussions"],
            progress=progress,
        )
        activity = collect_activity(
            client,
            store,
            config["repository"],
            activity_window=config["window"],
            source_profile=collection.source_profile,
            include_discussions=config["include_discussions"],
            progress=progress,
        )
        context = CollectContext(
            client=client,
            store=store,
            repository=config["repository"],
            progress=progress,
            budget_exhausted=prior_budget_exhausted or progress.budget_exhausted,
        )
        pull_requests = collect_pull_requests(
            client,
            store,
            config["repository"],
            activity_window=config["window"],
            activity_overview=activity.overview,
            focus=config.get("focus", []),
            progress=progress,
            force=args.force,
        )
        proposals = collect_proposals(
            context,
            activity_window=config["window"],
            activity_overview=activity.overview,
            pr_selection=pull_requests.selection,
            include_discussions=config["include_discussions"],
        )
        releases = collect_releases(
            context,
            activity_window=config["window"],
        )
        documents = collect_documents(
            context,
            source_profile=collection.source_profile,
            revision=collection.default_branch_sha,
            include_docs=config["include_docs"],
        )
    except Exception as exc:
        finished_at = to_iso_z(datetime.now(UTC))
        code = getattr(exc, "code", type(exc).__name__)
        exit_code = getattr(exc, "exit_code", EXIT_INTERNAL)
        usage = client.usage_snapshot()
        prior_collected = previous_manifest["stages"].get("collect") in (
            "complete",
            "partial",
        )
        if not progress.coverage and prior_collected:
            _restore_previous_snapshot(
                manifest,
                previous_manifest,
                usage,
                note=(
                    f"collect failed before any channel: {code}; "
                    "previous snapshot preserved"
                ),
            )
            try:
                store.write_manifest(manifest)
            except Exception as manifest_exc:
                print(f"warning: 无法写入 manifest: {manifest_exc}", file=sys.stderr)
            try:
                if previous_checkpoint is not None:
                    progress.restore(previous_checkpoint)
                    progress.flush(
                        observed_at=str(
                            previous_checkpoint.get("observed_at") or finished_at
                        ),
                        extra={"budget_consumed": manifest["budget_consumed"]},
                    )
                else:
                    progress.restore(
                        {
                            "status": previous_manifest["stages"]["collect"],
                            "config_hash": manifest["config_hash"],
                            "observed_at": (
                                previous_manifest["observed"]["finished_at"]
                                or finished_at
                            ),
                            "coverage": previous_manifest["coverage"],
                            "default_branch": previous_manifest["default_branch"],
                            "default_branch_sha": previous_manifest[
                                "default_branch_sha"
                            ],
                        }
                    )
                    progress.flush(
                        observed_at=(
                            previous_manifest["observed"]["finished_at"] or finished_at
                        ),
                        extra={"budget_consumed": manifest["budget_consumed"]},
                    )
            except Exception as checkpoint_exc:
                print(
                    f"warning: 无法恢复 collect checkpoint: {checkpoint_exc}",
                    file=sys.stderr,
                )
            raise

        state, stage_status = derive_stage_status(
            exit_code=getattr(exc, "exit_code", EXIT_INTERNAL),
            has_coverage=bool(progress.coverage),
        )
        manifest["state"] = state
        manifest["stages"]["collect"] = stage_status
        manifest["collection_finished_at"] = finished_at
        manifest["observed"]["finished_at"] = finished_at
        manifest["coverage"] = merge_coverage(manifest["coverage"], progress.coverage)
        for key in ("default_branch", "default_branch_sha"):
            if progress.state.get(key):
                manifest[key] = progress.state[key]
        manifest["budget_consumed"] = _merge_usage(
            manifest["budget_consumed"],
            usage,
        )
        manifest["notes"].append(f"collect {state}: {code}")
        manifest_written = False
        try:
            store.write_manifest(manifest)
            manifest_written = True
        except Exception as manifest_exc:
            print(f"warning: 无法写入 manifest: {manifest_exc}", file=sys.stderr)
        if manifest_written:
            discard_report(store)
        try:
            progress.flush(
                status=state,
                error=code,
                exit_code=exit_code,
                observed_at=finished_at,
            )
        except Exception as checkpoint_exc:
            print(f"warning: 无法写入 collect checkpoint: {checkpoint_exc}", file=sys.stderr)
        raise

    finished_at = to_iso_z(datetime.now(UTC))
    coverage = merge_coverage(
        merge_coverage(
            merge_coverage(collection.coverage, activity.coverage),
            merge_coverage(releases.coverage, documents.coverage),
        ),
        merge_coverage(pull_requests.coverage, proposals.coverage),
    )
    blocking = (
        has_blocking_gaps(coverage)
        or context.budget_exhausted
        or progress.budget_exhausted
    )
    manifest["coverage"] = merge_coverage(manifest["coverage"], coverage)
    manifest["default_branch"] = collection.default_branch
    manifest["default_branch_sha"] = collection.default_branch_sha
    manifest["budget_consumed"] = _merge_usage(
        manifest["budget_consumed"],
        client.usage_snapshot(),
    )
    manifest["stages"]["collect"] = "partial" if blocking else "complete"
    manifest["observed"]["finished_at"] = finished_at
    manifest["collection_finished_at"] = finished_at
    manifest["state"] = "partial" if blocking else "collecting"
    if blocking:
        manifest["notes"].append("collect completed with blocking coverage gaps")
    if context.budget_exhausted:
        manifest["notes"].append("collect stopped by run-level budget exhaustion")
    progress.flush(
        status="partial" if blocking else "complete",
        observed_at=finished_at,
    )
    store.write_manifest(manifest)
    if invalidated:
        discard_report(store)

    warnings = [
        f"{item['channel']}: {item['coverage_status']}"
        for item in coverage
        if item["coverage_status"] in ("partial", "sampled", "unavailable")
    ]
    if context.budget_exhausted:
        warnings.append("budget_exhausted: run-level budget exhausted")
    result = {
        "ok": True,
        "command": "collect",
        "result": {
            "run": str(run_dir),
            "default_branch": collection.default_branch,
            "default_branch_sha": collection.default_branch_sha,
            "source_profile": str(run_dir / "analysis/source-profile.json"),
            "activity_overview": str(run_dir / "analysis/activity-overview.json"),
            "activity_counts": activity.counts,
            "activity_candidate_count": activity.candidate_count,
            "releases_dir": str(run_dir / "raw/releases"),
            "release_counts": releases.counts,
            "docs_dir": str(run_dir / "raw/docs"),
            "doc_counts": documents.counts,
            "proposal_selection": str(run_dir / "analysis/proposal-selection.json"),
            "issue_details_dir": str(run_dir / "raw/issues"),
            "discussion_details_dir": str(run_dir / "raw/discussions"),
            "proposal_counts": proposals.counts,
            "pr_selection": str(run_dir / "analysis/pr-selection.json"),
            "pr_details_dir": str(run_dir / "raw/pr-details"),
            "pr_detail_counts": pull_requests.counts,
            "coverage": coverage,
            "budget_consumed": manifest["budget_consumed"],
            "partial": blocking,
        },
        "warnings": warnings,
    }
    coverage_text = ", ".join(
        f"{item['channel']}={item['coverage_status']}" for item in coverage
    )
    lines = [
        f"run: {run_dir}",
        f"default_branch: {collection.default_branch} ({collection.default_branch_sha})",
        f"source_profile: {run_dir / 'analysis/source-profile.json'}",
        f"activity_overview: {run_dir / 'analysis/activity-overview.json'}",
        f"activity_candidates: {activity.candidate_count}",
        f"releases: {releases.counts['written']}"
        f" (selected {releases.counts['selected']})",
        f"documents: {documents.counts['read']}/{documents.counts['selected']}"
        f" (reused {documents.counts['reused']})",
        f"proposals: issue={proposals.counts['issue_selected']} "
        f"discussion={proposals.counts['discussion_selected']} "
        f"(candidates {proposals.counts['candidates']})",
        f"pr_details: {pull_requests.counts['details_written']}"
        f" (selected {pull_requests.counts['selected']})",
        f"coverage: {coverage_text}",
        "budget: requests="
        f"{manifest['budget_consumed']['requests']} "
        f"bytes={manifest['budget_consumed']['total_bytes']}",
    ]
    print(f"collected {config['repository']['slug']}", file=sys.stderr)
    return result, lines


def _command_collect_selection(
    store: SnapshotStore,
    manifest: dict[str, Any],
    config: dict[str, Any],
    selection_value: str,
) -> tuple[dict[str, Any], list[str]]:
    stage = manifest["stages"].get("collect")
    if stage not in ("complete", "partial"):
        raise UsageError(
            f"collect 尚未完成（{stage}），不能按 selection 增量深读",
            source="cli.collect.selection",
            recovery="先完成常规 collect 与 prepare，再提交 selection.json",
            details={"collect_status": stage},
        )
    selection_path = Path(selection_value).expanduser().resolve()
    if not selection_path.is_file():
        raise UsageError(
            f"selection 文件不存在：{selection_path}",
            source="cli.collect.selection",
            recovery="传入 Agent 写好的 analysis/selection.json",
        )
    selection = load_agent_selection(store, manifest, path=selection_path)
    assert selection is not None
    previous_state = str(manifest["state"])
    note = f"selection read started: {selection_path}"
    if note not in manifest["notes"]:
        manifest["notes"].append(note)
    manifest["state"] = "collecting"
    store.write_manifest(manifest)
    client = GitHubClient(
        store,
        config["budget"],
        initial_usage=manifest["budget_consumed"],
    )
    requests_before = int(manifest["budget_consumed"].get("requests", 0))
    try:
        report = collect_selection(
            store,
            client,
            config,
            manifest,
            selection,
            selection_path=selection_path,
        )
    except Exception:
        manifest["budget_consumed"] = _merge_usage(
            manifest["budget_consumed"],
            client.usage_snapshot(),
        )
        manifest["state"] = "partial"
        observed = _finish_selection_observation(manifest, requests_before)
        manifest["notes"].append("selection read failed")
        report_path = store.secure_path(SELECTION_REPORT_PATH)
        invalidated = _invalidate_downstream(manifest) if observed else False
        if report_path.is_file():
            try:
                partial_report = store.read_json(SELECTION_REPORT_PATH)
            except RERError:
                partial_report = {}
            if int((partial_report.get("counts") or {}).get("fetched") or 0) > 0:
                invalidated = _invalidate_downstream(manifest) or invalidated
        store.write_manifest(manifest)
        if invalidated:
            discard_report(store)
        raise
    manifest["budget_consumed"] = _merge_usage(
        manifest["budget_consumed"],
        client.usage_snapshot(),
    )
    observed = _finish_selection_observation(manifest, requests_before)
    invalidated = False
    if report["counts"]["fetched"] or observed:
        invalidated = _invalidate_downstream(manifest)
    if report["partial"]:
        manifest["state"] = "partial"
    elif report["counts"]["fetched"]:
        manifest["state"] = "collecting"
    else:
        manifest["state"] = previous_state
    manifest["notes"].append(
        "selection read: "
        f"selected={report['counts']['selected']} "
        f"fetched={report['counts']['fetched']} "
        f"skipped_unchanged={report['counts']['skipped_unchanged']} "
        f"recorded={report['counts']['recorded']} "
        f"not_attempted={report['counts']['not_attempted']} "
        f"failed={report['counts']['failed']}"
    )
    store.write_manifest(manifest)
    if invalidated:
        discard_report(store)
    report_path = store.secure_path(SELECTION_REPORT_PATH)
    result = {
        "ok": True,
        "command": "collect",
        "result": {
            "run": str(store.run_dir),
            "selection": str(selection_path),
            "selection_report": str(report_path),
            "counts": report["counts"],
            "budget": report["budget"],
            "partial": report["partial"],
        },
        "warnings": (
            [f"selection partial: {', '.join(report['limitations'])}"]
            if report["partial"]
            else []
        ),
    }
    lines = [
        f"run: {store.run_dir}",
        f"selection: {selection_path}",
        f"selection_report: {report_path}",
        f"selected: {report['counts']['selected']}",
        f"fetched: {report['counts']['fetched']} "
        f"skipped_unchanged={report['counts']['skipped_unchanged']} "
        f"recorded={report['counts']['recorded']}",
        f"not_attempted: {report['counts']['not_attempted']} "
        f"failed={report['counts']['failed']}",
        f"deep_entities: {report['budget']['deep_entities_after']}"
        f"/{report['budget']['max_deep_entities']}",
    ]
    return result, lines


def _finish_selection_observation(manifest: dict[str, Any], requests_before: int) -> bool:
    """Extend the observation window only when incremental reads reached GitHub."""
    if int(manifest["budget_consumed"].get("requests", 0)) <= requests_before:
        return False
    finished_at = to_iso_z(datetime.now(UTC))
    manifest["observed"]["finished_at"] = finished_at
    manifest["collection_finished_at"] = finished_at
    return True


def command_audit(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    run_dir = Path(args.run).expanduser().resolve()
    if not run_dir.is_dir():
        raise UsageError(f"run 目录不存在：{run_dir}", source="cli.audit")
    report = audit_snapshot(SnapshotStore(run_dir, read_only=True))
    result = {
        "ok": report["integrity"]["ok"],
        "command": "audit",
        "result": {"run": str(run_dir), **report},
        "warnings": report["recovery"],
    }
    lines = [
        f"run: {run_dir}",
        f"audit: {report['status']}",
        f"rebuild_required: {len(report['rebuild_required'])}",
        f"attention_required: {len(report['attention_required'])}",
        *report["recovery"],
    ]
    return result, lines


@_command_lock
def command_prepare(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    run_dir = Path(args.run).expanduser().resolve()
    if not run_dir.is_dir():
        raise UsageError(
            f"run 目录不存在：{run_dir}",
            source="cli.prepare",
            recovery="先运行 init/collect，并将返回的 run 路径传给 --run",
        )
    store = SnapshotStore(run_dir)
    if store.initialization_pending():
        raise UsageError(
            "run 仍处于初始化未完成状态",
            source="cli.prepare",
            recovery="重新运行 init 创建新的 run",
        )
    manifest, config = _load_run(store)
    collect_status = manifest["stages"].get("collect")
    if collect_status not in ("complete", "partial"):
        raise UsageError(
            f"collect 尚未完成（{collect_status}），不能 prepare",
            source="cli.prepare",
            recovery="先完成 collect；partial 状态允许 prepare",
            details={"collect_status": collect_status},
        )
    before = _prepare_artifact_snapshot(store)
    previous_downstream = {
        stage: manifest["stages"].get(stage)
        for stage in ("research", "validate", "render")
    }
    manifest["stages"]["prepare"] = "in_progress"
    store.write_manifest(manifest)
    try:
        report = prepare_run(store, config, manifest)
    except Exception:
        manifest["stages"]["prepare"] = "failed"
        manifest["state"] = "failed"
        if "prepare failed" not in manifest["notes"]:
            manifest["notes"].append("prepare failed")
        try:
            store.write_manifest(manifest)
        except Exception as exc:
            print(f"warning: 无法写入 prepare 失败状态: {exc}", file=sys.stderr)
        raise
    after = _prepare_artifact_snapshot(store)
    invalidated = False
    if after != before:
        for stage in ("research", "validate", "render"):
            manifest["stages"][stage] = "not_started"
        if any(value not in (None, "not_started") for value in previous_downstream.values()):
            note = "prepare changed evidence; downstream stages invalidated"
            if note not in manifest["notes"]:
                manifest["notes"].append(note)
            invalidated = True
    manifest["stages"]["prepare"] = "complete"
    manifest["state"] = "prepared"
    store.write_manifest(manifest)
    if invalidated:
        discard_report(store)
    path = store.secure_path("analysis/prepare-report.json")
    warnings = [
        f"evidence_truncated: {item['document_key']}"
        for item in report["evidence_truncated"]
    ]
    if report.get("relations_merged"):
        warnings.append(f"relations_merged: {report['relations_merged']}")
    has_evidence = report["evidence_total"] > 0
    if not has_evidence:
        warnings.append(
            "no_research_evidence: 没有可供研究的证据；请按 coverage 补读材料，"
            "预算已耗尽时新建较小窗口的 run，或使用 import-source 导入来源后重跑 prepare"
        )
    result = {
        "ok": True,
        "command": "prepare",
        "result": {
            "run": str(store.run_dir),
            "partial": not has_evidence,
            "has_evidence": has_evidence,
            "report": str(path),
            "evidence_path": str(store.secure_path("normalized/evidence.jsonl")),
            "relations_path": str(store.secure_path("normalized/relations.jsonl")),
            "text_sources": report["text_sources"],
            "evidence_blocks": report["evidence_blocks"],
            "evidence_derived": report["evidence_derived"],
            "evidence_total": report["evidence_total"],
            "relations": report["relations"],
            "relations_derived": report["relations_derived"],
            "relations_total": report["relations_total"],
            "relations_merged": report["relations_merged"],
            "relation_counts": report["relation_counts"],
            "commit_entities": report["commit_entities"],
            "external_entities": report["external_entities"],
            "evidence_truncated": report["evidence_truncated"],
            "counts": report["counts"],
            "candidates": str(store.secure_path(CANDIDATES_PATH)),
            "pack_index": str(store.secure_path(PACK_INDEX_PATH)),
            "requests": manifest["budget_consumed"]["requests"],
        },
        "warnings": warnings,
    }
    lines = [
        f"run: {store.run_dir}",
        f"evidence: {report['evidence_blocks']} blocks from {report['text_sources']} sources",
        f"relations: {report['relations']} ({report['relation_counts']})",
        f"commits: {report['commit_entities']} links; "
        f"external entities: {report['external_entities']}",
        f"candidates: {store.secure_path(CANDIDATES_PATH)}",
        f"packs: {store.secure_path(PACK_INDEX_PATH)}",
        f"report: {path}",
    ]
    print(f"prepared {config['repository']['slug']}", file=sys.stderr)
    return result, lines


@_command_lock
def command_import_source(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    store, manifest, _config = _open_run(
        args.run, "import-source", require_current=True
    )
    if not store.secure_path("normalized/entities.jsonl").is_file():
        raise UsageError(
            "缺少 normalized/entities.jsonl，不能导入外部来源",
            source="cli.import_source",
            recovery="先运行 prepare，使 release/PR 实体进入 normalized/entities.jsonl",
        )
    envelope_path = Path(args.input).expanduser().resolve()
    envelope = load_envelope(envelope_path)
    imported = import_source(store, envelope)
    _invalidate_downstream(
        manifest,
        note="import-source changed normalized inputs; downstream stages invalidated",
    )
    manifest["state"] = "collecting"
    manifest["notes"].append(
        "import-source: "
        f"evidence={imported['evidence_id']} relations={len(imported['relation_ids'])}"
    )
    store.write_manifest(manifest)
    discard_report(store)
    discard_incomplete(store)
    warnings = [
        "import-source invalidated prepare/validate/render; rerun prepare",
        *[str(item) for item in imported.get("warnings") or []],
    ]
    aliases = imported.get("relation_aliases") or {}
    if aliases:
        warnings.append(
            "merged relation aliases: "
            + ", ".join(
                f"{source}->{target}" for source, target in sorted(aliases.items())
            )
        )
    result = {
        "ok": True,
        "command": "import-source",
        "result": {
            "run": str(store.run_dir),
            "input": str(envelope_path),
            **imported,
            "relations": str(store.secure_path("normalized/relations.jsonl")),
            "evidence": str(store.secure_path("normalized/evidence.jsonl")),
        },
        "warnings": warnings,
    }
    lines = [
        f"run: {store.run_dir}",
        f"evidence: {imported['evidence_id']}",
        f"relations: {len(imported['relation_ids'])}",
        "next: rerun prepare",
    ]
    return result, lines


@_command_lock
def command_validate(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    store, manifest, _config = _open_run(args.run, "validate")
    prepare_status = manifest["stages"].get("prepare")
    if prepare_status != "complete":
        raise UsageError(
            f"prepare 尚未完成（{prepare_status}），不能 validate",
            source="cli.validate",
            recovery="先运行 prepare，再由 Agent 写入 analysis/research.json",
            details={"prepare_status": prepare_status},
        )
    if not store.secure_path(RESEARCH_PATH).is_file():
        stale_validation = store.secure_path(VALIDATION_PATH)
        if stale_validation.is_file():
            stale_validation.unlink()
        manifest["stages"]["validate"] = "failed"
        manifest["stages"]["render"] = "not_started"
        manifest["state"] = "researching"
        store.write_manifest(manifest)
        write_incomplete(
            store,
            manifest,
            [f"缺少 {RESEARCH_PATH}，无法校验研究结果"],
            {"ok": False, "errors": [], "warnings": []},
        )
        raise UsageError(
            f"缺少 {RESEARCH_PATH}",
            source="cli.validate",
            recovery="由 Agent 写入 analysis/research.json 后再运行 validate",
        )
    previous_sha = _previous_research_sha(store)
    document = validate_run(store, manifest)
    path = write_validation(store, document)
    if document["research_sha256"] is not None:
        manifest["stages"]["research"] = "complete"
    invalidated = previous_sha != document["research_sha256"]
    if invalidated:
        manifest["stages"]["render"] = "not_started"
    if document["ok"]:
        manifest["stages"]["validate"] = "complete"
        manifest["state"] = (
            "rendered"
            if manifest["stages"].get("render") == "complete"
            else "validated"
        )
    else:
        manifest["stages"]["validate"] = "failed"
        manifest["stages"]["render"] = "not_started"
        manifest["state"] = "researching"
    store.write_manifest(manifest)
    if invalidated:
        discard_report(store)
    if not document["ok"]:
        write_incomplete(
            store,
            manifest,
            [
                f"validation 未通过：{document['counts']['errors']} 个阻断错误，"
                "旧的 report.md 已失效"
            ],
            document,
        )
        raise ValidationFailure(
            f"研究结果未通过校验（{document['counts']['errors']} 个阻断错误）",
            source="cli.validate",
            recovery="按 validation.errors 修复 analysis/research.json 后重新 validate",
            details={
                "validation": str(path),
                "errors": document["counts"]["errors"],
                "warnings": document["counts"]["warnings"],
                "codes": sorted({item["code"] for item in document["errors"]}),
            },
        )
    discard_incomplete(store)
    result = {
        "ok": True,
        "command": "validate",
        "result": {
            "run": str(store.run_dir),
            "research": str(store.secure_path(RESEARCH_PATH)),
            "validation": str(store.secure_path(VALIDATION_PATH)),
            "evidence_review": str(store.secure_path(EVIDENCE_REVIEW_PATH)),
            "ok": document["ok"],
            "errors": document["counts"]["errors"],
            "warnings": document["counts"]["warnings"],
            "checks": document["checks"],
            "lifecycle": document["counts"]["lifecycle"],
        },
        "warnings": [item["message"] for item in document["warnings"]],
    }
    lines = [
        f"run: {store.run_dir}",
        f"validation: {store.secure_path(VALIDATION_PATH)}",
        f"evidence review: {store.secure_path(EVIDENCE_REVIEW_PATH)}",
        f"errors: {document['counts']['errors']}; warnings: {document['counts']['warnings']}",
        f"lifecycle: {document['counts']['lifecycle']} capabilities checked",
    ]
    print("validated research.json", file=sys.stderr)
    return result, lines


@_command_lock
def command_render(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    store, manifest, _config = _open_run(args.run, "render")
    validate_status = manifest["stages"].get("validate")
    if validate_status == "failed":
        manifest["stages"]["render"] = "failed"
        store.write_manifest(manifest)
        write_incomplete(
            store,
            manifest,
            ["validate 未通过，旧的 report.md 已失效"],
            {"ok": False, "errors": [], "warnings": []},
        )
        raise ValidationFailure(
            "validate 未通过，不能渲染正式报告",
            source="cli.render",
            recovery="按 analysis/validation.json 修复 research.json 后重新 validate",
            details={"validate_status": validate_status},
        )
    if validate_status != "complete":
        manifest["stages"]["render"] = "failed"
        store.write_manifest(manifest)
        write_incomplete(
            store,
            manifest,
            [f"validate 尚未通过（{validate_status}），旧的 report.md 已失效"],
            {"ok": False, "errors": [], "warnings": []},
        )
        raise UsageError(
            f"validate 尚未通过（{validate_status}），不能 render",
            source="cli.render",
            recovery="先运行 validate，修复所有阻断错误后再 render",
            details={"validate_status": validate_status},
        )
    discard_incomplete(store)
    try:
        report = render_run(store, manifest)
    except Exception as exc:
        _cleanup_failed_render(store, manifest, exc)
        raise
    manifest["stages"]["render"] = "complete"
    manifest["state"] = "rendered"
    store.write_manifest(manifest)
    result = {
        "ok": True,
        "command": "render",
        "result": {
            "run": str(store.run_dir),
            "report": report["report"],
            "render_report": report["render_report"],
            "sha256": report["report_sha256"],
            "chars": report["chars"],
            "template_version": report["template_version"],
        },
        "warnings": [],
    }
    lines = [
        f"run: {store.run_dir}",
        f"report: {report['report']}",
        f"sha256: {report['report_sha256']}",
        f"chars: {report['chars']}",
    ]
    print(f"rendered {store.secure_path(REPORT_PATH)}", file=sys.stderr)
    return result, lines


def _open_run(
    run: str,
    command: str,
    *,
    require_current: bool = False,
) -> tuple[SnapshotStore, dict[str, Any], dict[str, Any]]:
    run_dir = Path(run).expanduser().resolve()
    if not run_dir.is_dir():
        raise UsageError(
            f"run 目录不存在：{run_dir}",
            source=f"cli.{command}",
            recovery="先运行 init/collect，并将返回的 run 路径传给 --run",
        )
    store = SnapshotStore(run_dir)
    if store.initialization_pending():
        raise UsageError(
            "run 仍处于初始化未完成状态",
            source=f"cli.{command}",
            recovery="重新运行 init 创建新的 run",
        )
    manifest, config = _load_run(store, require_current=require_current)
    return store, manifest, config


def _cleanup_failed_render(
    store: SnapshotStore,
    manifest: dict[str, Any],
    error: Exception,
) -> None:
    """Never leave a formal report behind when rendering fails after the gate."""
    manifest["stages"]["render"] = "failed"
    manifest["state"] = "researching"
    _write_manifest_best_effort(store, manifest)
    message = str(getattr(error, "message", error) or type(error).__name__)
    if store.secure_path("incomplete.md").is_file():
        try:
            discard_report(store)
        except Exception as discard_exc:
            print(
                f"warning: 无法删除旧报告: {discard_exc}",
                file=sys.stderr,
            )
        return
    try:
        write_incomplete(
            store,
            manifest,
            [f"render 执行失败，旧的 report.md 已失效：{message}"],
            {"ok": False, "errors": [], "warnings": []},
        )
    except Exception as cleanup_exc:
        print(
            f"warning: 无法写入 incomplete.md: {cleanup_exc}",
            file=sys.stderr,
        )
        try:
            discard_report(store)
        except Exception as discard_exc:
            print(
                f"warning: 无法删除旧报告: {discard_exc}",
                file=sys.stderr,
            )


def _previous_research_sha(store: SnapshotStore) -> str | None:
    path = store.secure_path(VALIDATION_PATH)
    if not path.is_file():
        return None
    try:
        document = store.read_json(VALIDATION_PATH)
    except RERError:
        return None
    if not isinstance(document, dict):
        return None
    value = document.get("research_sha256")
    return value if isinstance(value, str) else None


def _write_manifest_best_effort(
    store: SnapshotStore,
    manifest: dict[str, Any],
) -> None:
    try:
        store.write_manifest(manifest)
    except Exception as exc:
        print(f"warning: 无法写入 manifest 状态: {exc}", file=sys.stderr)


def _prepare_artifact_snapshot(store: SnapshotStore) -> dict[str, bytes]:
    paths = [
        "normalized/evidence.jsonl",
        "normalized/relations.jsonl",
        CANDIDATES_PATH,
        PACK_INDEX_PATH,
    ]
    directory = store.secure_path("analysis/packs")
    if directory.is_dir():
        paths.extend(
            f"analysis/packs/{path.name}" for path in sorted(directory.glob("*.md"))
        )
    return {
        relpath: store.secure_path(relpath).read_bytes()
        for relpath in paths
        if store.secure_path(relpath).is_file()
    }


def _load_run(
    store: SnapshotStore,
    *,
    require_current: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = store.read_manifest()
    if require_current and manifest.get("schema_version") != SCHEMA_VERSION:
        found_version = manifest.get("schema_version")
        raise UsageError(
            f"manifest schema_version 不兼容：{found_version!r} != {SCHEMA_VERSION}",
            source="manifest.schema_version",
            details={
                "found": found_version,
                "expected": SCHEMA_VERSION,
            },
            recovery="旧版本 run 不兼容；请重新运行 init",
        )
    for key in ("host", "owner", "name"):
        value = manifest["repository"].get(key)
        if isinstance(value, str) and value != value.lower():
            raise ArtifactError(
                "legacy run 的 repository 大小写未规范化",
                source="cli.run_binding",
                recovery="旧版本创建的 run 需要重新运行 init",
                details={"field": key, "value": value},
            )
    try:
        path_parts = (
            store.run_dir.parents[2].name,
            store.run_dir.parents[1].name,
            store.run_dir.parents[0].name,
        )
    except IndexError as exc:
        raise ArtifactError(
            "run 目录层级不完整",
            source="cli.run_binding",
            recovery="重新运行 init，不要手工移动 run 目录",
        ) from exc
    expected_parts = (
        manifest["repository"]["host"],
        manifest["repository"]["owner"],
        manifest["repository"]["name"],
    )
    if path_parts != expected_parts:
        raise ArtifactError(
            "run 目录与 manifest.repository 不一致",
            source="cli.run_binding",
            recovery="旧版本或手工移动的 run 需要重新运行 init",
            details={"path": list(path_parts), "manifest": list(expected_parts)},
        )
    config = read_resolved_config(
        store.run_dir / "config.resolved.json",
        artifact=not require_current,
    )
    if manifest["run_id"] != store.run_dir.name:
        raise ArtifactError(
            "manifest.run_id 与 run 目录名不一致",
            source="cli.run_binding",
            recovery="不要手工移动 run 目录；重新运行 init",
            details={"manifest_run_id": manifest["run_id"], "run_dir": store.run_dir.name},
        )
    if manifest["config_hash"] != config["config_hash"]:
        raise ArtifactError(
            "manifest.config_hash 与 config.resolved.json 不一致",
            source="cli.run_binding",
            recovery="不要手工修改 run 内的配置；重新运行 init",
            details={
                "manifest_config_hash": manifest["config_hash"],
                "config_hash": config["config_hash"],
            },
        )
    for key in ("host", "owner", "name"):
        if manifest["repository"].get(key) != config["repository"].get(key):
            raise ArtifactError(
                "manifest.repository 与 config.resolved.json 不一致",
                source="cli.run_binding",
                recovery="不要手工修改 run 内的配置；重新运行 init",
                details={
                    "field": key,
                    "manifest": manifest["repository"].get(key),
                    "config": config["repository"].get(key),
                },
            )
    expected_window = {
        "start": config["window"]["start"],
        "end": config["window"]["end"],
        "timezone": config["window"]["timezone"],
    }
    if manifest["activity_window"] != expected_window:
        raise ArtifactError(
            "manifest.activity_window 与 config.resolved.json 不一致",
            source="cli.run_binding",
            recovery="不要手工修改 run 内的配置；重新运行 init",
            details={"manifest": manifest["activity_window"], "config": expected_window},
        )
    return manifest, config


def _invalidate_downstream(
    manifest: dict[str, Any],
    *,
    note: str = "collect changed inputs; downstream stages invalidated",
) -> bool:
    changed = False
    for stage in _DOWNSTREAM_STAGES:
        if manifest["stages"].get(stage) != "not_started":
            manifest["stages"][stage] = "not_started"
            changed = True
    if changed:
        if manifest.get("state") in ("validated", "rendered"):
            manifest["state"] = "collecting"
        manifest["notes"].append(note)
    return changed


def _max_usage(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in _BUDGET_COUNTERS:
        current = existing.get(key, 0) or 0
        candidate = incoming.get(key, 0) or 0
        if isinstance(current, float) or isinstance(candidate, float):
            merged[key] = round(max(float(current), float(candidate)), 3)
        else:
            merged[key] = max(int(current), int(candidate))
    return merged


def _merge_usage(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key in _BUDGET_COUNTERS:
        current = existing.get(key, 0) or 0
        added = incoming.get(key, 0) or 0
        if isinstance(current, float) or isinstance(added, float):
            merged[key] = round(float(current) + float(added), 3)
        else:
            merged[key] = int(current) + int(added)
    return merged


def _preflight_run_budget(manifest: dict[str, Any], config: dict[str, Any]) -> None:
    budget = config["budget"]
    used = manifest["budget_consumed"]
    for used_key, budget_key, label in (
        ("requests", "max_requests", "请求"),
        ("total_bytes", "max_total_bytes", "累计响应字节"),
    ):
        current = int(used.get(used_key, 0) or 0)
        limit = int(budget[budget_key])
        if current >= limit:
            raise BudgetExceededError(
                f"{label}预算已用尽（{limit}）",
                source="cli.budget",
                recovery="新建 run，或在 init 时提高预算后重新采集",
                details={"used": current, "limit": limit},
            )
    runtime_seconds = float(used.get("runtime_seconds", 0) or 0)
    runtime_limit = int(budget["max_runtime_minutes"]) * 60
    if runtime_seconds >= runtime_limit:
        raise BudgetExceededError(
            f"运行时间预算已用尽（{runtime_limit} 秒）",
            source="cli.budget",
            recovery="新建 run，或在 init 时提高预算后重新采集",
            details={"used_seconds": round(runtime_seconds, 3), "limit_seconds": runtime_limit},
        )


def _read_checkpoint_or_none(store: SnapshotStore) -> dict[str, Any] | None:
    try:
        return store.read_checkpoint("collect")
    except RERError:
        return None


def _restore_previous_snapshot(
    manifest: dict[str, Any],
    previous: dict[str, Any],
    usage: dict[str, Any],
    *,
    note: str,
) -> None:
    for key in (
        "state",
        "stages",
        "observed",
        "collection_started_at",
        "collection_finished_at",
        "coverage",
        "notes",
        "default_branch",
        "default_branch_sha",
    ):
        manifest[key] = copy.deepcopy(previous[key])
    manifest["budget_consumed"] = _merge_usage(previous["budget_consumed"], usage)
    manifest["notes"].append(note)


def command_doctor(args: argparse.Namespace) -> tuple[dict[str, Any], list[str]]:
    checks: list[dict[str, str]] = []

    python_ok = sys.version_info >= (3, 11)
    checks.append(
        {
            "name": "python",
            "status": "ok" if python_ok else "error",
            "detail": platform.python_version(),
        }
    )

    try:
        jsonschema_version = importlib.metadata.version("jsonschema")
        checks.append({"name": "jsonschema", "status": "ok", "detail": jsonschema_version})
    except importlib.metadata.PackageNotFoundError:
        checks.append(
            {
                "name": "jsonschema",
                "status": "error",
                "detail": "未安装（需要 >=4.23,<5）",
            }
        )

    try:
        formats = format_checker_status()
        checks.append(
            {
                "name": "format_checkers",
                "status": "ok" if all(formats.values()) else "error",
                "detail": ", ".join(
                    f"{name}={'ok' if ready else 'missing'}"
                    for name, ready in formats.items()
                ),
            }
        )
    except RERError as exc:
        checks.append(
            {
                "name": "format_checkers",
                "status": "error",
                "detail": exc.message,
            }
        )

    git_path = shutil.which("git")
    checks.append(
        {
            "name": "git",
            "status": "ok" if git_path else "missing",
            "detail": (
                _first_line(_run_command([git_path, "--version"]))
                if git_path
                else "未安装"
            ),
        }
    )

    gh_path = shutil.which("gh")
    if not gh_path:
        checks.append({"name": "gh", "status": "error", "detail": "未安装"})
        checks.append(
            {"name": "gh_auth", "status": "error", "detail": "gh 未安装，无法认证"}
        )
    else:
        version = _run_command([gh_path, "--version"])
        checks.append(
            {
                "name": "gh",
                "status": "ok" if version.returncode == 0 else "error",
                "detail": _redact(_first_line(version)),
            }
        )
        auth = _run_command([gh_path, "auth", "status", "--hostname", "github.com"])
        combined = _redact((auth.stdout or "") + (auth.stderr or ""))
        account_match = re.search(r"Logged in to github\.com account ([^\s]+)", combined)
        account = account_match.group(1) if account_match else "unknown"
        if auth.returncode == 0:
            checks.append(
                {
                    "name": "gh_auth",
                    "status": "ok",
                    "detail": f"github.com account={account}",
                }
            )
        else:
            checks.append(
                {
                    "name": "gh_auth",
                    "status": "error",
                    "detail": _redact(_first_line(auth)) or "认证失败",
                }
            )

    repo_detail: dict[str, Any] | None = None
    if args.repo:
        repo = parse_repository(args.repo)
        repo_detail = {**repo.to_dict(), "access": "not_checked"}
        if gh_path:
            probe = probe_repository(gh_path, repo.owner, repo.name)
            repo_detail.update(
                {
                    "access": probe["status"],
                    "http_status": probe.get("http_status"),
                    "default_branch": probe.get("default_branch"),
                    "archived": probe.get("archived"),
                }
            )
            checks.append(
                {
                    "name": "repo_access",
                    "status": "ok" if probe["status"] == "ok" else "error",
                    "detail": (
                        f"access={probe['status']}"
                        f" default_branch={probe.get('default_branch')}"
                    ),
                }
            )
        else:
            repo_detail["access"] = "unavailable"
            checks.append(
                {
                    "name": "repo_access",
                    "status": "error",
                    "detail": "gh 未安装，无法检查仓库访问",
                }
            )

    critical = {"python", "jsonschema", "format_checkers", "gh", "gh_auth"}
    if repo_detail:
        critical.add("repo_access")
    degraded = any(check["status"] != "ok" and check["name"] in critical for check in checks)
    status = "degraded" if degraded else "ok"
    result = {
        "ok": True,
        "command": "doctor",
        "result": {"status": status, "checks": checks, "repo": repo_detail},
        "warnings": [] if not degraded else ["部分必需能力不可用"],
    }
    lines = [f"[{check['status']}] {check['name']}: {check['detail']}" for check in checks]
    if repo_detail:
        lines.append(f"repo: {repo_detail['canonical_url']} (access={repo_detail['access']})")
    return result, lines


def _normalize_format_arguments(arguments: list[str]) -> list[str]:
    """Allow --format before or after the subcommand."""
    remaining: list[str] = []
    format_arguments: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == "--format" and index + 1 < len(arguments):
            format_arguments.extend([value, arguments[index + 1]])
            index += 2
            continue
        if value.startswith("--format="):
            format_arguments.append(value)
            index += 1
            continue
        remaining.append(value)
        index += 1
    return format_arguments + remaining


def _peek_format(arguments: list[str]) -> str:
    selected = "json"
    for index, value in enumerate(arguments):
        if value == "--format" and index + 1 < len(arguments):
            selected = arguments[index + 1]
        elif value.startswith("--format="):
            selected = value.split("=", 1)[1]
    return selected


def _require_nonempty(name: str, value: str) -> None:
    if not value.strip():
        raise UsageError(f"--{name} 不能为空", source="cli")


def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(command, 1, "", f"{type(exc).__name__}: {exc}")


def _first_line(completed: subprocess.CompletedProcess[str]) -> str:
    text = ((completed.stdout or "") + (completed.stderr or "")).strip()
    return text.splitlines()[0][:300] if text else ""


def _redact(text: str) -> str:
    return redact_secrets(text)


def _emit_success(result: dict[str, Any], lines: list[str], output_format: str) -> None:
    if output_format == "text":
        print("\n".join(lines))
        for warning in result.get("warnings") or []:
            print(f"warning: {warning}", file=sys.stderr)
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _emit_error(command: str | None, error: RERError, output_format: str) -> None:
    if output_format == "text":
        print(f"error [{error.code}]: {error.message}", file=sys.stderr)
        if error.recovery:
            print(f"recovery: {error.recovery}", file=sys.stderr)
        return
    envelope = {"ok": False, "command": command, "error": error.to_dict()}
    print(json.dumps(envelope, ensure_ascii=False, indent=2))
    print(f"{error.code}: {error.message}", file=sys.stderr)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
