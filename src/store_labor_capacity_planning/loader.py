"""CSV 输入读取与校验。

读取过程不会把缺失或异常值静默转换为 0：每条无法解释的记录都会产生一条问题，
并决定是跳过该行还是保留为"状态未知"。文件编码按 UTF-8（含 BOM）读取。
"""

from __future__ import annotations

import csv
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import IssueBag
from .models import (
    ActualCoverage,
    Activity,
    Availability,
    Budget,
    DemandObservation,
    Employee,
    Shift,
    normalize_acceptance_state,
    normalize_activity_kind,
    normalize_availability_state,
    normalize_preference,
)

DEMAND_FILE = "hourly_demand.csv"
STAFF_FILE = "staff.csv"
AVAILABILITY_FILE = "availability.csv"
SHIFT_FILE = "shift_plans.csv"
ACTUAL_FILE = "actual_coverage.csv"

ACTIVITY_FILE = "activities.csv"
BUDGET_FILE = "budgets.csv"

DEMAND_COLUMNS = [
    "store_id",
    "date",
    "hour",
    "transactions",
    "traffic_count",
    "traffic_coverage",
    "activity_id",
]
STAFF_COLUMNS = ["employee_id", "store_id", "role", "skill", "contract_hours", "region"]
AVAILABILITY_COLUMNS = [
    "employee_id",
    "date",
    "start_hour",
    "end_hour",
    "availability_state",
    "preference",
]
SHIFT_COLUMNS = [
    "shift_id",
    "store_id",
    "date",
    "role",
    "start_hour",
    "end_hour",
    "assigned_employee",
    "acceptance_state",
]
ACTUAL_COLUMNS = [
    "shift_id",
    "clocked_minutes",
    "task_load",
    "service_wait_minutes",
    "variance_reason",
]
ACTIVITY_COLUMNS = [
    "activity_id",
    "store_id",
    "date",
    "kind",
    "start_hour",
    "end_hour",
    "uplift",
    "note",
]
BUDGET_COLUMNS = ["region", "period_start", "period_end", "amount", "currency"]

_TRUTHY = {"1", "true", "yes", "y", "是"}


class DataError(ValueError):
    """输入资料无法继续处理。"""


@dataclass
class DataBundle:
    demand: list[DemandObservation] = field(default_factory=list)
    staff: list[Employee] = field(default_factory=list)
    availability: list[Availability] = field(default_factory=list)
    shifts: list[Shift] = field(default_factory=list)
    actual: list[ActualCoverage] = field(default_factory=list)
    activities: list[Activity] = field(default_factory=list)
    budgets: list[Budget] = field(default_factory=list)
    issues: IssueBag = field(default_factory=IssueBag)
    source_files: dict[str, str] = field(default_factory=dict)

    # -- 索引 -------------------------------------------------------------

    @property
    def staff_by_id(self) -> dict[str, Employee]:
        return getattr(self, "_staff_by_id", None) or self._build_staff_index()

    def _build_staff_index(self) -> dict[str, Employee]:
        index = {s.employee_id: s for s in self.staff}
        object.__setattr__(self, "_staff_by_id", index)
        return index

    @property
    def shifts_by_id(self) -> dict[str, Shift]:
        index = getattr(self, "_shifts_by_id", None)
        if index is None:
            index = {s.shift_id: s for s in self.shifts}
            object.__setattr__(self, "_shifts_by_id", index)
        return index

    @property
    def actual_by_shift(self) -> dict[str, ActualCoverage]:
        return {a.shift_id: a for a in self.actual}

    @property
    def staff_by_store(self) -> dict[str, list[Employee]]:
        result: dict[str, list[Employee]] = {}
        for member in self.staff:
            result.setdefault(member.store_id, []).append(member)
        for rows in result.values():
            rows.sort(key=lambda e: e.employee_id)
        return result

    @property
    def availability_by_employee_date(self) -> dict[tuple[str, dt.date], list[Availability]]:
        result: dict[tuple[str, dt.date], list[Availability]] = {}
        for entry in self.availability:
            result.setdefault((entry.employee_id, entry.date), []).append(entry)
        for rows in result.values():
            rows.sort(key=lambda a: (a.start_hour, a.end_hour))
        return result

    @property
    def stores(self) -> list[str]:
        names = {e.store_id for e in self.staff}
        names |= {d.store_id for d in self.demand}
        names |= {s.store_id for s in self.shifts}
        names |= {a.store_id for a in self.activities}
        return sorted(names)

    @property
    def demand_stores(self) -> list[str]:
        return sorted({d.store_id for d in self.demand})

    @property
    def staff_stores(self) -> list[str]:
        return sorted({e.store_id for e in self.staff})

    @property
    def dates(self) -> list[dt.date]:
        dates = {d.date for d in self.demand} | {s.date for s in self.shifts}
        return sorted(dates)

    def availability_for(self, employee_id: str, date: dt.date) -> list[Availability]:
        return self.availability_by_employee_date.get((employee_id, date), [])

    def activities_on(self, store_id: str, date: dt.date) -> list[Activity]:
        return [a for a in self.activities if a.store_id == store_id and a.date == date]

    def budget_for(self, region: str, date: dt.date) -> Budget | None:
        candidates = [
            b for b in self.budgets if b.region == region and b.covers(date)
        ]
        if not candidates:
            return None
        # 区间更窄的预算优先，便于按周/按日细化。
        return min(
            candidates,
            key=lambda b: (b.period_end - b.period_start),
        )


# ---------------------------------------------------------------------------
# 读取基础工具
# ---------------------------------------------------------------------------


def _read_rows(path: Path, required: list[str], bag: IssueBag) -> list[dict[str, str]]:
    if not path.exists():
        bag.error(
            "file-missing",
            f"缺少资料文件 {path.name}",
            source=path.name,
            blocks_publication=True,
        )
        return []
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            header = reader.fieldnames or []
            missing = [c for c in required if c not in header]
            if missing:
                bag.error(
                    "column-missing",
                    f"{path.name} 缺少字段: {', '.join(missing)}",
                    source=path.name,
                    blocks_publication=True,
                )
                return []
            extra = [c for c in header if c not in required]
            if extra:
                bag.warning(
                    "column-extra",
                    f"{path.name} 含有未识别字段: {', '.join(extra)}（已忽略）",
                    source=path.name,
                )
            rows: list[dict[str, str]] = []
            for number, raw in enumerate(reader, start=2):
                if raw is None:
                    continue
                cleaned = {
                    key: (value if isinstance(value, str) else "")
                    for key, value in raw.items()
                    if key is not None
                }
                if all(not value.strip() for value in cleaned.values()):
                    bag.warning(
                        "blank-row",
                        f"{path.name} 第 {number} 行为空行，已忽略",
                        source=path.name,
                        row=number,
                    )
                    continue
                if None in raw:
                    bag.error(
                        "row-extra-values",
                        f"{path.name} 第 {number} 行列数多于表头，已跳过",
                        source=path.name,
                        row=number,
                    )
                    continue
                cleaned["_row"] = number
                rows.append(cleaned)
            return rows
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        bag.error(
            "file-unreadable",
            f"{path.name} 无法读取: {exc}",
            source=path.name,
            blocks_publication=True,
        )
        return []


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _parse_date(value: str, *, source: str, row: int, field_name: str, bag: IssueBag) -> dt.date | None:
    text = _clean(value)
    if not text:
        bag.error(
            "field-missing",
            f"{source} 第 {row} 行缺少 {field_name}",
            source=source,
            row=row,
        )
        return None
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        bag.error(
            "date-invalid",
            f"{source} 第 {row} 行 {field_name}={text!r} 不是 ISO 日期",
            source=source,
            row=row,
        )
        return None


def _parse_int(
    value: str,
    *,
    source: str,
    row: int,
    field_name: str,
    bag: IssueBag,
    minimum: int | None = None,
    maximum: int | None = None,
    required: bool = True,
) -> int | None:
    text = _clean(value)
    if not text:
        if required:
            bag.error(
                "field-missing",
                f"{source} 第 {row} 行缺少 {field_name}",
                source=source,
                row=row,
            )
        return None
    try:
        number = int(text)
    except ValueError:
        try:
            as_float = float(text)
        except ValueError:
            bag.error(
                "number-invalid",
                f"{source} 第 {row} 行 {field_name}={text!r} 不是整数",
                source=source,
                row=row,
            )
            return None
        if abs(as_float - round(as_float)) > 1e-9:
            bag.error(
                "number-invalid",
                f"{source} 第 {row} 行 {field_name}={text!r} 必须是整数",
                source=source,
                row=row,
            )
            return None
        number = int(round(as_float))
    if minimum is not None and number < minimum:
        bag.error(
            "number-range",
            f"{source} 第 {row} 行 {field_name}={number} 小于下限 {minimum}",
            source=source,
            row=row,
        )
        return None
    if maximum is not None and number > maximum:
        bag.error(
            "number-range",
            f"{source} 第 {row} 行 {field_name}={number} 大于上限 {maximum}",
            source=source,
            row=row,
        )
        return None
    return number


def _parse_float(
    value: str,
    *,
    source: str,
    row: int,
    field_name: str,
    bag: IssueBag,
    minimum: float | None = None,
    maximum: float | None = None,
    required: bool = False,
) -> float | None:
    text = _clean(value)
    if not text:
        if required:
            bag.error(
                "field-missing",
                f"{source} 第 {row} 行缺少 {field_name}",
                source=source,
                row=row,
            )
        return None
    try:
        number = float(text)
    except ValueError:
        bag.error(
            "number-invalid",
            f"{source} 第 {row} 行 {field_name}={text!r} 不是数字",
            source=source,
            row=row,
        )
        return None
    if number != number or number in (float("inf"), float("-inf")):
        bag.error(
            "number-invalid",
            f"{source} 第 {row} 行 {field_name} 不是有限数值",
            source=source,
            row=row,
        )
        return None
    if minimum is not None and number < minimum:
        bag.error(
            "number-range",
            f"{source} 第 {row} 行 {field_name}={number} 小于下限 {minimum}",
            source=source,
            row=row,
        )
        return None
    if maximum is not None and number > maximum:
        bag.error(
            "number-range",
            f"{source} 第 {row} 行 {field_name}={number} 大于上限 {maximum}",
            source=source,
            row=row,
        )
        return None
    return number


# ---------------------------------------------------------------------------
# 各文件的读取
# ---------------------------------------------------------------------------


def _load_demand(path: Path, bag: IssueBag) -> list[DemandObservation]:
    rows = _read_rows(path, DEMAND_COLUMNS, bag)
    result: list[DemandObservation] = []
    seen: dict[tuple[str, dt.date, int], int] = {}
    for raw in rows:
        row = raw["_row"]
        store = _clean(raw["store_id"])
        if not store:
            bag.error("field-missing", f"{path.name} 第 {row} 行缺少 store_id", source=path.name, row=row)
            continue
        date = _parse_date(raw["date"], source=path.name, row=row, field_name="date", bag=bag)
        hour = _parse_int(
            raw["hour"], source=path.name, row=row, field_name="hour", bag=bag, minimum=0, maximum=23
        )
        if date is None or hour is None:
            continue
        transactions = _parse_float(
            raw["transactions"],
            source=path.name,
            row=row,
            field_name="transactions",
            bag=bag,
            minimum=0,
        )
        traffic = _parse_float(
            raw["traffic_count"],
            source=path.name,
            row=row,
            field_name="traffic_count",
            bag=bag,
            minimum=0,
        )
        coverage = _parse_float(
            raw["traffic_coverage"],
            source=path.name,
            row=row,
            field_name="traffic_coverage",
            bag=bag,
            minimum=0,
            maximum=1,
        )
        if _clean(raw["traffic_coverage"]) and coverage is None:
            # 覆盖率写错时不要把它当成 0 覆盖，用 None 表示未知。
            coverage = None
        if transactions is None and _clean(raw["transactions"]):
            continue
        activity = _clean(raw["activity_id"]) or None
        key = (store, date, hour)
        if key in seen:
            bag.error(
                "demand-duplicate",
                f"{path.name} 第 {row} 行与第 {seen[key]} 行的门店/日期/小时重复",
                source=path.name,
                row=row,
                entity=store,
            )
            continue
        seen[key] = row
        if coverage == 0 and traffic not in (None, 0):
            bag.warning(
                "traffic-uncovered",
                f"{path.name} 第 {row} 行覆盖率为 0，客流计数不能代表全量客流",
                source=path.name,
                row=row,
                entity=store,
            )
        result.append(
            DemandObservation(
                store_id=store,
                date=date,
                hour=hour,
                transactions=transactions,
                traffic_count=traffic,
                traffic_coverage=coverage,
                activity_id=activity,
                row=row,
            )
        )
    return result


def _load_staff(path: Path, bag: IssueBag) -> list[Employee]:
    rows = _read_rows(path, STAFF_COLUMNS, bag)
    result: list[Employee] = []
    seen: dict[str, int] = {}
    for raw in rows:
        row = raw["_row"]
        employee_id = _clean(raw["employee_id"])
        store_id = _clean(raw["store_id"])
        role = _clean(raw["role"]).lower()
        skill = _clean(raw["skill"]).lower()
        region = _clean(raw["region"])
        if not employee_id or not store_id or not role:
            bag.error(
                "field-missing",
                f"{path.name} 第 {row} 行缺少 employee_id/store_id/role",
                source=path.name,
                row=row,
            )
            continue
        if employee_id in seen:
            bag.error(
                "employee-duplicate",
                f"{path.name} 第 {row} 行员工 {employee_id} 与第 {seen[employee_id]} 行重复",
                source=path.name,
                row=row,
                entity=employee_id,
            )
            continue
        seen[employee_id] = row
        contract = _parse_float(
            raw["contract_hours"],
            source=path.name,
            row=row,
            field_name="contract_hours",
            bag=bag,
            minimum=0,
            maximum=80,
            required=True,
        )
        if contract is None:
            continue
        if not skill:
            bag.warning(
                "skill-missing",
                f"{path.name} 第 {row} 行员工 {employee_id} 缺少技能，将按岗位默认技能推断",
                source=path.name,
                row=row,
                entity=employee_id,
            )
        if not region:
            bag.error(
                "region-missing",
                f"{path.name} 第 {row} 行员工 {employee_id} 缺区域，无法做预算核算",
                source=path.name,
                row=row,
                entity=employee_id,
            )
        result.append(
            Employee(
                employee_id=employee_id,
                store_id=store_id,
                role=role,
                skill=skill,
                contract_hours=contract,
                region=region,
                row=row,
            )
        )
    return result


def _load_availability(path: Path, bag: IssueBag) -> list[Availability]:
    rows = _read_rows(path, AVAILABILITY_COLUMNS, bag)
    result: list[Availability] = []
    seen: set[tuple[str, dt.date, int, int]] = set()
    for raw in rows:
        row = raw["_row"]
        employee_id = _clean(raw["employee_id"])
        if not employee_id:
            bag.error("field-missing", f"{path.name} 第 {row} 行缺少 employee_id", source=path.name, row=row)
            continue
        date = _parse_date(raw["date"], source=path.name, row=row, field_name="date", bag=bag)
        start = _parse_int(
            raw["start_hour"], source=path.name, row=row, field_name="start_hour", bag=bag, minimum=0, maximum=24
        )
        end = _parse_int(
            raw["end_hour"], source=path.name, row=row, field_name="end_hour", bag=bag, minimum=0, maximum=24
        )
        if date is None or start is None or end is None:
            continue
        if end <= start:
            bag.error(
                "window-invalid",
                f"{path.name} 第 {row} 行可用时段 {start}-{end} 结束不晚于开始",
                source=path.name,
                row=row,
                entity=employee_id,
            )
            continue
        state_raw = _clean(raw["availability_state"])
        state = normalize_availability_state(state_raw)
        if state == "unknown":
            bag.warning(
                "availability-unknown",
                f"{path.name} 第 {row} 行可用状态 {state_raw!r} 无法识别，按未知处理",
                source=path.name,
                row=row,
                entity=employee_id,
            )
        preference = normalize_preference(raw["preference"])
        key = (employee_id, date, start, end)
        if key in seen:
            bag.warning(
                "availability-duplicate",
                f"{path.name} 第 {row} 行与已有可用时段重复，保留先出现的记录",
                source=path.name,
                row=row,
                entity=employee_id,
            )
            continue
        seen.add(key)
        result.append(
            Availability(
                employee_id=employee_id,
                date=date,
                start_hour=start,
                end_hour=end,
                state=state,
                preference=preference,
                row=row,
            )
        )
    return result


def _load_shifts(path: Path, bag: IssueBag) -> list[Shift]:
    rows = _read_rows(path, SHIFT_COLUMNS, bag)
    result: list[Shift] = []
    seen: dict[str, int] = {}
    for raw in rows:
        row = raw["_row"]
        shift_id = _clean(raw["shift_id"])
        store_id = _clean(raw["store_id"])
        employee_id = _clean(raw["assigned_employee"])
        role = _clean(raw["role"]).lower()
        if not shift_id or not store_id or not role:
            bag.error(
                "field-missing",
                f"{path.name} 第 {row} 行缺少 shift_id/store_id/role",
                source=path.name,
                row=row,
            )
            continue
        if shift_id in seen:
            bag.error(
                "shift-duplicate",
                f"{path.name} 第 {row} 行班次 {shift_id} 与第 {seen[shift_id]} 行重复",
                source=path.name,
                row=row,
                entity=shift_id,
            )
            continue
        seen[shift_id] = row
        date = _parse_date(raw["date"], source=path.name, row=row, field_name="date", bag=bag)
        start = _parse_int(
            raw["start_hour"], source=path.name, row=row, field_name="start_hour", bag=bag, minimum=0, maximum=24
        )
        end = _parse_int(
            raw["end_hour"], source=path.name, row=row, field_name="end_hour", bag=bag, minimum=0, maximum=30
        )
        if date is None or start is None or end is None:
            continue
        if end <= start:
            bag.error(
                "window-invalid",
                f"{path.name} 第 {row} 行班次 {shift_id} 结束小时不晚于开始",
                source=path.name,
                row=row,
                entity=shift_id,
            )
            continue
        if not employee_id:
            bag.error(
                "assignment-missing",
                f"{path.name} 第 {row} 行班次 {shift_id} 未指派员工",
                source=path.name,
                row=row,
                entity=shift_id,
            )
            continue
        state_raw = _clean(raw["acceptance_state"])
        state = normalize_acceptance_state(state_raw)
        if state == "unknown":
            bag.warning(
                "acceptance-unknown",
                f"{path.name} 第 {row} 行班次 {shift_id} 接受状态 {state_raw!r} 无法识别，按未知处理",
                source=path.name,
                row=row,
                entity=shift_id,
            )
        result.append(
            Shift(
                shift_id=shift_id,
                store_id=store_id,
                date=date,
                role=role,
                start_hour=start,
                end_hour=end,
                assigned_employee=employee_id,
                acceptance_state=state,
                origin="imported",
            )
        )
    return result


def _load_actual(path: Path, bag: IssueBag) -> list[ActualCoverage]:
    rows = _read_rows(path, ACTUAL_COLUMNS, bag)
    result: list[ActualCoverage] = []
    seen: dict[str, int] = {}
    for raw in rows:
        row = raw["_row"]
        shift_id = _clean(raw["shift_id"])
        if not shift_id:
            bag.error("field-missing", f"{path.name} 第 {row} 行缺少 shift_id", source=path.name, row=row)
            continue
        if shift_id in seen:
            bag.error(
                "actual-duplicate",
                f"{path.name} 第 {row} 行班次 {shift_id} 的实际记录重复",
                source=path.name,
                row=row,
                entity=shift_id,
            )
            continue
        seen[shift_id] = row
        clocked = _parse_int(
            raw["clocked_minutes"],
            source=path.name,
            row=row,
            field_name="clocked_minutes",
            bag=bag,
            minimum=0,
        )
        task_load = _parse_float(
            raw["task_load"], source=path.name, row=row, field_name="task_load", bag=bag, minimum=0
        )
        wait = _parse_float(
            raw["service_wait_minutes"],
            source=path.name,
            row=row,
            field_name="service_wait_minutes",
            bag=bag,
            minimum=0,
        )
        reason = _clean(raw["variance_reason"]) or "none"
        result.append(
            ActualCoverage(
                shift_id=shift_id,
                clocked_minutes=clocked,
                task_load=task_load,
                service_wait_minutes=wait,
                variance_reason=reason,
                row=row,
            )
        )
    return result


def _load_activities(path: Path, bag: IssueBag) -> list[Activity]:
    if not path.exists():
        bag.info(
            "activities-absent",
            f"未提供 {path.name}，不应用活动或临时闭店调整",
            source=path.name,
        )
        return []
    rows = _read_rows(path, ACTIVITY_COLUMNS, bag)
    result: list[Activity] = []
    seen: dict[str, int] = {}
    for raw in rows:
        row = raw["_row"]
        activity_id = _clean(raw["activity_id"])
        store_id = _clean(raw["store_id"])
        if not activity_id or not store_id:
            bag.error(
                "field-missing",
                f"{path.name} 第 {row} 行缺少 activity_id/store_id",
                source=path.name,
                row=row,
            )
            continue
        if activity_id in seen:
            bag.error(
                "activity-duplicate",
                f"{path.name} 第 {row} 行活动 {activity_id} 重复",
                source=path.name,
                row=row,
                entity=activity_id,
            )
            continue
        seen[activity_id] = row
        date = _parse_date(raw["date"], source=path.name, row=row, field_name="date", bag=bag)
        start = _parse_int(
            raw["start_hour"], source=path.name, row=row, field_name="start_hour", bag=bag, minimum=0, maximum=23
        )
        end = _parse_int(
            raw["end_hour"], source=path.name, row=row, field_name="end_hour", bag=bag, minimum=1, maximum=24
        )
        if date is None or start is None or end is None:
            continue
        if end <= start:
            bag.error(
                "window-invalid",
                f"{path.name} 第 {row} 行活动 {activity_id} 的时段结束不晚于开始",
                source=path.name,
                row=row,
                entity=activity_id,
            )
            continue
        kind_raw = _clean(raw["kind"])
        kind = normalize_activity_kind(kind_raw)
        uplift = _parse_float(
            raw["uplift"],
            source=path.name,
            row=row,
            field_name="uplift",
            bag=bag,
            minimum=-1,
            maximum=5,
        )
        if kind == "promotion" and uplift is None:
            bag.warning(
                "uplift-missing",
                f"{path.name} 第 {row} 行促销活动 {activity_id} 未给 uplift，按 0 处理",
                source=path.name,
                row=row,
                entity=activity_id,
            )
        result.append(
            Activity(
                activity_id=activity_id,
                store_id=store_id,
                date=date,
                kind=kind,
                start_hour=start,
                end_hour=end,
                uplift=uplift,
                note=_clean(raw.get("note", "")),
                row=row,
            )
        )
    return result


def _load_budgets(path: Path, bag: IssueBag) -> list[Budget]:
    if not path.exists():
        bag.info(
            "budgets-absent",
            f"未提供 {path.name}，方案比较不显示预算差异",
            source=path.name,
        )
        return []
    rows = _read_rows(path, BUDGET_COLUMNS, bag)
    result: list[Budget] = []
    for raw in rows:
        row = raw["_row"]
        region = _clean(raw["region"])
        if not region:
            bag.error("field-missing", f"{path.name} 第 {row} 行缺少 region", source=path.name, row=row)
            continue
        start = _parse_date(raw["period_start"], source=path.name, row=row, field_name="period_start", bag=bag)
        end = _parse_date(raw["period_end"], source=path.name, row=row, field_name="period_end", bag=bag)
        amount = _parse_float(
            raw["amount"], source=path.name, row=row, field_name="amount", bag=bag, minimum=0, required=True
        )
        if start is None or end is None or amount is None:
            continue
        if end < start:
            bag.error(
                "window-invalid",
                f"{path.name} 第 {row} 行预算区间结束早于开始",
                source=path.name,
                row=row,
                entity=region,
            )
            continue
        result.append(
            Budget(
                region=region,
                period_start=start,
                period_end=end,
                amount=amount,
                currency=_clean(raw["currency"]) or "元",
                row=row,
            )
        )
    return result


# ---------------------------------------------------------------------------
# 关联校验
# ---------------------------------------------------------------------------


def _validate_references(
    demand: list[DemandObservation],
    staff: list[Employee],
    availability: list[Availability],
    shifts: list[Shift],
    actual: list[ActualCoverage],
    activities: list[Activity],
    bag: IssueBag,
) -> None:
    staff_ids = {e.employee_id for e in staff}
    staff_by_id = {e.employee_id: e for e in staff}
    shift_ids = {s.shift_id for s in shifts}
    activity_ids = {a.activity_id for a in activities}

    demand_stores = {d.store_id for d in demand}
    staff_stores = {e.store_id for e in staff}
    if demand_stores - staff_stores:
        missing = ", ".join(sorted(demand_stores - staff_stores))
        bag.warning(
            "store-without-staff",
            f"有需求记录但无员工资料的门店: {missing}（无法排班）",
            entity=missing,
        )
    if staff_stores - demand_stores:
        missing = ", ".join(sorted(staff_stores - demand_stores))
        bag.warning(
            "store-without-demand",
            f"有员工资料但无历史需求的门店: {missing}（预测将使用全局均值）",
            entity=missing,
        )

    for entry in availability:
        if entry.employee_id not in staff_ids:
            bag.error(
                "availability-unknown-employee",
                f"可用时段引用了不存在的员工 {entry.employee_id}",
                source=AVAILABILITY_FILE,
                row=entry.row,
                entity=entry.employee_id,
            )

    for shift in shifts:
        member = staff_by_id.get(shift.assigned_employee)
        if member is None:
            bag.error(
                "shift-unknown-employee",
                f"班次 {shift.shift_id} 指派的员工 {shift.assigned_employee} 不存在",
                source=SHIFT_FILE,
                entity=shift.shift_id,
            )
            continue
        if member.store_id != shift.store_id:
            bag.warning(
                "shift-cross-store",
                f"班次 {shift.shift_id} 在 {shift.store_id}，但 {member.employee_id} 属于 {member.store_id}",
                source=SHIFT_FILE,
                entity=shift.shift_id,
            )
        if member.role != shift.role:
            bag.warning(
                "shift-role-mismatch",
                f"班次 {shift.shift_id} 岗位为 {shift.role}，但 {member.employee_id} 的岗位是 {member.role}",
                source=SHIFT_FILE,
                entity=shift.shift_id,
            )

    for record in actual:
        if record.shift_id not in shift_ids:
            bag.error(
                "actual-unknown-shift",
                f"实际覆盖引用了不存在的班次 {record.shift_id}",
                source=ACTUAL_FILE,
                row=record.row,
                entity=record.shift_id,
            )

    covered = {a.shift_id for a in actual}
    for shift in shifts:
        if shift.shift_id not in covered:
            bag.info(
                "actual-pending",
                f"班次 {shift.shift_id} 尚无实际覆盖记录，覆盖结果未知",
                source=ACTUAL_FILE,
                entity=shift.shift_id,
            )

    for entry in demand:
        if entry.activity_id and entry.activity_id not in activity_ids and activities:
            bag.info(
                "demand-activity-unlisted",
                f"{entry.store_id} {entry.date.isoformat()} {entry.hour} 时引用活动 {entry.activity_id}，但活动资料中没有该编号；历史活动只作为标签",
                source=DEMAND_FILE,
                row=entry.row,
                entity=entry.activity_id,
            )


def load_bundle(
    data_dir: str | Path,
    *,
    activities_path: str | Path | None = None,
    budgets_path: str | Path | None = None,
) -> DataBundle:
    """读取一个资料目录，返回实体与问题清单。

    基础五个文件缺失属于阻断错误；活动和预算文件可选。
    """

    root = Path(data_dir)
    if not root.exists() or not root.is_dir():
        raise DataError(f"资料目录不存在或不是目录: {root}")
    bag = IssueBag()
    files = {
        DEMAND_FILE: root / DEMAND_FILE,
        STAFF_FILE: root / STAFF_FILE,
        AVAILABILITY_FILE: root / AVAILABILITY_FILE,
        SHIFT_FILE: root / SHIFT_FILE,
        ACTUAL_FILE: root / ACTUAL_FILE,
    }
    demand = _load_demand(files[DEMAND_FILE], bag)
    staff = _load_staff(files[STAFF_FILE], bag)
    availability = _load_availability(files[AVAILABILITY_FILE], bag)
    shifts = _load_shifts(files[SHIFT_FILE], bag)
    actual = _load_actual(files[ACTUAL_FILE], bag)
    activity_file = Path(activities_path) if activities_path else root / ACTIVITY_FILE
    budget_file = Path(budgets_path) if budgets_path else root / BUDGET_FILE
    activities = _load_activities(activity_file, bag)
    budgets = _load_budgets(budget_file, bag)
    _validate_references(demand, staff, availability, shifts, actual, activities, bag)
    bundle = DataBundle(
        demand=demand,
        staff=staff,
        availability=availability,
        shifts=shifts,
        actual=actual,
        activities=activities,
        budgets=budgets,
        issues=bag,
        source_files={name: str(path) for name, path in files.items()},
    )
    return bundle


def require_core_data(bundle: DataBundle) -> None:
    """在无法做任何有意义的计算时抛出 DataError。"""

    if not bundle.demand:
        raise DataError("没有可用的历史需求记录，无法预测")
    if not bundle.staff:
        raise DataError("没有员工资料，无法生成排班")
