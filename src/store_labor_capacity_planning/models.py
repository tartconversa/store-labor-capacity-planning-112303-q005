"""业务数据模型。

模型保持输入字段的业务含义：缺失、未覆盖、待审、拒绝和未知都是独立状态，
不会被折叠成 0 或成功。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Iterable, Mapping


# ---------------------------------------------------------------------------
# 枚举型字符串与规范化
# ---------------------------------------------------------------------------

AVAILABILITY_STATES = ("available", "preferred", "unavailable", "unknown")
ACCEPTANCE_STATES = ("proposed", "accepted", "declined", "cancelled", "unknown")
ACTIVITY_KINDS = ("promotion", "closure", "other")

_AVAILABILITY_ALIASES = {
    "available": "available",
    "preferred": "preferred",
    "unavailable": "unavailable",
    "unknown": "unknown",
    "": "unknown",
    "可排": "available",
    "可用": "available",
    "优先": "preferred",
    "优先可排": "preferred",
    "不可用": "unavailable",
    "未知": "unknown",
}

_ACCEPTANCE_ALIASES = {
    "proposed": "proposed",
    "pending": "proposed",
    "draft": "proposed",
    "待确认": "proposed",
    "accepted": "accepted",
    "confirmed": "accepted",
    "accepted-by-employee": "accepted",
    "已接受": "accepted",
    "declined": "declined",
    "rejected": "declined",
    "已拒绝": "declined",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "已取消": "cancelled",
    "unknown": "unknown",
    "": "unknown",
}

_ACTIVITY_KIND_ALIASES = {
    "promotion": "promotion",
    "promo": "promotion",
    "event": "promotion",
    "促销": "promotion",
    "活动": "promotion",
    "closure": "closure",
    "closed": "closure",
    "temporary-closure": "closure",
    "闭店": "closure",
    "临时闭店": "closure",
    "other": "other",
    "其他": "other",
    "": "other",
}


def normalize_availability_state(value: str | None) -> str:
    return _AVAILABILITY_ALIASES.get((value or "").strip().lower(), "unknown")


def normalize_acceptance_state(value: str | None) -> str:
    return _ACCEPTANCE_ALIASES.get((value or "").strip().lower(), "unknown")


def normalize_activity_kind(value: str | None) -> str:
    return _ACTIVITY_KIND_ALIASES.get((value or "").strip().lower(), "other")


def normalize_preference(value: str | None) -> str:
    text = (value or "").strip().lower()
    return text if text in {"morning", "evening", "none"} else (text or "none")


# ---------------------------------------------------------------------------
# 输入实体
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemandObservation:
    store_id: str
    date: dt.date
    hour: int
    transactions: float | None
    traffic_count: float | None
    traffic_coverage: float | None
    activity_id: str | None
    row: int | None = None

    @property
    def traffic_usable(self) -> bool:
        """只有覆盖率为正且计数存在时，客流才可作为观测值。"""

        return (
            self.traffic_count is not None
            and self.traffic_coverage is not None
            and self.traffic_coverage > 0
        )

    @property
    def estimated_traffic(self) -> float | None:
        """按覆盖率放大的整点客流估计。

        覆盖率为 0 或缺失时不推算：缺失不等于没有顾客。
        """

        if not self.traffic_usable:
            return None
        return self.traffic_count / self.traffic_coverage  # type: ignore[operator]

    @property
    def traffic_state(self) -> str:
        if self.traffic_count is None:
            return "missing"
        if self.traffic_coverage is None:
            return "coverage-unknown"
        if self.traffic_coverage <= 0:
            return "uncovered"
        return "observed"

    @property
    def conversion(self) -> float | None:
        """客流到交易的转化率；仅在客流可用且交易存在时计算。"""

        traffic = self.estimated_traffic
        if traffic is None or traffic <= 0 or self.transactions is None:
            return None
        return self.transactions / traffic


@dataclass(frozen=True)
class Employee:
    employee_id: str
    store_id: str
    role: str
    skill: str
    contract_hours: float
    region: str
    row: int | None = None


@dataclass(frozen=True)
class Availability:
    employee_id: str
    date: dt.date
    start_hour: int
    end_hour: int
    state: str
    preference: str
    row: int | None = None

    @property
    def is_available(self) -> bool:
        return self.state in {"available", "preferred"}

    @property
    def is_preferred(self) -> bool:
        return self.state == "preferred"

    @property
    def hours(self) -> float:
        return max(0, self.end_hour - self.start_hour)

    def covers(self, start_hour: float, end_hour: float) -> bool:
        return self.start_hour <= start_hour and end_hour <= self.end_hour

    def overlaps(self, start_hour: float, end_hour: float) -> bool:
        return self.start_hour < end_hour and start_hour < self.end_hour


@dataclass(frozen=True)
class Shift:
    shift_id: str
    store_id: str
    date: dt.date
    role: str
    start_hour: int
    end_hour: int
    assigned_employee: str
    acceptance_state: str = "proposed"
    origin: str = "imported"
    scenario: str | None = None
    revision: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)
    replacement_for: str | None = None
    requires_renegotiation: bool = False

    @property
    def hours(self) -> float:
        return max(0, self.end_hour - self.start_hour)

    @property
    def is_accepted(self) -> bool:
        return self.acceptance_state == "accepted"

    @property
    def is_declined(self) -> bool:
        return self.acceptance_state in {"declined", "cancelled"}

    @property
    def is_proposed(self) -> bool:
        return self.acceptance_state == "proposed"

    @property
    def is_commitment(self) -> bool:
        return self.acceptance_state == "accepted"

    def with_updates(self, **kwargs) -> "Shift":
        return replace(self, **kwargs)


@dataclass(frozen=True)
class ActualCoverage:
    shift_id: str
    clocked_minutes: int | None
    task_load: float | None
    service_wait_minutes: float | None
    variance_reason: str
    row: int | None = None

    @property
    def clocked_hours(self) -> float | None:
        if self.clocked_minutes is None:
            return None
        return self.clocked_minutes / 60.0


@dataclass(frozen=True)
class Activity:
    activity_id: str
    store_id: str
    date: dt.date
    kind: str
    start_hour: int
    end_hour: int
    uplift: float | None = None
    note: str = ""
    row: int | None = None

    @property
    def is_closure(self) -> bool:
        return self.kind == "closure"

    def covers_hour(self, hour: int) -> bool:
        return self.start_hour <= hour < self.end_hour


@dataclass(frozen=True)
class Budget:
    region: str
    period_start: dt.date
    period_end: dt.date
    amount: float
    currency: str = "元"
    row: int | None = None

    def covers(self, date: dt.date) -> bool:
        return self.period_start <= date <= self.period_end


# ---------------------------------------------------------------------------
# 输出结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForecastCell:
    store_id: str
    date: dt.date
    hour: int
    expected_traffic: float | None
    expected_transactions: float | None
    traffic_lower: float | None
    traffic_upper: float | None
    confidence: float
    open: bool = True
    activity_id: str | None = None
    traffic_basis: str = "estimated"
    notes: tuple[str, ...] = field(default_factory=tuple)

    def to_row(self) -> dict:
        return {
            "store_id": self.store_id,
            "date": self.date.isoformat(),
            "hour": self.hour,
            "open": "true" if self.open else "false",
            "expected_traffic": _fmt(self.expected_traffic),
            "traffic_lower": _fmt(self.traffic_lower),
            "traffic_upper": _fmt(self.traffic_upper),
            "expected_transactions": _fmt(self.expected_transactions),
            "confidence": f"{self.confidence:.2f}",
            "traffic_basis": self.traffic_basis,
            "activity_id": self.activity_id or "",
            "notes": " | ".join(self.notes),
        }


@dataclass
class DayForecast:
    store_id: str
    date: dt.date
    open_hours: tuple[int, ...]
    closed_hours: tuple[int, ...]
    cells: list[ForecastCell]
    activity_ids: tuple[str, ...] = ()
    confidence: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def expected_traffic(self) -> float:
        return sum(c.expected_traffic or 0.0 for c in self.cells if c.open)

    @property
    def expected_transactions(self) -> float:
        return sum(c.expected_transactions or 0.0 for c in self.cells if c.open)


@dataclass
class ForecastResult:
    days: list[DayForecast]
    issues: list = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)

    def by_store_date(self) -> dict[tuple[str, dt.date], DayForecast]:
        return {(d.store_id, d.date): d for d in self.days}

    @property
    def cells(self) -> list[ForecastCell]:
        return [cell for day in self.days for cell in day.cells]


@dataclass(frozen=True)
class RoleRequirement:
    store_id: str
    date: dt.date
    role: str
    by_hour: Mapping[int, float]
    priority: float = 1.0
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_hours(self) -> float:
        return sum(self.by_hour.values())

    @property
    def hours(self) -> tuple[int, ...]:
        return tuple(sorted(self.by_hour))


@dataclass
class RequirementSet:
    requirements: list[RoleRequirement]
    issues: list = field(default_factory=list)

    def for_day(self, store_id: str, date: dt.date) -> list[RoleRequirement]:
        return [
            r
            for r in self.requirements
            if r.store_id == store_id and r.date == date
        ]

    def by_store_date_role(self) -> dict[tuple[str, dt.date, str], RoleRequirement]:
        return {
            (r.store_id, r.date, r.role): r for r in self.requirements
        }

    @property
    def total_hours(self) -> float:
        return sum(r.total_hours for r in self.requirements)


@dataclass
class ScenarioPlan:
    key: str
    name: str
    description: str
    shifts: list[Shift]
    issues: list = field(default_factory=list)
    revision: int = 0
    change_log: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def accepted_shifts(self) -> list[Shift]:
        return [s for s in self.shifts if s.is_accepted]

    def proposed_shifts(self) -> list[Shift]:
        return [s for s in self.shifts if s.is_proposed]

    def active_shifts(self) -> list[Shift]:
        return [s for s in self.shifts if not s.is_declined]


@dataclass
class Comparison:
    scenarios: list[str]
    rows: list[dict]
    baseline_key: str | None = None
    issues: list = field(default_factory=list)


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}"


def merge_availability(
    entries: Iterable[Availability],
) -> dict[dt.date, list[Availability]]:
    result: dict[dt.date, list[Availability]] = {}
    for entry in entries:
        result.setdefault(entry.date, []).append(entry)
    for rows in result.values():
        rows.sort(key=lambda a: (a.start_hour, a.end_hour, a.state))
    return result
