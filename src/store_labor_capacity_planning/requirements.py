"""把小时预测转换为各岗位的覆盖小时需求。

关键点：

* 交易量受收银台开放数量影响，因此销售岗位需求按"交易量 / 单台小时处理能力"计算，
  不足一个整人的部分保留为小数工时，由排班阶段合并。
* 收货、清洁和顾客服务是独立任务，即使销售预测很低也必须有人承担。
* 闭店小时不产生岗位需求，但闭店前/后的一次性收尾任务单独计入。
* 每个需求都带优先级，方案比较和缺口说明按优先级排序。
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from .config import PlanningConfig, ServiceStandards
from .constants import ROLE_LABELS
from .errors import IssueBag
from .models import (
    ForecastCell,
    ForecastResult,
    RequirementSet,
    RoleRequirement,
)


@dataclass
class _DayContext:
    store_id: str
    date: dt.date
    open_cells: list[ForecastCell]
    notes: list[str] = field(default_factory=list)


def _round_hours(value: float) -> float:
    """保留两位小数，避免浮点噪声进入需求表。"""

    return round(max(0.0, value) + 0.0, 2)


def _sales_requirement(cells: list[ForecastCell], standards: ServiceStandards, notes: list[str]) -> dict[int, float]:
    result: dict[int, float] = {}
    for cell in cells:
        transactions = cell.expected_transactions or 0.0
        register_hours = transactions / standards.transactions_per_register_hour
        required = max(register_hours, standards.min_sales_hours_per_open_hour)
        result[cell.hour] = _round_hours(required)
    return result


def _service_requirement(
    cells: list[ForecastCell],
    standards: ServiceStandards,
    notes: list[str],
) -> dict[int, float]:
    result: dict[int, float] = {}
    for cell in cells:
        traffic = cell.expected_traffic or 0.0
        service_hours = (
            traffic / standards.customers_per_service_hour
            + standards.cleaning_hours_per_open_hour
        )
        result[cell.hour] = _round_hours(
            max(service_hours, standards.min_service_hours_per_open_hour)
        )
    # 高峰保护：客流最高的若干小时，服务岗位至少 peak_min_service_hours。
    if cells and standards.peak_hours > 0:
        ranked = sorted(
            cells,
            key=lambda c: (c.expected_traffic or 0.0),
            reverse=True,
        )[: standards.peak_hours]
        for cell in ranked:
            current = result.get(cell.hour, 0.0)
            if current < standards.peak_min_service_hours:
                result[cell.hour] = _round_hours(standards.peak_min_service_hours)
        notes.append(
            f"客流最高的 {len(ranked)} 个小时按高峰标准保证服务岗位覆盖"
        )
    return result


def _fixed_block(
    cells: list[ForecastCell],
    *,
    total_hours: float,
    window_hours: int,
    prefer_earliest: bool,
) -> dict[int, float]:
    """把一天的固定任务排入营业开始后的若干小时。

    固定任务按小时均摊；窗口不足时把剩余工时压入窗口末尾，避免任务丢失。
    """

    if not cells or total_hours <= 0:
        return {}
    ordered = sorted(cells, key=lambda c: c.hour, reverse=not prefer_earliest)
    window = ordered[: max(1, min(window_hours, len(ordered)))]
    per_hour = total_hours / len(window)
    result = {cell.hour: _round_hours(per_hour) for cell in window}
    # 小数舍入的尾差补到窗口最后一个小时。
    diff = _round_hours(total_hours) - _round_hours(sum(result.values()))
    if abs(diff) >= 0.01 and window:
        last = window[-1].hour
        result[last] = _round_hours(result[last] + diff)
    return result


def _closure_tasks(
    cells: list[ForecastCell],
    standards: ServiceStandards,
) -> dict[int, float]:
    """临时闭店当天需要收尾的基本任务，安排在闭店前后仍可用的一个小时。"""

    if not cells:
        return {}
    target = cells[-1]
    return {target.hour: _round_hours(standards.closure_task_hours)}


def build_requirements(
    forecast: ForecastResult,
    config: PlanningConfig,
    *,
    bag: IssueBag | None = None,
) -> RequirementSet:
    issues = bag if bag is not None else IssueBag()
    standards = config.standards
    requirements: list[RoleRequirement] = []
    for day in forecast.days:
        cells = [c for c in day.cells if c.open]
        closed = [c for c in day.cells if not c.open]
        notes: list[str] = []
        if not cells:
            # 当天完全闭店：不产生常规岗位需求，也不把闭店当成"没有顾客"。
            if day.closed_hours:
                issues.info(
                    "requirement-closed-day",
                    f"{day.store_id} {day.date.isoformat()} 全天临时闭店，不生成岗位需求",
                    entity=day.store_id,
                )
            continue
        sales = _sales_requirement(cells, standards, notes)
        service = _service_requirement(cells, standards, notes)
        receiving = _fixed_block(
            cells,
            total_hours=standards.receiving_hours_per_open_day,
            window_hours=standards.receiving_window_hours,
            prefer_earliest=True,
        )
        if receiving:
            notes.append("收货任务安排在营业开始后的窗口内，与销售高低无关")
        food = _fixed_block(
            cells,
            total_hours=standards.food_safety_hours_per_open_day,
            window_hours=standards.food_window_hours,
            prefer_earliest=False,
        )
        if food:
            notes.append("食品与安全检查安排在较晚的窗口内")
        if closed and day.closed_hours:
            extra = _closure_tasks(day.cells, standards)
            for hour, hours in extra.items():
                service[hour] = _round_hours(service.get(hour, 0.0) + hours)
            notes.append("临时闭店时段需要收尾与交接")
        priorities = {"sales": 1.0, "service": 0.9, "receiving": 0.6, "food": 0.5}
        for role, by_hour in (
            ("sales", sales),
            ("service", service),
            ("receiving", receiving),
            ("food", food),
        ):
            if not by_hour:
                continue
            total = _round_hours(sum(by_hour.values()))
            if total <= 0:
                continue
            requirements.append(
                RoleRequirement(
                    store_id=day.store_id,
                    date=day.date,
                    role=role,
                    by_hour={hour: _round_hours(value) for hour, value in sorted(by_hour.items())},
                    priority=priorities[role],
                    notes=tuple(notes),
                )
            )
    issues.info(
        "requirement-model",
        "岗位需求 = 销售(交易/收银处理能力) + 服务(客流/服务能力+清洁) + 固定收货/食品安全任务；固定任务不受销售高低影响",
    )
    return RequirementSet(requirements=requirements, issues=list(issues.items))


def requirement_totals(requirement_set: RequirementSet) -> dict[str, float]:
    totals: dict[str, float] = {}
    for requirement in requirement_set.requirements:
        totals[requirement.role] = totals.get(requirement.role, 0.0) + requirement.total_hours
    return {role: round(value, 2) for role, value in sorted(totals.items())}


def role_name(role: str) -> str:
    return ROLE_LABELS.get(role, role)
