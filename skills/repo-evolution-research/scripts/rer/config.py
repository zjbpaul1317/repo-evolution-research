"""Research configuration: repository parsing, time windows, modes and budgets."""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import SCHEMA_VERSION, SKILL_ROOT
from .errors import ArtifactError, ConfigHashError, InternalError, RERError, UsageError
from .models import canonical_json, to_iso_z, validate_document

ASSET_DIR = SKILL_ROOT / "assets"
DEFAULT_CONFIG_PATH = ASSET_DIR / "config.default.json"

MODE_DEFAULTS: dict[str, dict[str, int]] = {
    "quick": {
        "days": 30,
        "max_deep_entities": 12,
        "max_deep_prs": 8,
        "agent_pr_reserve": 2,
    },
    "standard": {
        "days": 90,
        "max_deep_entities": 40,
        "max_deep_prs": 22,
        "agent_pr_reserve": 4,
    },
    "deep": {
        "days": 180,
        "max_deep_entities": 80,
        "max_deep_prs": 44,
        "agent_pr_reserve": 8,
    },
}
MODES = tuple(MODE_DEFAULTS)
MAX_DAYS = 3650
MAX_MONTHS = 120


@dataclass(frozen=True)
class BudgetField:
    """Single source of truth for a budget field.

    `default` is the standard-mode value. `mode_derived` fields are overridden
    by `MODE_DEFAULTS`; `required` controls the JSON Schema `required` list.
    """

    key: str
    default: int
    description: str
    required: bool = True
    mode_derived: bool = False


BUDGET_FIELDS: tuple[BudgetField, ...] = (
    BudgetField("max_requests", 400, "同一 run 累计 GitHub 请求数上限"),
    BudgetField("max_discovery_entities", 6000, "发现阶段实体上限"),
    BudgetField(
        "max_deep_entities",
        MODE_DEFAULTS["standard"]["max_deep_entities"],
        "同一 run 唯一深读实体上限（跨续跑累计，不重复计数；由 mode 覆盖）",
        mode_derived=True,
    ),
    BudgetField(
        "max_deep_prs",
        MODE_DEFAULTS["standard"]["max_deep_prs"],
        "同一 run 唯一深读 PR 上限（跨续跑累计；由 mode 覆盖）",
        required=False,
        mode_derived=True,
    ),
    BudgetField(
        "agent_pr_reserve",
        MODE_DEFAULTS["standard"]["agent_pr_reserve"],
        "初始 collect 为 Agent selection 保留的 PR 深读名额（由 mode 覆盖）",
        required=False,
        mode_derived=True,
    ),
    BudgetField("max_relation_hops", 2, "关系扩展跳数"),
    BudgetField("max_external_entities", 20, "阅读候选中的外部关联实体上限；离线引用索引完整保留"),
    BudgetField("max_response_bytes", 8388608, "单次响应字节上限"),
    BudgetField("max_total_bytes", 157286400, "同一 run 累计响应字节上限"),
    BudgetField("max_runtime_minutes", 35, "同一 run 累计运行时长上限（分钟，跨续跑累计）"),
    BudgetField("max_pack_chars", 100000, "报告 pack 字符上限"),
)
BUDGET_KEYS = tuple(field.key for field in BUDGET_FIELDS)
BUDGET_DEFAULTS = {field.key: field.default for field in BUDGET_FIELDS}

_SUPPORTED_HOSTS = {"github.com", "www.github.com"}
_OWNER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True)
class RepoRef:
    host: str
    owner: str
    name: str
    input: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def canonical_url(self) -> str:
        return f"https://{self.host}/{self.owner}/{self.name}"

    def to_dict(self) -> dict[str, str]:
        return {
            "input": self.input,
            "host": self.host,
            "owner": self.owner,
            "name": self.name,
            "slug": self.slug,
            "canonical_url": self.canonical_url,
        }


def parse_repository(value: str) -> RepoRef:
    """Accept https, git@, ssh and owner/repo forms; reject ambiguous paths."""
    if not isinstance(value, str) or not value.strip():
        raise UsageError("仓库地址不能为空", source="config.repository")

    raw = value.strip()
    host: str
    path: str

    if raw.lower().startswith("git@"):
        match = re.fullmatch(r"git@([^:]+):(.+)", raw)
        if not match:
            raise UsageError(
                f"无法解析仓库地址：{raw}",
                source="config.repository",
                recovery="使用 https://github.com/<owner>/<repo> 或 <owner>/<repo>",
            )
        host, path = match.group(1), match.group(2)
    elif "://" in raw:
        parsed = urlsplit(raw)
        if parsed.scheme not in ("http", "https", "ssh", "git"):
            raise UsageError(
                f"不支持的仓库协议：{parsed.scheme}",
                source="config.repository",
                recovery="使用 https://github.com/<owner>/<repo>",
            )
        has_credentials = bool(parsed.password)
        if parsed.scheme in ("http", "https"):
            has_credentials = has_credentials or bool(parsed.username)
        elif parsed.scheme in ("ssh", "git"):
            has_credentials = has_credentials or parsed.username not in (None, "git")
        if has_credentials:
            raise UsageError(
                "仓库地址不能包含用户名或令牌",
                source="config.repository",
                recovery="只提供 https://github.com/<owner>/<repo>",
            )
        if parsed.query or parsed.fragment:
            raise UsageError(
                "仓库地址不能包含查询参数或片段",
                source="config.repository",
                recovery="移除 ?token=... 或 #... 后重试",
            )
        host = parsed.hostname or ""
        path = parsed.path
    elif raw.lower().startswith("github.com/"):
        host, path = "github.com", raw[len("github.com/") :]
    else:
        host, path = "github.com", raw

    host = host.lower().strip()
    if host not in _SUPPORTED_HOSTS:
        raise UsageError(
            f"暂不支持该托管域名：{host or '<empty>'}",
            source="config.repository",
            recovery="V0.1 仅支持 github.com 仓库",
        )
    host = "github.com"

    path = path.strip().strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if any(part == "" for part in path.split("/")):
        raise UsageError(
            f"仓库路径包含空段：{raw}",
            source="config.repository",
            recovery="使用 https://github.com/<owner>/<repo>",
        )
    parts = [part for part in path.split("/") if part]
    if len(parts) != 2:
        raise UsageError(
            f"仓库路径必须为 <owner>/<repo>：{raw}",
            source="config.repository",
            recovery="使用 https://github.com/<owner>/<repo>",
        )

    owner, name = parts
    for label, part, pattern in (("owner", owner, _OWNER_RE), ("repo", name, _NAME_RE)):
        if not pattern.fullmatch(part) or part.endswith(".") or part in (".", ".."):
            raise UsageError(
                f"非法的仓库 {label}：{part}",
                source="config.repository",
                recovery="检查 GitHub 仓库地址是否完整",
            )
    if len(owner) > 39:
        raise UsageError("owner 长度超过 GitHub 限制", source="config.repository")
    if len(name) > 100:
        raise UsageError("repo 名称长度超过 GitHub 限制", source="config.repository")

    return RepoRef(host=host, owner=owner.lower(), name=name.lower(), input=raw)


@lru_cache(maxsize=1)
def _defaults_document() -> dict[str, Any]:
    if not DEFAULT_CONFIG_PATH.is_file():
        raise InternalError(f"缺少默认配置：{DEFAULT_CONFIG_PATH}", source="config.default")
    return json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))


def default_config() -> dict[str, Any]:
    return json.loads(json.dumps(_defaults_document()))


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise UsageError(
            f"未知时区：{name}",
            source="config.window.timezone",
            recovery="使用 IANA 时区名，例如 Asia/Shanghai 或 UTC",
        ) from exc


def parse_moment(value: str, tz: ZoneInfo) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise UsageError("时间不能为空", source="config.window")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise UsageError(
            f"无法解析时间：{value}",
            source="config.window",
            recovery="使用 ISO 8601，例如 2026-08-03T00:00:00Z 或 2026-08-03",
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    return moment.astimezone(UTC)


def subtract_months(moment: datetime, months: int) -> datetime:
    """Calendar-month subtraction with month-end clamping."""
    total = moment.year * 12 + (moment.month - 1) - months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def resolve_window(
    *,
    mode: str = "standard",
    days: int | None = None,
    months: int | None = None,
    start: str | None = None,
    end: str | None = None,
    timezone_name: str = "Asia/Shanghai",
    now: datetime | None = None,
) -> dict[str, Any]:
    if mode not in MODE_DEFAULTS:
        raise UsageError(f"未知研究模式：{mode}", source="config.mode")
    if days is not None and months is not None:
        raise UsageError("days 和 months 不能同时指定", source="config.window")
    for label, value, upper in (("days", days, MAX_DAYS), ("months", months, MAX_MONTHS)):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise UsageError(f"{label} 必须为正整数", source="config.window")
        if value > upper:
            raise UsageError(
                f"{label} 不能超过 {upper}",
                source="config.window",
                recovery=f"缩小时间窗口到 {upper} {label}以内",
            )

    for label, value in (("start", start), ("end", end)):
        if value is not None and not str(value).strip():
            raise UsageError(f"{label} 不能为空", source="config.window")

    tz = load_timezone(timezone_name)
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise UsageError("now 必须包含时区", source="config.window")
    current = current.astimezone(UTC).replace(microsecond=0)

    explicit_start = parse_moment(start, tz) if start else None
    explicit_end = parse_moment(end, tz) if end else None

    if explicit_start is not None and explicit_end is not None:
        if explicit_start >= explicit_end:
            raise UsageError("start 必须早于 end", source="config.window")
        expression = {"kind": "explicit", "days": None, "months": None}
        resolved_start, resolved_end = explicit_start, explicit_end
    else:
        resolved_end = explicit_end or current
        if explicit_start is not None:
            resolved_start = explicit_start
            expression = {"kind": "explicit", "days": None, "months": None}
        elif months is not None:
            try:
                local_end = resolved_end.astimezone(tz)
                shifted = subtract_months(local_end.replace(tzinfo=None), months)
                resolved_start = shifted.replace(tzinfo=tz).astimezone(UTC)
            except (OverflowError, ValueError) as exc:
                raise UsageError(
                    "时间窗口超出可表示范围",
                    source="config.window",
                    recovery="缩小 months 或调整 end",
                ) from exc
            expression = {"kind": "months", "days": None, "months": months}
        else:
            effective_days = days if days is not None else MODE_DEFAULTS[mode]["days"]
            try:
                resolved_start = resolved_end - timedelta(days=effective_days)
            except (OverflowError, ValueError) as exc:
                raise UsageError(
                    "时间窗口超出可表示范围",
                    source="config.window",
                    recovery="缩小 days 或调整 end",
                ) from exc
            expression = {"kind": "days", "days": effective_days, "months": None}
        if resolved_start >= resolved_end:
            raise UsageError("start 必须早于 end", source="config.window")

    duration_days = round((resolved_end - resolved_start).total_seconds() / 86400, 6)
    return {
        "start": to_iso_z(resolved_start),
        "end": to_iso_z(resolved_end),
        "timezone": timezone_name,
        "duration_days": duration_days,
        "expression": expression,
    }


def _merge_budget(
    mode: str,
    defaults: dict[str, Any],
    overrides: dict[str, Any] | None = None,
) -> dict[str, int]:
    budget = dict(defaults)
    for field in BUDGET_FIELDS:
        if field.mode_derived:
            budget[field.key] = MODE_DEFAULTS[mode][field.key]
    for key, value in (overrides or {}).items():
        if key not in BUDGET_KEYS:
            raise UsageError(f"未知预算字段：{key}", source="config.budget")
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise UsageError(
                f"预算 {key} 必须为正整数",
                source="config.budget",
                details={"value": value},
            )
        budget[key] = value
    for key in BUDGET_KEYS:
        value = budget.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise InternalError(f"无效的默认预算字段：{key}", source="config.default")
    return {key: budget[key] for key in BUDGET_KEYS}


def build_config(
    *,
    repository: str,
    mode: str = "standard",
    days: int | None = None,
    months: int | None = None,
    start: str | None = None,
    end: str | None = None,
    timezone_name: str = "Asia/Shanghai",
    focus: list[str] | None = None,
    compare_previous: bool = False,
    include_discussions: bool = True,
    include_docs: bool = True,
    language: str = "zh-CN",
    output_root: str = ".repo-research",
    budget_overrides: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    repo = parse_repository(repository)
    defaults = default_config()
    if mode not in MODE_DEFAULTS:
        raise UsageError(f"未知研究模式：{mode}", source="config.mode")
    if not isinstance(output_root, str) or not output_root.strip():
        raise UsageError("output_root 不能为空", source="config.output_root")
    if not isinstance(language, str) or not language.strip():
        raise UsageError("language 不能为空", source="config.language")

    window = resolve_window(
        mode=mode,
        days=days,
        months=months,
        start=start,
        end=end,
        timezone_name=timezone_name,
        now=now,
    )
    focus_items: list[str] = []
    for item in focus or []:
        cleaned = item.strip()
        if not cleaned:
            raise UsageError("focus 不能包含空字符串", source="config.focus")
        if cleaned not in focus_items:
            focus_items.append(cleaned)

    config: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "repository": repo.to_dict(),
        "mode": mode,
        "window": window,
        "focus": focus_items,
        "compare_previous": bool(compare_previous),
        "include_discussions": bool(include_discussions),
        "include_docs": bool(include_docs),
        "language": language,
        "output_root": output_root,
        "budget": _merge_budget(mode, defaults["budget"], budget_overrides),
    }
    config["config_hash"] = compute_config_hash(config)
    validate_document(config, "config")
    return config


def compute_config_hash(config: dict[str, Any]) -> str:
    """Hash normalized identity and research options, not input spelling or paths."""
    stable = {
        key: value
        for key, value in config.items()
        if key not in ("config_hash", "output_root")
    }
    repository = stable.get("repository")
    if isinstance(repository, dict):
        stable["repository"] = {
            "host": str(repository.get("host", "")).lower(),
            "owner": str(repository.get("owner", "")).casefold(),
            "name": str(repository.get("name", "")).casefold(),
        }
    return hashlib.sha256(canonical_json(stable).encode("utf-8")).hexdigest()


def read_resolved_config(path: Path, *, artifact: bool = False) -> dict[str, Any]:
    try:
        try:
            document = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(
                f"无法读取配置：{path}",
                source="config.read",
                details={"reason": str(exc)},
            ) from exc
        found_version = (
            document.get("schema_version") if isinstance(document, dict) else None
        )
        if found_version != SCHEMA_VERSION and not (
            artifact and found_version == "1.2"
        ):
            raise UsageError(
                f"配置 schema_version 不兼容：{found_version!r} != {SCHEMA_VERSION}",
                source="config.schema_version",
                details={"found": found_version, "expected": SCHEMA_VERSION},
                recovery="旧版本 run 不兼容；请重新运行 init",
            )
        validate_document(document, "config")
        expected = document["config_hash"]
        actual = compute_config_hash(document)
        if expected != actual:
            raise ConfigHashError(
                "config_hash 与配置内容不一致",
                source="config.hash",
                details={"expected": expected, "actual": actual},
                recovery="不要手工修改 resolved config；重新运行 init",
            )
        return document
    except RERError as exc:
        if not artifact:
            raise
        if exc.source == "config.schema_version":
            raise ArtifactError(
                "旧快照不兼容：配置 schema_version 与当前版本不一致",
                source="config.schema_version",
                details=exc.details,
                recovery="旧 0.2.x run 需要重新运行 init，不能继续写入",
            ) from exc
        raise ArtifactError(
            f"快照配置损坏：{path.name}",
            source="config.artifact",
            details={"reason": exc.message, "code": exc.code},
            recovery="保留快照用于诊断；不要继续写入该 run",
        ) from exc


__all__ = [
    "BUDGET_DEFAULTS",
    "BUDGET_FIELDS",
    "BUDGET_KEYS",
    "BudgetField",
    "DEFAULT_CONFIG_PATH",
    "MAX_DAYS",
    "MAX_MONTHS",
    "MODE_DEFAULTS",
    "MODES",
    "RepoRef",
    "build_config",
    "compute_config_hash",
    "default_config",
    "load_timezone",
    "parse_moment",
    "parse_repository",
    "read_resolved_config",
    "resolve_window",
    "subtract_months",
]
