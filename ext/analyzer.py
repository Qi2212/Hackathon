"""Task Analyzer：自然语言 -> TaskSpec。

分层策略（与分工文档一致：LLM 只负责理解，不负责承诺资源）：

1. 确定性规则先解析用户原文里的显式信息（型号、张数、显存、时长、截止、预算、中断要求）；
2. LLM 只对仍然缺失的字段做推断，且 GPU 型号必须落在候选集合内；
3. 策略默认值最后补齐，并在 provenance 里标成 default；
4. 缺失的关键字段生成追问，`status` 进入 `needs_confirmation` 或 `draft`，
   只有显式确认接口才能把状态改为 `confirmed`。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from ext.llm import LLMProvider, LLMResult, NoopProvider
from ext.schemas import (
    CONTRACT_VERSION,
    FieldProvenance,
    MissingField,
    TaskSpec,
    TaskType,
)

DEFAULT_KNOWN_MODELS = ["H100", "A100", "V100", "L40S", "A10", "RTX 4090", "RTX 3090", "MI300"]

TASK_TYPES: set[str] = {
    "training",
    "fine_tuning",
    "inference",
    "data_processing",
    "simulation",
    "notebook",
    "unknown",
}

# 正则别名 -> 规范型号（长的放前面，避免 a100 被 a10 抢先匹配）
GPU_MODEL_ALIASES: list[tuple[str, str]] = [
    ("rtx 4090", "RTX 4090"),
    ("rtx4090", "RTX 4090"),
    ("4090", "RTX 4090"),
    ("rtx 3090", "RTX 3090"),
    ("rtx3090", "RTX 3090"),
    ("3090", "RTX 3090"),
    ("h100", "H100"),
    ("a100", "A100"),
    ("v100", "V100"),
    ("l40s", "L40S"),
    ("mi300", "MI300"),
    ("a10", "A10"),
]

_MODEL_PATTERN = "|".join(re.escape(alias) for alias, _ in GPU_MODEL_ALIASES)

_TASK_PATTERNS: list[tuple[str, TaskType]] = [
    (r"微调|fine[- ]?tun|lora|qlora|sft", "fine_tuning"),
    (r"预训练|训练|pretrain|training|train", "training"),
    (r"推理|inference|serving|部署|上线", "inference"),
    (r"数据清洗|数据处理|数据预处理|清洗|预处理|etl|标注", "data_processing"),
    (r"仿真|simulat|渲染|render", "simulation"),
    (r"notebook|jupyter|交互式|探索性分析", "notebook"),
]

_NUM = r"(\d+(?:\.\d+)?)"
_UNIT_GB = r"(g|gb|gib)"

_WEEKDAYS = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_INT_LIMITS: dict[str, tuple[int, int]] = {
    "resources.gpu_count": (0, 16),
    "resources.min_gpu_memory_mib": (0, 1_048_576),
    "resources.cpu_millicores": (0, 1_000_000),
    "resources.memory_mib": (0, 16_777_216),
    "resources.ephemeral_storage_mib": (0, 104_857_600),
    "duration.expected_seconds": (0, 31_536_000),
    "interruption.max_restarts": (0, 20),
}

_ANSWER_PATHS: dict[str, str] = {
    "task_type": "task_type",
    "gpu_count": "resources.gpu_count",
    "gpu_models": "resources.gpu_models",
    "min_gpu_memory_mib": "resources.min_gpu_memory_mib",
    "cpu_millicores": "resources.cpu_millicores",
    "memory_mib": "resources.memory_mib",
    "ephemeral_storage_mib": "resources.ephemeral_storage_mib",
    "duration_seconds": "duration.expected_seconds",
    "deadline": "timing.deadline",
    "urgent": "timing.urgent",
    "max_cost": "budget.max_cost",
    "currency": "budget.currency",
    "interruptible": "interruption.interruptible",
    "checkpointable": "interruption.checkpointable",
    "max_restarts": "interruption.max_restarts",
}


class BlockingAnswersMissing(Exception):
    def __init__(self, missing: list[MissingField]) -> None:
        self.missing = missing
        self.fields = [item.field for item in missing]
        super().__init__(f"blocking fields missing: {self.fields}")


@dataclass
class AnalysisResult:
    spec: TaskSpec
    llm: dict[str, Any]
    known_models: list[str]


@dataclass
class _Extracted:
    values: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)

    def put(self, path: str, value: Any, evidence: str) -> None:
        if path in self.values:
            return
        self.values[path] = value
        self.evidence[path] = evidence[:80]


# ---------------------------------------------------------------------------
# 确定性解析
# ---------------------------------------------------------------------------


def _first(pattern: str, text: str) -> re.Match[str] | None:
    return re.search(pattern, text, re.IGNORECASE)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _deadline_from(text: str, now: datetime) -> datetime | None:
    match = _first(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        return _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))

    match = _first(r"(?:截止|deadline|最晚|before)\D{0,6}(\d{1,2})[-/](\d{1,2})", text)
    if match:
        return _safe_date(now.year, int(match.group(1)), int(match.group(2)), after=now)

    match = _first(r"(\d{1,2})月(\d{1,2})[日号]", text)
    if match:
        return _safe_date(now.year, int(match.group(1)), int(match.group(2)), after=now)

    match = _first(r"(今天|明天|后天)", text)
    if match:
        offset = {"今天": 0, "明天": 1, "后天": 2}[match.group(1)]
        return (now + timedelta(days=offset)).replace(hour=23, minute=59, second=0, microsecond=0)

    match = _first(r"(本周|这周|下周|周)([一二三四五六日天])", text)
    if match:
        prefix, weekday = match.group(1), match.group(2)
        target = _WEEKDAYS[weekday]
        today = now.weekday()
        if "下" in prefix:
            delta = 7 - today + target
        elif prefix in ("本周", "这周"):
            delta = target - today
            if delta < 0:
                delta += 7
        else:
            delta = (target - today) % 7 or 7
        return (now + timedelta(days=delta)).replace(hour=23, minute=59, second=0, microsecond=0)

    match = _first(r"(\d+)\s*(天|小时)\s*内", text)
    if match:
        amount = int(match.group(1))
        delta = timedelta(days=amount) if match.group(2) == "天" else timedelta(hours=amount)
        return now + delta
    return None


def _safe_date(year: int, month: int, day: int, after: datetime | None = None) -> datetime | None:
    try:
        candidate = datetime(year, month, day, 23, 59, tzinfo=timezone.utc)
    except ValueError:
        return None
    if after is not None and candidate < after:
        try:
            candidate = candidate.replace(year=year + 1)
        except ValueError:
            return None
    return candidate


def _extract(text: str, now: datetime) -> _Extracted:
    out = _Extracted()
    lowered = text.lower()

    for pattern, task_type in _TASK_PATTERNS:
        match = _first(pattern, lowered)
        if match:
            out.put("task_type", task_type, match.group(0))
            break

    for alias, canonical in GPU_MODEL_ALIASES:
        match = _first(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered)
        if match:
            models = out.values.setdefault("resources.gpu_models", [])
            if canonical not in models:
                models.append(canonical)
            out.evidence.setdefault("resources.gpu_models", match.group(0))

    count_patterns = [
        rf"(\d+)\s*[x×]\s*(?:张|块|卡)?\s*(?:{_MODEL_PATTERN})",
        rf"(?:{_MODEL_PATTERN})\s*[x×]\s*(\d+)",
        r"(\d+)\s*(?:张|块|卡)",
        r"(?:需要|使用|占用|申请|要)\s*(\d+)\s*(?:张|块|卡)",
    ]
    for pattern in count_patterns:
        match = _first(pattern, lowered)
        if match:
            out.put("resources.gpu_count", _clamp(int(match.group(1)), 0, 16), match.group(0))
            break
    else:
        numerals = {"单卡": 1, "单张": 1, "双卡": 2, "两张卡": 2, "两卡": 2, "四卡": 4, "四张": 4, "八卡": 8}
        for word, value in numerals.items():
            if word in lowered:
                out.put("resources.gpu_count", value, word)
                break

    match = _first(rf"(?:显存|vram)\D{{0,8}}{_NUM}\s*{_UNIT_GB}", lowered)
    if not match:
        match = _first(rf"{_NUM}\s*{_UNIT_GB}\s*(?:显存|vram)", lowered)
    if match:
        out.put("resources.min_gpu_memory_mib", round(float(match.group(1)) * 1024), match.group(0))

    match = _first(rf"{_NUM}\s*(?:核|vcpu|cores?)(?![a-z])", lowered)
    if match:
        out.put("resources.cpu_millicores", round(float(match.group(1)) * 1000), match.group(0))

    match = _first(rf"(?:内存|ram|memory)\D{{0,6}}{_NUM}\s*{_UNIT_GB}", lowered)
    if not match:
        match = _first(rf"{_NUM}\s*{_UNIT_GB}\s*(?:内存|ram|memory)", lowered)
    if match:
        out.put("resources.memory_mib", round(float(match.group(1)) * 1024), match.group(0))

    match = _first(rf"{_NUM}\s*(tb|tib|t|gb|gib|g)\s*(?:的)?\s*(?:数据|数据集|存储|磁盘|空间)", lowered)
    if match:
        unit = match.group(2).lower()
        factor = 1024 * 1024 if unit.startswith("t") else 1024
        out.put("resources.ephemeral_storage_mib", round(float(match.group(1)) * factor), match.group(0))

    match = _first(rf"{_NUM}\s*(?:个)?\s*(?:小时|hour|hours|hrs?|h)(?![a-z])", lowered)
    if match:
        out.put("duration.expected_seconds", round(float(match.group(1)) * 3600), match.group(0))
    else:
        match = _first(rf"{_NUM}\s*(?:分钟|min|mins|minute|minutes)(?![a-z])", lowered)
        if match:
            out.put("duration.expected_seconds", round(float(match.group(1)) * 60), match.group(0))
        else:
            match = _first(rf"{_NUM}\s*(?:天|day|days)(?!\s*内)(?![a-z])", lowered)
            if match:
                out.put("duration.expected_seconds", round(float(match.group(1)) * 86400), match.group(0))

    deadline = _deadline_from(text, now)
    if deadline is not None:
        out.put("timing.deadline", _iso(deadline), deadline.strftime("%Y-%m-%d %H:%M"))

    match = _first(r"(?:预算|费用|花费|成本|budget|cost)\D{0,10}(\$|¥|￥)?\s*(\d+(?:\.\d+)?)", lowered)
    if not match:
        match = _first(r"(\d+(?:\.\d+)?)\s*(?:元|块钱)", lowered)
    if match:
        groups = match.groups()
        symbol, number = (groups[0], groups[1]) if len(groups) >= 2 else (None, groups[0])
        out.put("budget.max_cost", float(number), match.group(0))
        out.put("budget.currency", "USD" if symbol == "$" else "CNY", match.group(0))

    if _first(r"不能中断|不可中断|不可抢占|不支持抢占|不允许抢占|non[- ]?preemptible|must not be (?:interrupted|preempted)|cannot be interrupted", lowered):
        out.put("interruption.interruptible", False, "不可中断")
    elif _first(r"可以中断|可抢占|preemptible|可以被抢占", lowered):
        out.put("interruption.interruptible", True, "可抢占")

    if _first(r"不能断点|无法\s*checkpoint|不支持\s*checkpoint|no checkpoint", lowered):
        out.put("interruption.checkpointable", False, "不能断点")
    elif _first(r"断点续训|checkpoint|断点恢复|可以中断", lowered):
        out.put("interruption.checkpointable", True, "断点续训")

    if _first(r"尽快|asap|立刻|马上|紧急|urgent", lowered):
        out.put("timing.urgent", True, "尽快")

    return out


# ---------------------------------------------------------------------------
# LLM 建议解析（宽容 + 白名单）
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> int | float:
    return max(low, min(high, value))


def _as_int(value: Any, low: int, high: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = int(value)
    if number < low or number > high:
        return None
    return number


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _canonical_model(name: str, known_models: Iterable[str]) -> str | None:
    target = re.sub(r"\s+", " ", name.strip().lower())
    for known in known_models:
        if known.strip().lower() == target:
            return known
    for alias, canonical in GPU_MODEL_ALIASES:
        if alias == target and canonical in set(known_models):
            return canonical
    return None


def _parse_suggestion(data: dict[str, Any], known_models: list[str], now: datetime) -> dict[str, Any]:
    """把 LLM 输出过滤成可安全落库的字段；型号必须在候选集合内。"""
    suggestion: dict[str, Any] = {}

    task_type = data.get("task_type")
    if isinstance(task_type, str) and task_type in TASK_TYPES and task_type != "unknown":
        suggestion["task_type"] = task_type

    count = _as_int(data.get("gpu_count"), 0, 16)
    if count:
        suggestion["resources.gpu_count"] = count

    raw_models = data.get("gpu_models")
    if isinstance(raw_models, list):
        canonical: list[str] = []
        for item in raw_models:
            if isinstance(item, str):
                matched = _canonical_model(item, known_models)
                if matched and matched not in canonical:
                    canonical.append(matched)
        if canonical:
            suggestion["resources.gpu_models"] = canonical

    int_fields = {
        "min_gpu_memory_mib": ("resources.min_gpu_memory_mib", 0, 1_048_576),
        "cpu_millicores": ("resources.cpu_millicores", 0, 1_000_000),
        "memory_mib": ("resources.memory_mib", 0, 16_777_216),
        "ephemeral_storage_mib": ("resources.ephemeral_storage_mib", 0, 104_857_600),
        "duration_seconds": ("duration.expected_seconds", 0, 31_536_000),
    }
    for key, (path, low, high) in int_fields.items():
        parsed = _as_int(data.get(key), low, high)
        if parsed:
            suggestion[path] = parsed

    deadline_raw = data.get("deadline")
    if isinstance(deadline_raw, str):
        deadline = _parse_iso(deadline_raw)
        if deadline is not None and deadline > now:
            suggestion["timing.deadline"] = _iso(deadline)

    budget = data.get("budget_max")
    if isinstance(budget, (int, float)) and not isinstance(budget, bool) and 0 < float(budget) <= 1_000_000_000:
        suggestion["budget.max_cost"] = float(budget)
        currency = data.get("currency")
        if currency in ("CNY", "USD"):
            suggestion["budget.currency"] = currency

    for key, path in (
        ("interruptible", "interruption.interruptible"),
        ("checkpointable", "interruption.checkpointable"),
        ("urgent", "timing.urgent"),
    ):
        flag = _as_bool(data.get(key))
        if flag is not None:
            suggestion[path] = flag

    confidence = data.get("confidence")
    suggestion["_confidence"] = (
        float(_clamp(confidence, 0.2, 0.75)) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 0.5
    )
    return suggestion


# ---------------------------------------------------------------------------
# Spec 组装 / 确认
# ---------------------------------------------------------------------------


def _assign(spec: TaskSpec, path: str, value: Any) -> None:
    parts = path.split(".")
    target: Any = spec
    for part in parts[:-1]:
        target = getattr(target, part)
    setattr(target, parts[-1], value)


def _mark(
    provenance: list[FieldProvenance],
    path: str,
    source: str,
    confidence: float,
    evidence: str | None = None,
) -> None:
    provenance[:] = [item for item in provenance if item.field != path]
    provenance.append(
        FieldProvenance(
            field=path,
            source=source,  # type: ignore[arg-type]
            confidence=round(confidence, 2),
            confirmed=source != "inferred",
            evidence=evidence,
        )
    )


def _apply_suggestion(
    spec: TaskSpec,
    suggestion: dict[str, Any],
    present: set[str],
    known_models: list[str],
    provenance: list[FieldProvenance],
) -> None:
    confidence = suggestion.pop("_confidence", 0.5)
    for path, value in suggestion.items():
        if path in present:
            continue
        _assign(spec, path, value)
        _mark(provenance, path, "inferred", confidence, "LLM 推断")
        present.add(path)


def _apply_defaults(spec: TaskSpec, provenance: list[FieldProvenance]) -> None:
    if not any(item.field == "interruption.interruptible" for item in provenance):
        spec.interruption.interruptible = True
        _mark(provenance, "interruption.interruptible", "default", 1.0, "默认允许抢占")
    spec.interruption.max_restarts = 0 if not spec.interruption.interruptible else (spec.interruption.max_restarts or 2)
    _mark(provenance, "interruption.max_restarts", "default", 1.0, "由可中断性推导")
    if not any(item.field == "interruption.checkpointable" for item in provenance):
        spec.interruption.checkpointable = True
        _mark(provenance, "interruption.checkpointable", "default", 1.0, "默认支持断点")
    if not any(item.field == "budget.currency" for item in provenance):
        spec.budget.currency = "CNY"
        _mark(provenance, "budget.currency", "default", 1.0, "默认人民币")


def _missing_fields(spec: TaskSpec) -> list[MissingField]:
    missing: list[MissingField] = []
    if spec.task_type == "unknown":
        missing.append(
            MissingField(
                field="task_type",
                question="这个任务更接近训练、微调、推理、数据处理还是仿真？",
                importance="blocking",
            )
        )
    if spec.task_type in ("training", "fine_tuning") and spec.resources.gpu_count <= 0:
        missing.append(
            MissingField(
                field="resources.gpu_count",
                question="需要几张 GPU？",
                importance="blocking",
                suggested_default="1",
            )
        )
    if spec.duration.expected_seconds <= 0:
        missing.append(
            MissingField(
                field="duration.expected_seconds",
                question="预计运行多久（小时或天）？",
                importance="recommended",
                suggested_default="2 小时",
            )
        )
    if spec.resources.gpu_count > 0 and not spec.resources.gpu_models:
        missing.append(
            MissingField(
                field="resources.gpu_models",
                question="对 GPU 型号有偏好吗（如 H100 / A100 / RTX 4090）？",
                importance="recommended",
            )
        )
    if spec.budget.max_cost is None:
        missing.append(
            MissingField(field="budget.max_cost", question="有预算上限吗？", importance="optional")
        )
    if spec.timing.deadline is None:
        missing.append(
            MissingField(field="timing.deadline", question="最晚什么时候要跑完？", importance="optional")
        )
    return missing


def _finalize(spec: TaskSpec, provenance: list[FieldProvenance]) -> None:
    spec.missing = _missing_fields(spec)
    substantive = [item.confidence for item in provenance if item.source != "default"]
    confidence = sum(substantive) / len(substantive) if substantive else 0.2
    if any(item.importance == "blocking" for item in spec.missing):
        confidence = min(confidence, 0.5)
    spec.confidence = round(confidence, 2)
    spec.provenance = sorted(provenance, key=lambda item: item.field)
    spec.status = "draft" if spec.task_type == "unknown" else "needs_confirmation"


class TaskAnalyzer:
    def __init__(
        self,
        llm: LLMProvider | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.llm = llm or NoopProvider()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def analyze(
        self,
        text: str,
        *,
        known_models: Iterable[str] | None = None,
        task_id: str | None = None,
        now: datetime | None = None,
    ) -> AnalysisResult:
        moment = now or self._clock()
        models = _normalize_models(known_models)
        extracted = _extract(text, moment)

        llm_result = LLMResult(None, self.llm.name, None)
        suggestion: dict[str, Any] = {}
        if text.strip():
            llm_result = self.llm.suggest(text, models)
            if llm_result.data:
                suggestion = _parse_suggestion(llm_result.data, models, moment)

        spec = TaskSpec(
            contract_version=CONTRACT_VERSION,
            task_id=task_id or f"task_{uuid.uuid4().hex[:12]}",
            raw_input=text,
            summary=text.strip()[:120],
            created_at=_iso(moment),
        )
        provenance: list[FieldProvenance] = []
        for path, value in extracted.values.items():
            _assign(spec, path, value)
            _mark(provenance, path, "user_explicit", 0.95, extracted.evidence.get(path))

        if suggestion:
            _apply_suggestion(spec, suggestion, set(extracted.values), models, provenance)

        _apply_defaults(spec, provenance)
        _finalize(spec, provenance)

        return AnalysisResult(
            spec=spec,
            llm={"provider": llm_result.provider, "used": llm_result.used, "error": llm_result.error},
            known_models=models,
        )

    def confirm(
        self,
        spec: TaskSpec,
        answers: dict[str, Any],
        confirm: bool,
    ) -> tuple[TaskSpec, list[str]]:
        updated = spec.model_copy(deep=True)
        provenance = list(updated.provenance)
        changed: list[str] = []

        for key, raw_value in answers.items():
            path = _ANSWER_PATHS.get(key)
            if path is None:
                continue
            value = _coerce_answer(key, raw_value)
            _assign(updated, path, value)
            _mark(provenance, path, "user_explicit", 1.0, "用户确认")
            changed.append(key)

        try:
            updated = TaskSpec.model_validate(updated.model_dump())
        except ValueError as exc:
            raise ValueError(f"答案超出契约范围: {exc}") from exc
        updated.provenance = provenance

        if not updated.interruption.interruptible:
            updated.interruption.max_restarts = 0
        elif updated.interruption.max_restarts == 0 and "max_restarts" not in answers:
            updated.interruption.max_restarts = 2
        _mark(provenance, "interruption.max_restarts", "default", 1.0, "由可中断性推导")

        _finalize(updated, provenance)
        blocking = [item for item in updated.missing if item.importance == "blocking"]
        if confirm:
            if blocking:
                raise BlockingAnswersMissing(blocking)
            updated.status = "confirmed"
        elif updated.status == "confirmed":
            updated.status = "needs_confirmation"
        return updated, changed


def _coerce_answer(key: str, value: Any) -> Any:
    if key == "task_type":
        if value not in TASK_TYPES:
            raise ValueError(f"task_type 必须是 {sorted(TASK_TYPES)} 之一")
        return value
    if key == "gpu_models":
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("gpu_models 必须是字符串数组")
        return value
    if key == "deadline":
        if value is None:
            return None
        if not isinstance(value, str) or _parse_iso(value) is None:
            raise ValueError("deadline 必须是 ISO-8601 时间")
        return _iso(_parse_iso(value))  # type: ignore[arg-type]
    if key == "currency":
        if value not in ("CNY", "USD"):
            raise ValueError("currency 只能是 CNY 或 USD")
        return value
    if key in {"interruptible", "checkpointable", "urgent"}:
        if not isinstance(value, bool):
            raise ValueError(f"{key} 必须是布尔值")
        return value
    if key == "max_cost":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("max_cost 必须是非负数")
        return float(value)
    path = _ANSWER_PATHS[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) < 0:
        raise ValueError(f"{key} 必须是非负整数")
    low, high = _INT_LIMITS.get(path, (0, 10**9))
    number = int(value)
    if number < low or number > high:
        raise ValueError(f"{key} 必须在 {low}..{high} 之间")
    return number


def _normalize_models(models: Iterable[str] | None) -> list[str]:
    if models:
        seen: list[str] = []
        for model in models:
            if isinstance(model, str) and model.strip() and model not in seen:
                seen.append(model)
        if seen:
            return seen
    return list(DEFAULT_KNOWN_MODELS)
