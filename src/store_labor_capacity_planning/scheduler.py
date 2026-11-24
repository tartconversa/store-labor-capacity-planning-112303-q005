"""排班方案生成、约束核实、覆盖与成本计算。

调度器是确定性的贪心构造：

1. 已接受班次先计入覆盖，并且永不删除；
2. 对每个门店/日期/岗位的剩余需求，枚举符合条件员工的候选班次；
3. 按"覆盖收益 - 成本 - 浪费 - 公平惩罚 + 可用性偏好"评分，逐个加入；
4. 达到方案目标或没有可行候选时停止，剩余缺口显式记录。

方案始终只是建议；员工接受后才形成承诺。导入的已确认班次即使违反当前约束也保留，
标记为需要协商替换，而不是静默删除。
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Mapping, Sequence

from .config import ConstraintConfig, CostConfig, PlanningConfig
from .constants import ROLE_LABELS, REGION_LABELS
from .errors import Issue, IssueBag, Severity
from .models import (
    Availability,
    Employee,
    RequirementSet,
    ScenarioPlan,
    Shift,
    Comparison,
)

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查
    from .loader import DataBundle


# ---------------------------------------------------------------------------
# 时间与成本工具
# ---------------------------------------------------------------------------


def iso_week_start(date: dt.date) -> dt.date:
    return date - dt.timedelta(days=date.weekday())


def week_key(date: dt.date) -> tuple[int, int]:
    iso = date.isocalendar()
    return (iso.year, iso.week)


def shift_start(shift: Shift) -> dt.datetime:
    return dt.datetime.combine(shift.date, dt.time(hour=min(shift.start_hour, 23)))


def shift_end(shift: Shift) -> dt.datetime:
    hour = shift.end_hour
    if hour >= 24:
        return dt.datetime.combine(shift.date + dt.timedelta(days=1), dt.time())
    return dt.datetime.combine(shift.date, dt.time(hour=hour))


def paid_hours(shift: Shift, constraints: ConstraintConfig) -> float:
    """付费工时：超过连续工作阈值后扣除休息时间。"""

    span = shift.hours
    if span <= constraints.break_after_hours:
        return span
    return max(0.0, span - constraints.break_minutes / 60.0)


def _hourly_rate(employee: Employee, config: CostConfig) -> float:
    base = config.region_rates.get(employee.region, config.default_rate)
    return base * config.role_multipliers.get(employee.role, 1.0)


def shift_cost(
    shift: Shift,
    employee: Employee,
    config: PlanningConfig,
    *,
    overtime: bool = False,
) -> float:
    """估算单个班次成本；加班由周累计函数追加。"""

    cost_config = config.cost
    constraints = config.constraints
    span = shift.hours
    paid = paid_hours(shift, constraints)
    if paid <= 0:
        return 0.0
    rate = _hourly_rate(employee, cost_config)
    evening_span = max(0.0, shift.end_hour - max(shift.start_hour, cost_config.evening_start_hour))
    paid_ratio = paid / span if span else 0.0
    evening_paid = evening_span * paid_ratio
    regular_paid = paid - evening_paid
    total = regular_paid * rate + evening_paid * rate * (1 + cost_config.evening_premium)
    if shift.date.weekday() >= 5:
        total *= 1 + cost_config.weekend_premium
    if overtime:
        total *= cost_config.overtime_multiplier
    return round(total, 2)


# ---------------------------------------------------------------------------
# 覆盖计算
# ---------------------------------------------------------------------------


@dataclass
class CoverageTotals:
    required_hours: float = 0.0
    covered_hours: float = 0.0
    scheduled_hours: float = 0.0
    overstaff_hours: float = 0.0
    uncovered_hours: float = 0.0

    @property
    def ratio(self) -> float:
        if self.required_hours <= 0:
            return 1.0
        return self.covered_hours / self.required_hours

    @property
    def utilization(self) -> float:
        if self.scheduled_hours <= 0:
            return 0.0
        return min(1.0, self.covered_hours / self.scheduled_hours)

    def to_dict(self) -> dict:
        return {
            "required_hours": round(self.required_hours, 2),
            "covered_hours": round(self.covered_hours, 2),
            "uncovered_hours": round(self.uncovered_hours, 2),
            "scheduled_hours": round(self.scheduled_hours, 2),
            "overstaff_hours": round(self.overstaff_hours, 2),
            "coverage_ratio": round(self.ratio, 4),
            "utilization": round(self.utilization, 4),
        }


@dataclass
class CoverageReport:
    totals: CoverageTotals
    by_role: dict[str, CoverageTotals]
    gaps: list[dict]
    included_states: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "included_states": list(self.included_states),
            "totals": self.totals.to_dict(),
            "by_role": {role: t.to_dict() for role, t in sorted(self.by_role.items())},
            "gaps": self.gaps,
        }


def _active_for_coverage(shift: Shift, included_states: Sequence[str]) -> bool:
    if not included_states:
        return False
    if shift.acceptance_state in included_states:
        return True
    return False


def compute_coverage(
    shifts: Sequence[Shift],
    requirements: RequirementSet,
    *,
    included_states: Sequence[str] = ("accepted", "proposed"),
    constraints: ConstraintConfig | None = None,
) -> CoverageReport:
    """按小时计算岗位覆盖。

    每小时容量计为覆盖该小时的"不同员工数"；同一员工重叠排班不会重复计数。
    需求为小数时（例如 0.4 个销售工时），一人到场即视为覆盖该小时需求。
    """

    break_config = constraints or _DEFAULT_CONSTRAINTS
    capacity: dict[tuple[str, dt.date, str, int], set[str]] = {}
    scheduled: dict[tuple[str, dt.date, str], float] = {}
    for shift in shifts:
        if not _active_for_coverage(shift, included_states):
            continue
        key = (shift.store_id, shift.date, shift.role)
        scheduled[key] = scheduled.get(key, 0.0) + paid_hours(shift, break_config)
        for hour in range(math.floor(shift.start_hour), math.ceil(shift.end_hour)):
            if shift.start_hour <= hour < shift.end_hour:
                capacity.setdefault(
                    (shift.store_id, shift.date, shift.role, hour), set()
                ).add(shift.assigned_employee)

    totals = CoverageTotals()
    by_role: dict[str, CoverageTotals] = {}
    gaps: list[dict] = []
    for requirement in requirements.requirements:
        role_totals = by_role.setdefault(requirement.role, CoverageTotals())
        key = (requirement.store_id, requirement.date, requirement.role)
        day_required = 0.0
        day_covered = 0.0
        for hour, need in requirement.by_hour.items():
            if need <= 0:
                continue
            present = len(capacity.get(key + (hour,), set()))
            covered = min(need, float(present))
            day_required += need
            day_covered += covered
            role_totals.required_hours += need
            role_totals.covered_hours += covered
            totals.required_hours += need
            totals.covered_hours += covered
            if present > need:
                role_totals.overstaff_hours += present - need
                totals.overstaff_hours += present - need
        day_scheduled = scheduled.get(key, 0.0)
        role_totals.scheduled_hours += day_scheduled
        totals.scheduled_hours += day_scheduled
        if day_required > 0 and day_covered + 1e-9 < day_required:
            missing = day_required - day_covered
            role_totals.uncovered_hours += missing
            totals.uncovered_hours += missing
            gaps.append(
                {
                    "store_id": requirement.store_id,
                    "date": requirement.date.isoformat(),
                    "role": requirement.role,
                    "required_hours": round(day_required, 2),
                    "covered_hours": round(day_covered, 2),
                    "uncovered_hours": round(missing, 2),
                    "coverage_ratio": round(day_covered / day_required, 4)
                    if day_required
                    else 1.0,
                    "priority": requirement.priority,
                }
            )
    return CoverageReport(
        totals=totals,
        by_role=by_role,
        gaps=gaps,
        included_states=tuple(included_states),
    )


# 覆盖计算只用到休息扣除，与具体方案无关；单独放一个常量避免污染签名。
_DEFAULT_CONSTRAINTS = ConstraintConfig()


# ---------------------------------------------------------------------------
# 约束核实
# ---------------------------------------------------------------------------


@dataclass
class PlanCheck:
    issues: IssueBag
    shift_issues: dict[str, list[Issue]]
    availability_unknown: dict[str, int]
    requires_renegotiation: set[str]
    weekly_hours: dict[tuple[str, tuple[int, int]], float]
    weekly_costs: dict[str, float]
    total_cost: float
    cost_by_region: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "issues": self.issues.to_dict(),
            "requires_renegotiation": sorted(self.requires_renegotiation),
            "weekly_hours": {
                f"{employee}:{year}-W{week:02d}": round(hours, 2)
                for (employee, (year, week)), hours in sorted(self.weekly_hours.items())
            },
            "total_cost": round(self.total_cost, 2),
            "cost_by_region": {
                region: round(value, 2) for region, value in sorted(self.cost_by_region.items())
            },
        }


def _window_block(entries: Sequence[Availability], start: float, end: float) -> str | None:
    """返回冲突说明；None 表示可用。"""

    if not entries:
        return "no-record"
    available = [e for e in entries if e.is_available]
    unavailable = [e for e in entries if e.state == "unavailable"]
    for entry in unavailable:
        if entry.overlaps(start, end):
            return f"unavailable-window:{entry.start_hour}-{entry.end_hour}"
    if not available:
        if any(e.state == "unknown" for e in entries):
            return "unknown-state"
        return "no-available-window"
    if not any(e.covers(start, end) for e in available):
        return "outside-window:" + ",".join(
            f"{e.start_hour}-{e.end_hour}" for e in available
        )
    return None


def check_plan(
    shifts: Sequence[Shift],
    bundle: DataBundle,
    config: PlanningConfig,
    *,
    requirements: RequirementSet | None = None,
    generated: bool = False,
    allow_cross_store: bool | None = None,
    weekly_cap_ratio: float = 1.0,
) -> PlanCheck:
    """核实员工归属、岗位、可用性、休息、合同与重叠。

    ``generated`` 为 True 时，生成班次违反硬约束属于阻断错误；导入班次只告警，
    因为已确认班次需要协商替换而不能静默删除。``allow_cross_store`` 与
    ``weekly_cap_ratio`` 用于按方案策略区分"有意为之的加班/支援"和真实违规。
    """

    bag = IssueBag()
    shift_issues: dict[str, list[Issue]] = {}
    availability_unknown: dict[str, int] = {}
    requires_renegotiation: set[str] = set()
    staff = bundle.staff_by_id
    constraints = config.constraints
    cross_store_allowed = (
        constraints.allow_cross_store if allow_cross_store is None else allow_cross_store
    )

    def add_shift_issue(shift: Shift, issue: Issue) -> None:
        shift_issues.setdefault(shift.shift_id, []).append(issue)
        bag.items.append(issue)

    active = [s for s in shifts if not s.is_declined]

    # 员工重叠与休息
    by_employee: dict[str, list[Shift]] = {}
    for shift in active:
        by_employee.setdefault(shift.assigned_employee, []).append(shift)
    for employee_id, rows in by_employee.items():
        rows.sort(key=lambda s: (s.date, s.start_hour, s.shift_id))
        latest: Shift | None = None
        latest_end: dt.datetime | None = None
        for current in rows:
            current_start = shift_start(current)
            current_end = shift_end(current)
            if latest is not None and latest_end is not None:
                if current_start < latest_end:
                    target = current if current.origin == "generated" else latest
                    add_shift_issue(
                        target,
                        Issue(
                            severity=Severity.ERROR
                            if generated and target.origin == "generated"
                            else Severity.WARNING,
                            code="shift-overlap",
                            message=(
                                f"员工 {employee_id} 的班次 {latest.shift_id} 与 {current.shift_id} 时间重叠"
                            ),
                            entity=current.shift_id,
                            blocks_publication=generated and target.origin == "generated",
                        ),
                    )
                else:
                    gap = (current_start - latest_end).total_seconds() / 3600.0
                    if gap < constraints.rest_hours_between_shifts:
                        target = current if current.origin == "generated" else latest
                        severity = (
                            Severity.ERROR
                            if target.origin == "generated" and generated
                            else Severity.WARNING
                        )
                        add_shift_issue(
                            target,
                            Issue(
                                severity=severity,
                                code="rest-violation",
                                message=(
                                    f"员工 {employee_id} 在 {latest.shift_id} 与 {current.shift_id} 之间仅休息 "
                                    f"{gap:.1f} 小时，低于 {constraints.rest_hours_between_shifts:g} 小时"
                                ),
                                entity=current.shift_id,
                                blocks_publication=severity is Severity.ERROR,
                            ),
                        )
            if latest_end is None or current_end > latest_end:
                latest = current
                latest_end = current_end

    # 连续工作天数
    for employee_id, rows in by_employee.items():
        dates = sorted({s.date for s in rows})
        run = 1
        longest = 1
        for previous_date, current_date in zip(dates, dates[1:]):
            if (current_date - previous_date).days == 1:
                run += 1
                longest = max(longest, run)
            else:
                run = 1
        if longest > constraints.max_consecutive_days:
            bag.warning(
                "consecutive-days",
                f"员工 {employee_id} 连续工作 {longest} 天，超过上限 {constraints.max_consecutive_days} 天",
                entity=employee_id,
            )

    # 逐班次检查
    for shift in active:
        employee = staff.get(shift.assigned_employee)
        if employee is None:
            add_shift_issue(
                shift,
                Issue(
                    severity=Severity.ERROR,
                    code="shift-unknown-employee",
                    message=f"班次 {shift.shift_id} 指派了不存在的员工 {shift.assigned_employee}",
                    entity=shift.shift_id,
                    blocks_publication=True,
                ),
            )
            continue
        generated_shift = shift.origin == "generated"
        blocking = generated_shift
        if shift.hours < constraints.min_shift_hours or shift.hours > constraints.max_shift_hours:
            severity = Severity.ERROR if blocking else Severity.WARNING
            add_shift_issue(
                shift,
                Issue(
                    severity=severity,
                    code="shift-length",
                    message=(
                        f"班次 {shift.shift_id} 时长 {shift.hours:g} 小时不在 "
                        f"{constraints.min_shift_hours:g}-{constraints.max_shift_hours:g} 小时范围内"
                    ),
                    entity=shift.shift_id,
                    blocks_publication=blocking,
                ),
            )
        if employee.store_id != shift.store_id:
            same_region = bool(
                employee.region and employee.region == _store_region(bundle, shift.store_id)
            )
            cross_ok = cross_store_allowed and same_region
            if not cross_ok:
                severity = Severity.ERROR if blocking else Severity.WARNING
                add_shift_issue(
                    shift,
                    Issue(
                        severity=severity,
                        code="shift-cross-store",
                        message=(
                            f"班次 {shift.shift_id} 在 {shift.store_id}，但 {employee.employee_id} 属于 "
                            f"{employee.store_id}（区域 {employee.region}）"
                        ),
                        entity=shift.shift_id,
                        blocks_publication=blocking,
                    ),
                )
                requires_renegotiation.add(shift.shift_id)
            elif generated_shift:
                add_shift_issue(
                    shift,
                    Issue(
                        severity=Severity.INFO,
                        code="cross-store-support",
                        message=(
                            f"班次 {shift.shift_id} 由同区域 {employee.store_id} 的 {employee.employee_id} 支援，需要门店负责人确认"
                        ),
                        entity=shift.shift_id,
                    ),
                )
        if employee.role != shift.role:
            severity = Severity.ERROR if blocking else Severity.WARNING
            add_shift_issue(
                shift,
                Issue(
                    severity=severity,
                    code="shift-role-mismatch",
                    message=(
                        f"班次 {shift.shift_id} 岗位为 {shift.role}，但 {employee.employee_id} 的岗位是 {employee.role}"
                    ),
                    entity=shift.shift_id,
                    blocks_publication=blocking,
                ),
            )
            requires_renegotiation.add(shift.shift_id)
        entries = bundle.availability_for(employee.employee_id, shift.date)
        window = _window_block(entries, shift.start_hour, shift.end_hour)
        if window == "no-record":
            availability_unknown[employee.employee_id] = (
                availability_unknown.get(employee.employee_id, 0) + 1
            )
            # 只有方案有意使用"未知可用性"时才作为提示；导入班次的未知日期
            # 只是资料覆盖不足，由汇总告警提示。
            if shift.origin == "generated" and "availability-unconfirmed" in shift.notes:
                shift_issues.setdefault(shift.shift_id, []).append(
                    Issue(
                        severity=Severity.INFO,
                        code="availability-unconfirmed",
                        message=(
                            f"班次 {shift.shift_id} 的员工 {employee.employee_id} 在 {shift.date.isoformat()} 没有可用性记录"
                        ),
                        entity=shift.shift_id,
                    )
                )
        elif window is not None:
            in_known_unavailable = window.startswith("unavailable-window") or window in {
                "no-available-window",
            }
            severity = (
                Severity.ERROR
                if generated_shift
                else (Severity.WARNING if in_known_unavailable else Severity.WARNING)
            )
            add_shift_issue(
                shift,
                Issue(
                    severity=severity,
                    code="availability-conflict",
                    message=(
                        f"班次 {shift.shift_id}（{shift.start_hour}-{shift.end_hour}）与 "
                        f"{employee.employee_id} 在 {shift.date.isoformat()} 的可用性冲突：{window}"
                    ),
                    entity=shift.shift_id,
                    blocks_publication=generated_shift,
                ),
            )
            if not generated_shift:
                requires_renegotiation.add(shift.shift_id)

    # 周工时、加班与成本
    weekly_hours: dict[tuple[str, tuple[int, int]], float] = {}
    weekly_costs: dict[str, float] = {}
    cost_by_region: dict[str, float] = {}
    total_cost = 0.0
    by_employee_week: dict[tuple[str, tuple[int, int]], list[Shift]] = {}
    for shift in active:
        employee = staff.get(shift.assigned_employee)
        if employee is None:
            continue
        key = (employee.employee_id, week_key(shift.date))
        by_employee_week.setdefault(key, []).append(shift)
    for (employee_id, week), rows in by_employee_week.items():
        employee = staff[employee_id]
        cap = constraints.max_weekly_hours or employee.contract_hours
        if constraints.max_weekly_hours is not None:
            cap = min(employee.contract_hours, constraints.max_weekly_hours)
        rows.sort(key=lambda s: (s.date, s.start_hour))
        cumulative = 0.0
        for shift in rows:
            hours = paid_hours(shift, constraints)
            overtime = cumulative >= cap - 1e-9
            partial = max(0.0, cumulative + hours - cap) if not overtime else 0.0
            cost = shift_cost(shift, employee, config)
            if partial > 0:
                # 部分加班：按比例提高超出部分的成本。
                base = shift_cost(shift, employee, config, overtime=False)
                per_hour = base / hours if hours else 0.0
                cost = base + per_hour * partial * (config.cost.overtime_multiplier - 1)
            elif overtime:
                cost = shift_cost(shift, employee, config, overtime=True)
            total_cost += cost
            cost_by_region[employee.region] = cost_by_region.get(employee.region, 0.0) + cost
            weekly_costs[employee_id] = weekly_costs.get(employee_id, 0.0) + cost
            cumulative += hours
        weekly_hours[(employee_id, week)] = cumulative
        if cumulative > cap + constraints.weekly_overtime_tolerance + 1e-9:
            over = cumulative - cap
            # 方案策略允许的合同上限内加班（例如覆盖优先 ×1.2）只算提示；
            # 超过策略上限仍按错误报告。
            planned_cap = cap * weekly_cap_ratio
            if cumulative <= planned_cap + constraints.weekly_overtime_tolerance + 1e-9:
                severity = Severity.INFO
            else:
                severity = Severity.ERROR if any(s.origin == "generated" for s in rows) else Severity.WARNING
            bag.add(
                severity,
                "weekly-hours",
                (
                    f"员工 {employee_id} 在 {week[0]}-W{week[1]:02d} 安排 {cumulative:.1f} 小时，"
                    f"超过合同/上限 {cap:.1f} 小时 {over:.1f} 小时"
                ),
                entity=employee_id,
                blocks_publication=severity is Severity.ERROR,
            )

    if availability_unknown:
        names = ", ".join(
            f"{employee}（{count} 班）" for employee, count in sorted(availability_unknown.items())
        )
        bag.warning(
            "availability-records-missing",
            f"以下员工在部分班次日期没有可用性记录，按未知处理，需要确认：{names}",
        )

    if requirements is not None:
        coverage = compute_coverage(
            shifts,
            requirements,
            included_states=("accepted", "proposed", "unknown"),
            constraints=constraints,
        )
        for gap in coverage.gaps:
            if gap["coverage_ratio"] >= config.scheduler.min_coverage_target:
                continue
            bag.warning(
                "coverage-gap",
                (
                    f"{gap['store_id']} {gap['date']} {gap['role']} 需求 {gap['required_hours']:.1f} 小时，"
                    f"覆盖 {gap['covered_hours']:.1f} 小时（{gap['coverage_ratio']*100:.0f}%），缺口 {gap['uncovered_hours']:.1f} 小时"
                ),
                entity=gap["store_id"],
            )

    return PlanCheck(
        issues=bag,
        shift_issues=shift_issues,
        availability_unknown=availability_unknown,
        requires_renegotiation=requires_renegotiation,
        weekly_hours=weekly_hours,
        weekly_costs=weekly_costs,
        total_cost=round(total_cost, 2),
        cost_by_region={k: round(v, 2) for k, v in cost_by_region.items()},
    )


def _store_region(bundle: DataBundle, store_id: str) -> str | None:
    for employee in bundle.staff:
        if employee.store_id == store_id:
            return employee.region
    return None


# ---------------------------------------------------------------------------
# 方案生成
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioProfile:
    key: str
    name: str
    description: str
    coverage_target: float
    cost_weight: float
    preference_weight: float
    allow_overtime_ratio: float = 1.0
    allow_cross_store: bool = False
    allow_unknown_availability: bool = False
    fairness_weight: float = 0.10


BALANCED = ScenarioProfile(
    key="balanced",
    name="均衡推荐",
    description="优先覆盖关键岗位，同时控制成本和员工偏好；仅使用本店员工，缺可用性记录的时段标为待确认。",
    coverage_target=0.95,
    cost_weight=0.18,
    preference_weight=0.20,
    allow_unknown_availability=True,
    fairness_weight=0.10,
)
COVERAGE_FIRST = ScenarioProfile(
    key="coverage",
    name="覆盖优先",
    description="允许同区域跨店支援、合同工时上限内的加班，并把缺可用性记录的时段作为待确认安排。",
    coverage_target=1.0,
    cost_weight=0.06,
    preference_weight=0.10,
    allow_overtime_ratio=1.20,
    allow_cross_store=True,
    allow_unknown_availability=True,
    fairness_weight=0.06,
)
COST_SAVING = ScenarioProfile(
    key="cost",
    name="成本优先",
    description="在可接受覆盖下限内减少排班，优先短班和低价时段；不使用加班、跨店或未确认可用性。",
    coverage_target=0.85,
    cost_weight=0.55,
    preference_weight=0.05,
    allow_unknown_availability=False,
    fairness_weight=0.15,
)

DEFAULT_PROFILES = (BALANCED, COVERAGE_FIRST, COST_SAVING)


@dataclass
class _Ledger:
    weekly_hours: dict[tuple[str, tuple[int, int]], float] = field(default_factory=dict)
    dates: dict[str, set[dt.date]] = field(default_factory=dict)
    shifts: dict[str, list[Shift]] = field(default_factory=dict)

    def add(self, shift: Shift, constraints: ConstraintConfig) -> None:
        key = (shift.assigned_employee, week_key(shift.date))
        self.weekly_hours[key] = self.weekly_hours.get(key, 0.0) + paid_hours(shift, constraints)
        self.dates.setdefault(shift.assigned_employee, set()).add(shift.date)
        self.shifts.setdefault(shift.assigned_employee, []).append(shift)


def _rest_ok(ledger: _Ledger, employee_id: str, shift: Shift, constraints: ConstraintConfig) -> bool:
    start = shift_start(shift)
    end = shift_end(shift)
    for other in ledger.shifts.get(employee_id, []):
        if other.shift_id == shift.shift_id:
            continue
        other_start = shift_start(other)
        other_end = shift_end(other)
        if other_start < end and start < other_end:
            return False
        gap = (other_start - end).total_seconds() / 3600.0
        if 0 <= gap < constraints.rest_hours_between_shifts:
            return False
        gap = (start - other_end).total_seconds() / 3600.0
        if 0 <= gap < constraints.rest_hours_between_shifts:
            return False
    return True


def _consecutive_ok(ledger: _Ledger, employee_id: str, date: dt.date, limit: int) -> bool:
    dates = set(ledger.dates.get(employee_id, set()))
    dates.add(date)
    # 只关心包含候选日期的最长连续段。
    run = 1
    cursor = date - dt.timedelta(days=1)
    while cursor in dates:
        run += 1
        cursor -= dt.timedelta(days=1)
    cursor = date + dt.timedelta(days=1)
    while cursor in dates:
        run += 1
        cursor += dt.timedelta(days=1)
    return run <= limit


def _employee_windows(
    bundle: DataBundle,
    employee: Employee,
    date: dt.date,
    *,
    allow_unknown: bool,
    store_open: tuple[int, int],
) -> list[tuple[int, int, str]]:
    """返回可选工作窗口及对应可用状态。"""

    entries = bundle.availability_for(employee.employee_id, date)
    windows: list[tuple[int, int, str]] = []
    if not entries:
        if allow_unknown:
            windows.append((store_open[0], store_open[1], "unknown"))
        return windows
    unavailable = [e for e in entries if e.state == "unavailable"]
    available = [e for e in entries if e.is_available]
    for entry in available:
        blocked = any(
            u.start_hour < entry.end_hour and entry.start_hour < u.end_hour
            for u in unavailable
        )
        if blocked:
            continue
        windows.append((entry.start_hour, entry.end_hour, entry.state))
    if not available and not unavailable and any(e.state == "unknown" for e in entries):
        if allow_unknown:
            windows.append((store_open[0], store_open[1], "unknown"))
    return windows


def _eligible_employees(
    bundle: DataBundle,
    store_id: str,
    role: str,
    profile: ScenarioProfile,
    config: PlanningConfig,
) -> list[Employee]:
    candidates: list[Employee] = []
    store_region = _store_region(bundle, store_id)
    for employee in bundle.staff:
        if employee.role != role:
            continue
        if employee.store_id == store_id:
            candidates.append(employee)
        elif (
            profile.allow_cross_store
            and store_region is not None
            and employee.region == store_region
        ):
            candidates.append(employee)
    return candidates


def _candidate_score(
    shift: Shift,
    employee: Employee,
    remaining: Mapping[int, float],
    *,
    profile: ScenarioProfile,
    config: PlanningConfig,
    ledger: _Ledger,
    window_state: str,
) -> float:
    gain = 0.0
    waste = 0.0
    for hour in range(shift.start_hour, shift.end_hour):
        need = remaining.get(hour, 0.0)
        if need > 0:
            # 需求不足 1 人时，仍按实际缺口计分，但一人可以同时覆盖多个岗位任务。
            gain += min(1.0, need)
        else:
            waste += 1.0
    cost = shift_cost(shift, employee, config)
    cost_hours = cost / max(config.cost.default_rate, 1e-6)
    week = week_key(shift.date)
    used = ledger.weekly_hours.get((employee.employee_id, week), 0.0)
    contract = employee.contract_hours * profile.allow_overtime_ratio
    load = used / contract if contract > 0 else 1.0
    fairness_penalty = max(0.0, load - 0.75) * 8.0 * profile.fairness_weight
    preference = 1.0 if window_state == "preferred" else (0.5 if window_state == "available" else 0.0)
    uncertain_penalty = 0.6 if window_state == "unknown" else 0.0
    score = (
        gain * 10.0
        - cost_hours * profile.cost_weight * 2.0
        - waste * 2.5
        + preference * profile.preference_weight * 3.0
        - fairness_penalty
        - uncertain_penalty
    )
    if employee.store_id != shift.store_id:
        score -= 1.5
    return score


def _prefix_for(profile: ScenarioProfile) -> str:
    return {"balanced": "BAL", "coverage": "COV", "cost": "SAV"}.get(
        profile.key, profile.key.upper()[:3]
    )


def _generate_shift_id(profile: ScenarioProfile, counter: int) -> str:
    return f"{_prefix_for(profile)}-{counter:04d}"


def generate_scenario(
    bundle: DataBundle,
    requirements: RequirementSet,
    config: PlanningConfig,
    *,
    profile: ScenarioProfile,
    dates: Sequence[dt.date],
    fixed_shifts: Sequence[Shift] | None = None,
    revision: int = 0,
    log_superseded: bool = True,
) -> ScenarioPlan:
    """生成方案。

    ``fixed_shifts`` 用于修订：这些班次视为已锁定（通常是已接受班次，以及未受影响的
    未确认班次），既会先占用覆盖和合同工时，也不会被重新生成。默认只锁定已接受班次。
    """

    date_set = set(dates)
    if fixed_shifts is None:
        fixed = [s for s in bundle.shifts if s.date in date_set and s.is_accepted]
    else:
        fixed = [s for s in fixed_shifts if s.date in date_set and not s.is_declined]
    fixed_ids = {s.shift_id for s in fixed}
    accepted = [s for s in fixed if s.is_accepted]
    ledger = _Ledger()
    for shift in fixed:
        ledger.add(shift, config.constraints)
    generated: list[Shift] = []
    counter = 1
    # 修订时从已有方案编号之后继续，避免 ID 冲突。
    for candidate in fixed:
        if candidate.shift_id.startswith(_prefix_for(profile)):
            try:
                counter = max(counter, int(candidate.shift_id.rsplit("-", 1)[1]) + 1)
            except (IndexError, ValueError):
                continue
    change_log: list[dict] = []
    if log_superseded:
        for shift in bundle.shifts:
            if shift.date not in date_set:
                continue
            if shift.shift_id in fixed_ids:
                continue
            if shift.is_accepted:
                continue
            change_log.append(
                {
                    "shift_id": shift.shift_id,
                    "action": "superseded",
                    "reason": f"导入状态为 {shift.acceptance_state}，未形成承诺，由推荐方案重新生成",
                }
            )

    # 已锁定班次先计入需求覆盖。
    remaining: dict[tuple[str, dt.date, str], dict[int, float]] = {}
    for requirement in requirements.requirements:
        key = (requirement.store_id, requirement.date, requirement.role)
        remaining[key] = dict(requirement.by_hour)
    for shift in fixed:
        key = (shift.store_id, shift.date, shift.role)
        bucket = remaining.get(key)
        if not bucket:
            continue
        for hour in range(shift.start_hour, shift.end_hour):
            if hour in bucket and bucket[hour] > 0:
                bucket[hour] = max(0.0, bucket[hour] - 1.0)

    open_ranges: dict[tuple[str, dt.date], tuple[int, int]] = {}
    for requirement in requirements.requirements:
        key = (requirement.store_id, requirement.date)
        hours = requirement.by_hour
        if not hours:
            continue
        low, high = min(hours), max(hours) + 1
        existing = open_ranges.get(key)
        if existing:
            open_ranges[key] = (min(existing[0], low), max(existing[1], high))
        else:
            open_ranges[key] = (low, high)

    # 缺口大的门店/日期先排，避免跨店方案被先到先得占满。
    keys = sorted(
        remaining,
        key=lambda k: (
            k[1],
            -sum(remaining[k].values()),
            k[0],
            k[2],
        ),
    )
    for key in keys:
        store_id, date, role = key
        bucket = remaining[key]
        requirement = next(
            r
            for r in requirements.requirements
            if r.store_id == store_id and r.date == date and r.role == role
        )
        if sum(bucket.values()) <= 0:
            continue
        open_range = open_ranges.get((store_id, date), (8, 20))
        eligible = _eligible_employees(bundle, store_id, role, profile, config)
        shifts_this_role = 0
        while sum(bucket.values()) > 0 and shifts_this_role < config.constraints.max_shifts_per_role_day:
            required_total = requirement.total_hours
            if required_total <= 0:
                break
            remaining_ratio = sum(min(1.0, v) for v in bucket.values()) / required_total
            achieved = max(0.0, 1.0 - remaining_ratio)
            if achieved + 1e-9 >= profile.coverage_target:
                break
            candidates: list[tuple[float, Shift, str]] = []
            for employee in eligible:
                if not _consecutive_ok(
                    ledger, employee.employee_id, date, config.constraints.max_consecutive_days
                ):
                    continue
                week = week_key(date)
                used = ledger.weekly_hours.get((employee.employee_id, week), 0.0)
                cap = employee.contract_hours * profile.allow_overtime_ratio
                if used >= cap - 1e-9:
                    continue
                windows = _employee_windows(
                    bundle,
                    employee,
                    date,
                    allow_unknown=(
                        profile.allow_unknown_availability
                        and config.constraints.allow_unknown_availability
                    ),
                    store_open=open_range,
                )
                for window_start, window_end, state in windows:
                    low = max(open_range[0], window_start)
                    high = min(open_range[1], window_end)
                    if high - low < config.constraints.min_shift_hours:
                        continue
                    for start in range(low, high):
                        max_length = min(
                            config.constraints.max_shift_hours,
                            high - start,
                        )
                        if max_length < config.constraints.min_shift_hours:
                            continue
                        for length in range(
                            int(config.constraints.min_shift_hours),
                            int(math.floor(max_length)) + 1,
                        ):
                            shift = Shift(
                                shift_id=_generate_shift_id(profile, counter),
                                store_id=store_id,
                                date=date,
                                role=role,
                                start_hour=start,
                                end_hour=start + length,
                                assigned_employee=employee.employee_id,
                                acceptance_state="proposed",
                                origin="generated",
                                scenario=profile.key,
                                notes=(
                                    ("availability-unconfirmed",)
                                    if state == "unknown"
                                    else ()
                                ),
                            )
                            if not _rest_ok(
                                ledger, employee.employee_id, shift, config.constraints
                            ):
                                continue
                            if used + paid_hours(shift, config.constraints) > cap + 1e-9:
                                continue
                            score = _candidate_score(
                                shift,
                                employee,
                                bucket,
                                profile=profile,
                                config=config,
                                ledger=ledger,
                                window_state=state,
                            )
                            candidates.append((score, shift, state))
            if not candidates:
                break
            candidates.sort(key=lambda item: (-item[0], item[1].start_hour, item[1].end_hour, item[1].assigned_employee))
            best_score, best_shift, state = candidates[0]
            threshold = 0.0 if achieved >= profile.coverage_target else -2.0
            if best_score < threshold:
                break
            counter += 1
            ledger.add(best_shift, config.constraints)
            generated.append(best_shift)
            shifts_this_role += 1
            for hour in range(best_shift.start_hour, best_shift.end_hour):
                if hour in bucket and bucket[hour] > 0:
                    bucket[hour] = 0.0
            change_log.append(
                {
                    "shift_id": best_shift.shift_id,
                    "action": "added",
                    "store_id": store_id,
                    "date": date.isoformat(),
                    "role": role,
                    "employee_id": best_shift.assigned_employee,
                    "start_hour": best_shift.start_hour,
                    "end_hour": best_shift.end_hour,
                    "score": round(best_score, 3),
                }
            )

    shifts = fixed + generated
    check = check_plan(
        shifts,
        bundle,
        config,
        requirements=requirements,
        generated=True,
        allow_cross_store=profile.allow_cross_store,
        weekly_cap_ratio=profile.allow_overtime_ratio,
    )
    plan_issues = IssueBag()
    plan_issues.extend(check.issues)
    for shift in generated:
        if "availability-unconfirmed" in shift.notes:
            plan_issues.info(
                "generated-availability-unknown",
                f"班次 {shift.shift_id} 的员工没有可用性记录，发布前需确认",
                entity=shift.shift_id,
            )
    return ScenarioPlan(
        key=profile.key,
        name=profile.name,
        description=profile.description,
        shifts=shifts,
        issues=list(plan_issues.items),
        change_log=change_log,
        revision=revision,
        metadata={
            "coverage_target": profile.coverage_target,
            "allow_cross_store": profile.allow_cross_store,
            "allow_overtime_ratio": profile.allow_overtime_ratio,
            "generated_count": len(generated),
            "accepted_carried": len(accepted),
            "fixed_count": len(fixed),
        },
    )


def baseline_scenario(
    bundle: DataBundle,
    requirements: RequirementSet,
    config: PlanningConfig,
    *,
    dates: Sequence[dt.date],
) -> ScenarioPlan:
    date_set = set(dates)
    shifts = [s for s in bundle.shifts if s.date in date_set]
    check = check_plan(
        shifts,
        bundle,
        config,
        requirements=requirements,
        generated=False,
    )
    return ScenarioPlan(
        key="baseline",
        name="导入基线",
        description="原样导入的班次计划，用于与推荐方案比较；不自动修正任何冲突。",
        shifts=shifts,
        issues=list(check.issues.items),
        change_log=[],
        metadata={"generated_count": 0, "accepted_carried": len([s for s in shifts if s.is_accepted])},
    )


# ---------------------------------------------------------------------------
# 方案评估与比较
# ---------------------------------------------------------------------------


@dataclass
class ScenarioMetrics:
    key: str
    name: str
    total_shifts: int
    generated_shifts: int
    accepted_shifts: int
    proposed_shifts: int
    declined_shifts: int
    scheduled_hours: float
    cost: float
    cost_by_region: Mapping[str, float]
    planned_coverage: CoverageReport
    committed_coverage: CoverageReport
    preference_ratio: float
    fairness: float
    violation_count: int
    blocking_count: int
    inherited_conflicts: int
    requires_renegotiation: int
    budget_status: dict
    score: float
    score_components: dict

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "name": self.name,
            "total_shifts": self.total_shifts,
            "generated_shifts": self.generated_shifts,
            "accepted_shifts": self.accepted_shifts,
            "proposed_shifts": self.proposed_shifts,
            "declined_shifts": self.declined_shifts,
            "scheduled_hours": round(self.scheduled_hours, 2),
            "cost": round(self.cost, 2),
            "cost_by_region": {k: round(v, 2) for k, v in sorted(self.cost_by_region.items())},
            "planned_coverage": self.planned_coverage.to_dict(),
            "committed_coverage": self.committed_coverage.to_dict(),
            "preference_ratio": round(self.preference_ratio, 4),
            "fairness": round(self.fairness, 4),
            "violation_count": self.violation_count,
            "blocking_count": self.blocking_count,
            "inherited_conflicts": self.inherited_conflicts,
            "requires_renegotiation": self.requires_renegotiation,
            "budget_status": self.budget_status,
            "score": round(self.score, 2),
            "score_components": {k: round(v, 3) for k, v in self.score_components.items()},
        }


def _preference_stats(
    shifts: Sequence[Shift],
    bundle: DataBundle,
    constraints: ConstraintConfig | None = None,
) -> tuple[float, float]:
    break_config = constraints or _DEFAULT_CONSTRAINTS
    preferred = 0.0
    total = 0.0
    hours_by_employee: dict[str, float] = {}
    for shift in shifts:
        if shift.is_declined:
            continue
        hours = paid_hours(shift, break_config)
        total += hours
        entries = bundle.availability_for(shift.assigned_employee, shift.date)
        if any(e.is_preferred and e.covers(shift.start_hour, shift.end_hour) for e in entries):
            preferred += hours
        hours_by_employee[shift.assigned_employee] = hours_by_employee.get(shift.assigned_employee, 0.0) + hours
    preference_ratio = preferred / total if total else 0.0
    loads = []
    staff = bundle.staff_by_id
    for employee_id, hours in hours_by_employee.items():
        employee = staff.get(employee_id)
        if employee is None or employee.contract_hours <= 0:
            continue
        loads.append(min(1.5, hours / (employee.contract_hours * 3)))
    if len(loads) >= 2:
        mean = statistics.fmean(loads)
        stdev = statistics.pstdev(loads)
        fairness = max(0.0, 1.0 - stdev / mean) if mean > 0 else 1.0
    else:
        fairness = 1.0
    return preference_ratio, fairness


def _budget_status(
    metrics_cost_by_region: Mapping[str, float],
    bundle: "DataBundle",
    dates: Sequence[dt.date],
) -> dict:
    """按区域汇总与规划日期重叠的预算期间，再与预计成本比较。

    预算按区间给出（例如每周一行）；重叠的期间只计一次，避免重复累计。
    """

    status: dict[str, dict] = {}
    regions = {e.region for e in bundle.staff if e.region}
    if dates:
        period_start, period_end = min(dates), max(dates)
    else:
        period_start = period_end = None
    for region in sorted(regions):
        cost = metrics_cost_by_region.get(region, 0.0)
        rows = [
            b
            for b in bundle.budgets
            if b.region == region
            and (
                period_start is None
                or (b.period_start <= period_end and period_start <= b.period_end)
            )
        ]
        if not rows:
            status[region] = {
                "region_name": REGION_LABELS.get(region, region),
                "budget": None,
                "cost": round(cost, 2),
                "state": "no-budget",
            }
            continue
        unique: dict[tuple[dt.date, dt.date, float], None] = {}
        for row in rows:
            unique[(row.period_start, row.period_end, row.amount)] = None
        amount = sum(item[2] for item in unique)
        covered_start = min(item[0] for item in unique)
        covered_end = max(item[1] for item in unique)
        variance = cost - amount
        state = (
            "within"
            if variance <= 0
            else ("near" if variance <= amount * 0.05 else "over")
        )
        status[region] = {
            "region_name": REGION_LABELS.get(region, region),
            "budget": round(amount, 2),
            "cost": round(cost, 2),
            "variance": round(variance, 2),
            "variance_ratio": round(variance / amount, 4) if amount else None,
            "state": state,
            "period_start": covered_start.isoformat(),
            "period_end": covered_end.isoformat(),
            "periods": len(unique),
        }
    return status


def evaluate_scenario(
    plan: ScenarioPlan,
    bundle: DataBundle,
    requirements: RequirementSet,
    config: PlanningConfig,
    *,
    dates: Sequence[dt.date],
) -> ScenarioMetrics:
    check = check_plan(
        plan.shifts,
        bundle,
        config,
        requirements=requirements,
        generated=plan.key != "baseline",
        allow_cross_store=bool(plan.metadata.get("allow_cross_store", False)),
        weekly_cap_ratio=float(plan.metadata.get("allow_overtime_ratio", 1.0)),
    )
    planned = compute_coverage(
        plan.shifts,
        requirements,
        included_states=("accepted", "proposed", "unknown"),
        constraints=config.constraints,
    )
    committed = compute_coverage(
        plan.shifts,
        requirements,
        included_states=("accepted",),
        constraints=config.constraints,
    )
    active = [s for s in plan.shifts if not s.is_declined]
    scheduled_hours = sum(paid_hours(s, config.constraints) for s in active)
    preference_ratio, fairness = _preference_stats(plan.shifts, bundle, config.constraints)
    violation_issues = [
        issue
        for issue in check.issues
        if issue.code
        in {
            "shift-overlap",
            "rest-violation",
            "weekly-hours",
            "availability-conflict",
            "shift-cross-store",
            "shift-role-mismatch",
            "shift-length",
            "consecutive-days",
        }
    ]
    generated_ids = {s.shift_id for s in plan.shifts if s.origin == "generated"}
    # 导入的已确认班次冲突无法在方案内消除，只能协商；不因此惩罚新方案，
    # 但会在 inherited_conflicts 中单独呈现。
    generated_violations = [
        issue for issue in violation_issues if issue.entity in generated_ids
    ]
    inherited_conflicts = len(violation_issues) - len(generated_violations)
    violation_count = len(generated_violations) if plan.key != "baseline" else len(violation_issues)
    blocking_count = len(
        [
            issue
            for issue in check.issues
            if issue.blocks_publication and (plan.key == "baseline" or issue.entity in generated_ids)
        ]
    )
    budget = _budget_status(check.cost_by_region, bundle, dates)

    coverage_component = planned.totals.ratio * 60.0
    if budget:
        per_region = []
        for region, entry in budget.items():
            if entry.get("budget"):
                used = entry["cost"] / entry["budget"] if entry["budget"] else 0.0
                per_region.append(max(0.0, 1.0 - max(0.0, used - 1.0)) * 20.0)
            else:
                per_region.append(10.0)
        budget_component = statistics.fmean(per_region)
    else:
        # 没有区域预算时，用"成本 / 排班工时"做相对评分：仅用于方案之间比较。
        per_hour = check.total_cost / scheduled_hours if scheduled_hours else 0.0
        reference = config.cost.default_rate * 1.1
        budget_component = 20.0 * max(0.0, min(1.0, 1.0 - max(0.0, per_hour / reference - 0.5)))
    preference_component = preference_ratio * 10.0
    fairness_component = fairness * 10.0
    penalty = min(
        30.0,
        violation_count * 1.5 + blocking_count * 5.0,
    )
    score = max(
        0.0,
        coverage_component + budget_component + preference_component + fairness_component - penalty,
    )
    return ScenarioMetrics(
        key=plan.key,
        name=plan.name,
        total_shifts=len(plan.shifts),
        generated_shifts=sum(1 for s in plan.shifts if s.origin == "generated"),
        accepted_shifts=sum(1 for s in plan.shifts if s.acceptance_state == "accepted"),
        proposed_shifts=sum(1 for s in plan.shifts if s.acceptance_state == "proposed"),
        declined_shifts=sum(1 for s in plan.shifts if s.is_declined),
        scheduled_hours=scheduled_hours,
        cost=check.total_cost,
        cost_by_region=check.cost_by_region,
        planned_coverage=planned,
        committed_coverage=committed,
        preference_ratio=preference_ratio,
        fairness=fairness,
        violation_count=violation_count,
        blocking_count=blocking_count,
        inherited_conflicts=inherited_conflicts,
        requires_renegotiation=len(check.requires_renegotiation),
        budget_status=budget,
        score=score,
        score_components={
            "coverage": coverage_component,
            "budget": budget_component,
            "preference": preference_component,
            "fairness": fairness_component,
            "penalty": penalty,
        },
    )


def compare_scenarios(
    plans: Mapping[str, ScenarioPlan],
    bundle: DataBundle,
    requirements: RequirementSet,
    config: PlanningConfig,
    *,
    dates: Sequence[dt.date],
    baseline_key: str = "baseline",
) -> tuple[Comparison, dict[str, ScenarioMetrics]]:
    metrics: dict[str, ScenarioMetrics] = {}
    for key, plan in plans.items():
        metrics[key] = evaluate_scenario(
            plan, bundle, requirements, config, dates=dates
        )
    rows: list[dict] = []
    for key, item in metrics.items():
        rows.append(
            {
                "scenario": key,
                "name": item.name,
                "score": round(item.score, 2),
                "planned_coverage": round(item.planned_coverage.totals.ratio, 4),
                "committed_coverage": round(item.committed_coverage.totals.ratio, 4),
                "scheduled_hours": round(item.scheduled_hours, 2),
                "cost": round(item.cost, 2),
                "violations": item.violation_count,
                "blocking": item.blocking_count,
                "inherited_conflicts": item.inherited_conflicts,
                "requires_renegotiation": item.requires_renegotiation,
                "budget_regions_over": sum(
                    1 for s in item.budget_status.values() if s.get("state") == "over"
                ),
                "accepted_shifts": item.accepted_shifts,
                "proposed_shifts": item.proposed_shifts,
                "is_baseline": key == baseline_key,
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["scenario"]))
    comparison = Comparison(
        scenarios=list(plans.keys()),
        rows=rows,
        baseline_key=baseline_key if baseline_key in plans else None,
        issues=[],
    )
    return comparison, metrics


def coverage_gap_summary(plan: ScenarioPlan, requirements: RequirementSet) -> list[dict]:
    report = compute_coverage(plan.shifts, requirements)
    return sorted(
        report.gaps,
        key=lambda g: (g["date"], g["store_id"], -g["uncovered_hours"]),
    )


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)
