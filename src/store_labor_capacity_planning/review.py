"""实际打卡、服务效果与预测回看。

回看只使用有实际覆盖记录的班次：

* 缺勤、迟到等差异原因保留原始值；clocked 为 0 通常对应缺勤，但仍以原始字段为准。
* 实际覆盖率按"打卡分钟 / 计划付费分钟"计算，超过 100% 单独标记为超出计划。
* 计划覆盖与服务等待来自两套数据，不做强行归因；只并列呈现并提示可能关系。
* 没有实际记录的班次状态为"未知"，不是 0 分钟。
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Sequence

from .config import PlanningConfig
from .constants import ROLE_LABELS, VARIANCE_LABELS
from .errors import IssueBag
from .models import Shift
from .scheduler import compute_coverage, paid_hours

if TYPE_CHECKING:  # pragma: no cover
    from .loader import DataBundle
    from .models import RequirementSet


@dataclass
class ShiftReview:
    shift_id: str
    store_id: str
    date: dt.date
    role: str
    employee_id: str
    acceptance_state: str
    planned_hours: float | None
    planned_paid_hours: float | None
    clocked_minutes: int | None
    clocked_hours: float | None
    attendance_ratio: float | None
    task_load: float | None
    service_wait_minutes: float | None
    variance_reason: str
    state: str

    def to_row(self) -> dict:
        return {
            "shift_id": self.shift_id,
            "store_id": self.store_id,
            "date": self.date.isoformat(),
            "role": self.role,
            "role_label": ROLE_LABELS.get(self.role, self.role),
            "employee_id": self.employee_id,
            "acceptance_state": self.acceptance_state,
            "planned_hours": _fmt(self.planned_hours),
            "planned_paid_hours": _fmt(self.planned_paid_hours),
            "clocked_minutes": "" if self.clocked_minutes is None else self.clocked_minutes,
            "clocked_hours": _fmt(self.clocked_hours),
            "attendance_ratio": _fmt(self.attendance_ratio),
            "variance_reason": self.variance_reason,
            "variance_label": VARIANCE_LABELS.get(self.variance_reason, self.variance_reason),
            "state": self.state,
        }


@dataclass
class StoreDayReview:
    store_id: str
    date: dt.date
    planned_hours: float
    clocked_hours: float | None
    attendance_ratio: float | None
    shifts_planned: int
    shifts_observed: int
    shifts_missing_actual: int
    coverage_ratio: float | None
    avg_wait_minutes: float | None
    max_wait_minutes: float | None
    avg_task_load: float | None
    variance_reasons: dict[str, int] = field(default_factory=dict)

    def to_row(self) -> dict:
        return {
            "store_id": self.store_id,
            "date": self.date.isoformat(),
            "planned_hours": _fmt(self.planned_hours),
            "clocked_hours": _fmt(self.clocked_hours),
            "attendance_ratio": _fmt(self.attendance_ratio),
            "shifts_planned": self.shifts_planned,
            "shifts_observed": self.shifts_observed,
            "shifts_missing_actual": self.shifts_missing_actual,
            "coverage_ratio": _fmt(self.coverage_ratio),
            "avg_wait_minutes": _fmt(self.avg_wait_minutes),
            "max_wait_minutes": _fmt(self.max_wait_minutes),
            "avg_task_load": _fmt(self.avg_task_load),
            "variance_reasons": ";".join(
                f"{k}={v}" for k, v in sorted(self.variance_reasons.items())
            ),
        }


@dataclass
class ReviewResult:
    shifts: list[ShiftReview]
    store_days: list[StoreDayReview]
    issues: list
    assumptions: list[str]
    summary: dict

    def shift_rows(self) -> list[dict]:
        return [row.to_row() for row in self.shifts]

    def store_day_rows(self) -> list[dict]:
        return [row.to_row() for row in self.store_days]


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}"


def build_review(
    bundle: "DataBundle",
    config: PlanningConfig,
    *,
    shifts: Sequence[Shift] | None = None,
    requirements: "RequirementSet | None" = None,
    bag: IssueBag | None = None,
) -> ReviewResult:
    issues = bag if bag is not None else IssueBag()
    source_shifts = list(shifts) if shifts is not None else list(bundle.shifts)
    actual_by_shift = bundle.actual_by_shift
    reviews: list[ShiftReview] = []
    for shift in sorted(source_shifts, key=lambda s: (s.date, s.store_id, s.start_hour)):
        record = actual_by_shift.get(shift.shift_id)
        planned = shift.hours
        planned_paid = paid_hours(shift, config.constraints)
        if record is None:
            state = "unknown"
            clocked = None
            ratio = None
            task = None
            wait = None
            reason = "none"
        else:
            clocked = record.clocked_hours
            state = "observed" if clocked is not None else "unknown"
            if clocked is None:
                ratio = None
            elif planned_paid > 0:
                ratio = clocked / planned_paid
            else:
                ratio = None
            task = record.task_load
            wait = record.service_wait_minutes
            reason = record.variance_reason
            if state == "unknown":
                issues.info(
                    "review-unknown-minutes",
                    f"班次 {shift.shift_id} 有覆盖记录但缺少打卡分钟，状态保持未知",
                    entity=shift.shift_id,
                )
            if ratio is not None and ratio > 1.0 + config.review.overtime_tolerance_minutes / max(
                planned_paid * 60, 1
            ):
                issues.warning(
                    "review-overtime",
                    f"班次 {shift.shift_id} 打卡 {clocked:.2f} 小时，超过计划付费 {planned_paid:.2f} 小时",
                    entity=shift.shift_id,
                )
        reviews.append(
            ShiftReview(
                shift_id=shift.shift_id,
                store_id=shift.store_id,
                date=shift.date,
                role=shift.role,
                employee_id=shift.assigned_employee,
                acceptance_state=shift.acceptance_state,
                planned_hours=planned,
                planned_paid_hours=planned_paid,
                clocked_minutes=None if record is None else record.clocked_minutes,
                clocked_hours=clocked,
                attendance_ratio=ratio,
                task_load=task,
                service_wait_minutes=wait,
                variance_reason=reason,
                state=state,
            )
        )

    observed = [r for r in reviews if r.state == "observed"]
    missing = [r for r in reviews if r.state == "unknown"]
    if missing:
        issues.info(
            "review-missing-actual",
            f"{len(missing)} 个班次没有实际覆盖记录，回看中标记为未知而非零分钟",
        )

    store_days = _aggregate_store_days(
        reviews, bundle, requirements, config, issues
    )

    waits = [r.service_wait_minutes for r in observed if r.service_wait_minutes is not None]
    tasks = [r.task_load for r in observed if r.task_load is not None]
    attendance = [r.attendance_ratio for r in observed if r.attendance_ratio is not None]
    summary = {
        "shifts_total": len(reviews),
        "shifts_observed": len(observed),
        "shifts_unknown": len(reviews) - len(observed),
        "accepted_shifts": sum(1 for r in reviews if r.acceptance_state == "accepted"),
        "declined_shifts": sum(1 for r in reviews if r.acceptance_state == "declined"),
        "proposed_shifts": sum(1 for r in reviews if r.acceptance_state == "proposed"),
        "planned_hours": round(sum(r.planned_paid_hours or 0.0 for r in reviews), 2),
        "clocked_hours": round(sum(r.clocked_hours or 0.0 for r in observed), 2),
        "attendance_ratio": round(statistics.fmean(attendance), 4) if attendance else None,
        "avg_wait_minutes": round(statistics.fmean(waits), 2) if waits else None,
        "max_wait_minutes": round(max(waits), 2) if waits else None,
        "avg_task_load": round(statistics.fmean(tasks), 2) if tasks else None,
        "variance_reasons": _count_reasons(observed),
        "absence_share": _share(observed, "absence"),
        "demand_shift_share": _share(observed, "demand-shift"),
        "late_delivery_share": _share(observed, "late-delivery"),
    }
    _flag_service_signals(summary, config, issues)
    return ReviewResult(
        shifts=reviews,
        store_days=store_days,
        issues=list(issues.items),
        assumptions=[
            "只有存在实际覆盖记录的班次进入打卡与工时统计；缺失记录保持未知。",
            "实际覆盖率 = 打卡分钟 / 计划付费分钟（扣除超过阈值的休息）。",
            "服务等待与任务负荷是独立观测，不能仅凭相关就断言由排班造成。",
            "差异原因字段为原始记录，不做自动归因。",
        ],
        summary=summary,
    )


def _count_reasons(rows: Sequence[ShiftReview]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.variance_reason] = counts.get(row.variance_reason, 0) + 1
    return dict(sorted(counts.items()))


def _share(rows: Sequence[ShiftReview], reason: str) -> float | None:
    if not rows:
        return None
    return round(sum(1 for r in rows if r.variance_reason == reason) / len(rows), 4)


def _aggregate_store_days(
    reviews: Sequence[ShiftReview],
    bundle: "DataBundle",
    requirements: "RequirementSet | None",
    config: PlanningConfig,
    issues: IssueBag,
) -> list[StoreDayReview]:
    groups: dict[tuple[str, dt.date], list[ShiftReview]] = {}
    for row in reviews:
        groups.setdefault((row.store_id, row.date), []).append(row)
    coverage_by_store_date: dict[tuple[str, dt.date], float] = {}
    if requirements is not None:
        plan_shifts = [s for s in bundle.shifts if s.shift_id in {r.shift_id for r in reviews}]
        report = compute_coverage(plan_shifts, requirements, included_states=("accepted", "proposed"))
        uncovered: dict[tuple[str, dt.date, str], float] = {}
        for gap in report.gaps:
            key = (gap["store_id"], dt.date.fromisoformat(gap["date"]), gap["role"])
            uncovered[key] = uncovered.get(key, 0.0) + gap["uncovered_hours"]
        totals: dict[tuple[str, dt.date], list[float]] = {}
        for requirement in requirements.requirements:
            key = (requirement.store_id, requirement.date)
            required = requirement.total_hours
            covered = required - uncovered.get((*key, requirement.role), 0.0)
            bucket = totals.setdefault(key, [0.0, 0.0])
            bucket[0] += required
            bucket[1] += max(0.0, covered)
        for key, (required, covered) in totals.items():
            coverage_by_store_date[key] = covered / required if required else 1.0

    result: list[StoreDayReview] = []
    for (store_id, date), rows in sorted(groups.items()):
        planned = sum(r.planned_paid_hours or 0.0 for r in rows)
        observed = [r for r in rows if r.state == "observed"]
        clocked = sum(r.clocked_hours or 0.0 for r in observed)
        waits = [r.service_wait_minutes for r in observed if r.service_wait_minutes is not None]
        tasks = [r.task_load for r in observed if r.task_load is not None]
        reasons = _count_reasons(observed)
        attendance_ratio = clocked / planned if planned > 0 and observed else None
        result.append(
            StoreDayReview(
                store_id=store_id,
                date=date,
                planned_hours=planned,
                clocked_hours=clocked if observed else None,
                attendance_ratio=attendance_ratio,
                shifts_planned=len(rows),
                shifts_observed=len(observed),
                shifts_missing_actual=len(rows) - len(observed),
                coverage_ratio=coverage_by_store_date.get((store_id, date)),
                avg_wait_minutes=statistics.fmean(waits) if waits else None,
                max_wait_minutes=max(waits) if waits else None,
                avg_task_load=statistics.fmean(tasks) if tasks else None,
                variance_reasons=reasons,
            )
        )
    return result


def _flag_service_signals(summary: dict, config: PlanningConfig, issues: IssueBag) -> None:
    wait = summary.get("avg_wait_minutes")
    if wait is not None and wait > config.standards.service_wait_target_minutes:
        issues.warning(
            "review-wait-target",
            (
                f"实际平均服务等待 {wait:.1f} 分钟，高于目标 "
                f"{config.standards.service_wait_target_minutes:.1f} 分钟；需结合覆盖与客流解释"
            ),
        )
    if summary.get("attendance_ratio") is not None and summary["attendance_ratio"] < 0.9:
        issues.warning(
            "review-attendance-low",
            f"实际出勤工时只有计划的 {summary['attendance_ratio']*100:.0f}%",
        )
    if summary.get("absence_share") and summary["absence_share"] > config.review.undercoverage_ratio:
        issues.warning(
            "review-absence",
            f"缺勤差异占比 {summary['absence_share']*100:.0f}%，超过阈值",
        )
