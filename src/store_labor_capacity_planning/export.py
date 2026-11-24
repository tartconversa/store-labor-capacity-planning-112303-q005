"""CSV/JSON 导出。

导出为确定性、可复核的表格；缺失值输出空字符串而不是 0。所有金额和工时保留两位小数，
日期使用 ISO 格式。文件以 UTF-8（带 BOM）写出，方便中文表格软件直接打开。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .constants import ROLE_LABELS
from .models import Comparison, ForecastResult, RequirementSet, ScenarioPlan
from .scheduler import CoverageReport, paid_hours
from .review import ReviewResult

SHIFT_EXPORT_COLUMNS = [
    "scenario",
    "revision",
    "shift_id",
    "store_id",
    "date",
    "role",
    "role_label",
    "start_hour",
    "end_hour",
    "hours",
    "paid_hours",
    "employee_id",
    "employee_store",
    "employee_region",
    "acceptance_state",
    "origin",
    "cost",
    "notes",
]


def _write_csv(path: Path, columns: Sequence[str], rows: Iterable[Mapping]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: _cell(row.get(column)) for column in columns})
    return path


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        if abs(value - round(value)) < 1e-9:
            return str(int(round(value)))
        return f"{value:.2f}"
    return str(value)


def export_shifts(
    path: str | Path,
    plan: ScenarioPlan,
    *,
    bundle,
    config,
) -> Path:
    from .scheduler import shift_cost

    rows = []
    for shift in sorted(plan.shifts, key=lambda s: (s.date, s.start_hour, s.shift_id)):
        employee = bundle.staff_by_id.get(shift.assigned_employee)
        cost = shift_cost(shift, employee, config) if employee else None
        rows.append(
            {
                "scenario": plan.key,
                "revision": plan.revision,
                "shift_id": shift.shift_id,
                "store_id": shift.store_id,
                "date": shift.date,
                "role": shift.role,
                "role_label": ROLE_LABELS.get(shift.role, shift.role),
                "start_hour": shift.start_hour,
                "end_hour": shift.end_hour,
                "hours": shift.hours,
                "paid_hours": paid_hours(shift, config.constraints),
                "employee_id": shift.assigned_employee,
                "employee_store": employee.store_id if employee else None,
                "employee_region": employee.region if employee else None,
                "acceptance_state": shift.acceptance_state,
                "origin": shift.origin,
                "cost": cost,
                "notes": ";".join(shift.notes),
            }
        )
    return _write_csv(Path(path), SHIFT_EXPORT_COLUMNS, rows)


def export_forecast(path: str | Path, forecast: ForecastResult) -> Path:
    columns = [
        "store_id",
        "date",
        "hour",
        "open",
        "expected_traffic",
        "traffic_lower",
        "traffic_upper",
        "expected_transactions",
        "confidence",
        "traffic_basis",
        "activity_id",
        "notes",
    ]
    rows = [cell.to_row() for cell in forecast.cells]
    return _write_csv(Path(path), columns, rows)


def export_requirements(path: str | Path, requirements: RequirementSet) -> Path:
    columns = [
        "store_id",
        "date",
        "role",
        "role_label",
        "hour",
        "required_hours",
        "priority",
    ]
    rows = []
    for requirement in sorted(
        requirements.requirements, key=lambda r: (r.date, r.store_id, r.role)
    ):
        for hour, hours in sorted(requirement.by_hour.items()):
            rows.append(
                {
                    "store_id": requirement.store_id,
                    "date": requirement.date.isoformat(),
                    "role": requirement.role,
                    "role_label": ROLE_LABELS.get(requirement.role, requirement.role),
                    "hour": hour,
                    "required_hours": hours,
                    "priority": requirement.priority,
                }
            )
    return _write_csv(Path(path), columns, rows)


def export_comparison(path: str | Path, comparison: Comparison) -> Path:
    if not comparison.rows:
        return _write_csv(Path(path), ["scenario"], [])
    columns = list(comparison.rows[0].keys())
    return _write_csv(Path(path), columns, comparison.rows)


def export_coverage(path: str | Path, plan: ScenarioPlan, coverage: CoverageReport) -> Path:
    columns = [
        "store_id",
        "date",
        "role",
        "role_label",
        "required_hours",
        "covered_hours",
        "uncovered_hours",
        "coverage_ratio",
        "priority",
    ]
    rows = [
        {
            **gap,
            "role_label": ROLE_LABELS.get(gap["role"], gap["role"]),
        }
        for gap in sorted(
            coverage.gaps, key=lambda g: (g["date"], g["store_id"], g["role"])
        )
    ]
    return _write_csv(Path(path), columns, rows)


def export_review_shifts(path: str | Path, review: ReviewResult) -> Path:
    columns = [
        "shift_id",
        "store_id",
        "date",
        "role",
        "role_label",
        "employee_id",
        "acceptance_state",
        "planned_hours",
        "planned_paid_hours",
        "clocked_minutes",
        "clocked_hours",
        "attendance_ratio",
        "variance_reason",
        "variance_label",
        "state",
    ]
    return _write_csv(Path(path), columns, review.shift_rows())


def export_review_store_days(path: str | Path, review: ReviewResult) -> Path:
    columns = [
        "store_id",
        "date",
        "planned_hours",
        "clocked_hours",
        "attendance_ratio",
        "shifts_planned",
        "shifts_observed",
        "shifts_missing_actual",
        "coverage_ratio",
        "avg_wait_minutes",
        "max_wait_minutes",
        "avg_task_load",
        "variance_reasons",
    ]
    return _write_csv(Path(path), columns, review.store_day_rows())


def export_issues(path: str | Path, issues) -> Path:
    columns = [
        "severity",
        "severity_label",
        "code",
        "message",
        "source",
        "row",
        "entity",
        "blocks_publication",
    ]
    return _write_csv(Path(path), columns, [issue.to_dict() for issue in issues.sorted_items()])


def write_json(path: str | Path, payload) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )
    return target


def _json_default(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"无法序列化的类型: {type(value)!r}")
