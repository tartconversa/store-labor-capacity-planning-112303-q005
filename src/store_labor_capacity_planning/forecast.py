"""小时级客流与交易预测。

设计原则：

* 缺失的客流记录不参与均值，也不等同于 0；只有 ``traffic_coverage > 0``
  的计数才会按覆盖率放大后使用。
* 预测只使用预测起点之前的观测，避免把未来数据泄漏进计划。
* 门店样本很少时向全局均值收缩；没有门店历史时退化为全局基线并显式标注。
* 闭店时段明确为"不营业"，促销时段按配置的 uplift 放大；两者都不是缺失。
"""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Sequence

from .config import ForecastConfig, PlanningConfig
from .errors import IssueBag
from .models import (
    Activity,
    DemandObservation,
    DayForecast,
    ForecastCell,
    ForecastResult,
)

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查
    from .loader import DataBundle


@dataclass
class _StoreModel:
    store_id: str
    base_transactions: float
    conversion: float
    observations: int
    traffic_observations: int
    confidence: float
    basis: str
    open_hour: int
    close_hour: int
    notes: list[str] = field(default_factory=list)
    residual_cv: float = 0.30


@dataclass
class _Factors:
    hour: dict[int, float]
    weekday: dict[int, float]


def _shrunk_ratio(
    values: Iterable[float],
    *,
    shrink: float,
    default: float = 1.0,
) -> float:
    """把样本均值向 ``default`` 收缩；样本越少越靠近默认值。"""

    data = [v for v in values if v is not None and v > 0]
    if not data:
        return default
    mean = statistics.fmean(data)
    weight = len(data) / (len(data) + max(shrink, 1e-9))
    return weight * mean + (1 - weight) * default


def _store_levels(
    demand: Sequence[DemandObservation],
) -> tuple[dict[str, float], float]:
    """每个门店的基准交易量，以及全部门店的全局中位数。"""

    by_store: dict[str, list[float]] = {}
    for row in demand:
        if row.transactions is None:
            continue
        by_store.setdefault(row.store_id, []).append(row.transactions)
    levels = {
        store: statistics.median(values)
        for store, values in by_store.items()
        if values
    }
    global_median = statistics.median(levels.values()) if levels else 0.0
    return levels, global_median


def _normalize_factors(factors: dict[int, float], expected_keys: Iterable[int]) -> dict[int, float]:
    if not factors:
        return {k: 1.0 for k in expected_keys}
    mean = statistics.fmean(factors.values())
    if mean <= 0:
        return {k: 1.0 for k in expected_keys}
    normalized = {k: v / mean for k, v in factors.items()}
    return {k: normalized.get(k, 1.0) for k in expected_keys}


def _conversion_rate(
    demand: Sequence[DemandObservation],
    config: ForecastConfig,
) -> tuple[dict[str, float], float]:
    """估计各门店的交易/客流转化率，并用全局中位数收缩。"""

    by_store: dict[str, list[float]] = {}
    for row in demand:
        conversion = row.conversion
        if conversion is None:
            continue
        if conversion <= 0:
            continue
        by_store.setdefault(row.store_id, []).append(conversion)
    global_values = [v for values in by_store.values() for v in values]
    global_rate = (
        statistics.median(global_values)
        if global_values
        else config.default_conversion_rate
    )
    global_rate = min(
        max(global_rate, config.min_conversion_rate), config.max_conversion_rate
    )
    rates: dict[str, float] = {}
    for store, values in by_store.items():
        rate = _shrunk_ratio(
            values,
            shrink=config.activity_shrink_observations,
            default=global_rate,
        )
        rates[store] = min(
            max(rate, config.min_conversion_rate), config.max_conversion_rate
        )
    return rates, global_rate


def _open_hours(demand: Sequence[DemandObservation], config: ForecastConfig) -> dict[str, tuple[int, int]]:
    by_store: dict[str, list[int]] = {}
    for row in demand:
        by_store.setdefault(row.store_id, []).append(row.hour)
    result: dict[str, tuple[int, int]] = {}
    for store, hours in by_store.items():
        result[store] = (min(hours), max(hours) + 1)
    return result


def _activity_for(
    activities: Sequence[Activity],
    *,
    store_id: str,
    date: dt.date,
) -> list[Activity]:
    return [a for a in activities if a.store_id == store_id and a.date == date]


def build_store_models(
    bundle: DataBundle,
    config: PlanningConfig,
    *,
    as_of: dt.date | None = None,
    bag: IssueBag,
) -> tuple[dict[str, _StoreModel], _Factors, dict[int, float]]:
    forecast_config = config.forecast
    history = [
        row
        for row in bundle.demand
        if as_of is None or row.date <= as_of
    ]
    if not history:
        bag.error("forecast-no-history", "预测起点之前没有历史需求记录")
        return {}, _Factors({}, {}), {}

    store_levels, global_median = _store_levels(history)
    hour_keys = range(24)
    weekday_keys = range(7)
    hour_factors: dict[int, float] = {hour: 1.0 for hour in hour_keys}

    # 用小时因子去季节化后再估计星期因子。
    weekday_groups: dict[int, list[float]] = {}
    for row in history:
        level = store_levels.get(row.store_id)
        if row.transactions is None or not level or level <= 0:
            continue
        adjusted = row.transactions / max(hour_factors.get(row.hour, 1.0), 1e-6)
        weekday_groups.setdefault(row.date.weekday(), []).append(adjusted / level)
    weekday_factors = _normalize_factors(
        {
            key: _shrunk_ratio(values, shrink=forecast_config.weekday_shrink_observations)
            for key, values in weekday_groups.items()
        },
        weekday_keys,
    )
    # 小时因子也用星期修正后的残差再拟合一次。
    hour_groups: dict[int, list[float]] = {}
    for row in history:
        level = store_levels.get(row.store_id)
        if row.transactions is None or not level or level <= 0:
            continue
        adjusted = row.transactions / max(
            weekday_factors.get(row.date.weekday(), 1.0), 1e-6
        )
        hour_groups.setdefault(row.hour, []).append(adjusted / level)
    hour_factors = _normalize_factors(
        {
            key: _shrunk_ratio(values, shrink=forecast_config.store_hour_shrink_observations)
            for key, values in hour_groups.items()
        },
        hour_keys,
    )

    conversions, global_conversion = _conversion_rate(history, forecast_config)
    open_hours = _open_hours(history, forecast_config)

    transaction_counts: dict[str, list[float]] = {}
    for row in history:
        if row.transactions is None:
            continue
        transaction_counts.setdefault(row.store_id, []).append(row.transactions)

    models: dict[str, _StoreModel] = {}
    stores = sorted(set(bundle.staff_stores) | set(bundle.demand_stores))
    for store in stores:
        observations = transaction_counts.get(store, [])
        traffic_observations = sum(
            1
            for row in history
            if row.store_id == store and row.traffic_usable
        )
        notes: list[str] = []
        if observations:
            seasonal = []
            for row in history:
                if row.store_id != store or row.transactions is None:
                    continue
                factor = hour_factors.get(row.hour, 1.0) * weekday_factors.get(
                    row.date.weekday(), 1.0
                )
                seasonal.append(row.transactions / max(factor, 1e-6))
            base = statistics.median(seasonal) if seasonal else statistics.median(observations)
            confidence = min(0.90, 0.30 + 0.075 * len(observations))
            basis = "store-history"
            if len(observations) < forecast_config.min_store_observations:
                base = (
                    base * len(observations) + global_median * 3
                ) / (len(observations) + 3)
                confidence = min(confidence, 0.45)
                notes.append(
                    f"门店仅有 {len(observations)} 条历史交易，已向全局均值收缩"
                )
        else:
            base = global_median
            confidence = 0.15
            basis = "global-fallback"
            notes.append("门店没有历史交易，使用全局均值并把置信度标为低")
            bag.warning(
                "forecast-store-no-history",
                f"门店 {store} 没有历史交易记录，预测使用全局均值，需人工确认",
                entity=store,
            )
        if traffic_observations == 0:
            notes.append("没有可用客流计数，客流由交易和转化率推算")
        elif traffic_observations < 3:
            notes.append(f"仅有 {traffic_observations} 条可用客流计数，客流估计不稳")
        if observations:
            residual_values = [
                abs(row.transactions - base * hour_factors.get(row.hour, 1.0) * weekday_factors.get(row.date.weekday(), 1.0))
                / max(base * hour_factors.get(row.hour, 1.0) * weekday_factors.get(row.date.weekday(), 1.0), 1e-6)
                for row in history
                if row.store_id == store and row.transactions is not None
            ]
            residual_cv = statistics.fmean(residual_values) if residual_values else 0.30
            residual_cv = max(residual_cv, forecast_config.min_relative_interval)
        else:
            residual_cv = 0.35
        conversion = conversions.get(store, global_conversion)
        start, end = open_hours.get(
            store, (forecast_config.default_open_hour, forecast_config.default_close_hour)
        )
        if not observations:
            start, end = (
                forecast_config.default_open_hour,
                forecast_config.default_close_hour,
            )
        if start >= end:
            start, end = forecast_config.default_open_hour, forecast_config.default_close_hour
        models[store] = _StoreModel(
            store_id=store,
            base_transactions=base,
            conversion=conversion,
            observations=len(observations),
            traffic_observations=traffic_observations,
            confidence=confidence,
            basis=basis,
            open_hour=start,
            close_hour=end,
            notes=notes,
            residual_cv=residual_cv,
        )
    return models, _Factors(hour_factors, weekday_factors), {
        "conversion": global_conversion,
        "global_median": global_median,
    }


def _activity_multiplier(activity: Activity, hour: int) -> float | None:
    if not activity.covers_hour(hour):
        return None
    if activity.kind == "promotion":
        return max(0.0, 1.0 + (activity.uplift or 0.0))
    return 1.0


def forecast_store_day(
    model: _StoreModel,
    factors: _Factors,
    *,
    date: dt.date,
    activities: Sequence[Activity],
    config: ForecastConfig,
    target_hours: tuple[int, int] | None = None,
) -> DayForecast:
    start, end = target_hours or (model.open_hour, model.close_hour)
    start = max(0, start)
    end = min(24, end)
    cells: list[ForecastCell] = []
    weekday_factor = factors.weekday.get(date.weekday(), 1.0)
    activity_ids: list[str] = []
    notes: list[str] = []
    for hour in range(start, end):
        hour_factor = factors.hour.get(hour, 1.0)
        multiplier = 1.0
        activity_id = None
        closed = False
        for activity in activities:
            if not activity.covers_hour(hour):
                continue
            activity_ids.append(activity.activity_id)
            if activity_id is None:
                activity_id = activity.activity_id
            if activity.is_closure:
                closed = True
            else:
                candidate = _activity_multiplier(activity, hour)
                if candidate is not None:
                    multiplier *= candidate
        expected = model.base_transactions * hour_factor * weekday_factor * multiplier
        expected = max(expected, 0.0)
        relative = min(1.0, model.residual_cv / math.sqrt(max(model.observations, 1)))
        relative = max(relative, config.min_relative_interval)
        if model.observations == 0:
            relative = max(relative, 0.35)
        z = config.interval_z
        lower = max(0.0, expected * (1 - z * relative))
        upper = expected * (1 + z * relative)
        confidence = model.confidence
        if closed:
            confidence = min(1.0, confidence + 0.05)
        elif multiplier != 1.0:
            confidence = max(0.1, confidence - 0.15)
        if closed:
            traffic = transactions = 0.0
            traffic_lower = traffic_upper = 0.0
            lower = upper = 0.0
            traffic_basis = "closed"
        else:
            transactions = expected
            if model.conversion > 0:
                traffic = expected / model.conversion
                traffic_lower = lower / model.conversion
                traffic_upper = upper / model.conversion
            else:  # pragma: no cover - 转换率下限保证不会发生
                traffic = expected
                traffic_lower = lower
                traffic_upper = upper
            traffic_basis = (
                "observed-support"
                if model.traffic_observations >= 3
                else "conversion-estimate"
            )
        cells.append(
            ForecastCell(
                store_id=model.store_id,
                date=date,
                hour=hour,
                expected_traffic=None if closed else traffic,
                expected_transactions=None if closed else transactions,
                traffic_lower=None if closed else traffic_lower,
                traffic_upper=None if closed else traffic_upper,
                confidence=confidence,
                open=not closed,
                activity_id=activity_id,
                traffic_basis=traffic_basis,
                notes=tuple(model.notes) if hour == start else (),
            )
        )
    open_hours = tuple(cell.hour for cell in cells if cell.open)
    closed_hours = tuple(cell.hour for cell in cells if not cell.open)
    if closed_hours:
        notes.append("存在临时闭店时段，闭店小时不计需求")
    day_confidence = (
        statistics.fmean(cell.confidence for cell in cells) if cells else 0.0
    )
    notes.extend(model.notes)
    return DayForecast(
        store_id=model.store_id,
        date=date,
        open_hours=open_hours,
        closed_hours=closed_hours,
        cells=cells,
        activity_ids=tuple(sorted(set(activity_ids))),
        confidence=day_confidence,
        notes=tuple(dict.fromkeys(notes)),
    )


def build_forecast(
    bundle: DataBundle,
    config: PlanningConfig,
    *,
    dates: Sequence[dt.date],
    as_of: dt.date | None = None,
    bag: IssueBag | None = None,
) -> ForecastResult:
    """为指定日期生成每个门店的小时预测。"""

    issues = bag if bag is not None else IssueBag()
    result_issues: list = []
    models, factors, _ = build_store_models(bundle, config, as_of=as_of, bag=issues)
    result_issues.extend(issues.items)
    if not models:
        return ForecastResult(days=[], issues=result_issues)

    if as_of is not None and dates and min(dates) <= as_of:
        issues.warning(
            "forecast-date-not-future",
            f"预测日期 {min(dates).isoformat()} 不晚于预测起点 {as_of.isoformat()}，历史拟合会包含目标日期",
        )

    days: list[DayForecast] = []
    for store, model in sorted(models.items()):
        for date in sorted(set(dates)):
            activities = [a for a in bundle.activities if a.store_id == store and a.date == date]
            days.append(
                forecast_store_day(
                    model,
                    factors,
                    date=date,
                    activities=activities,
                    config=config.forecast,
                )
            )
    assumptions = [
        "客流缺失或覆盖率为 0 视为未知，不当作 0；客流优先用覆盖率放大后的计数。",
        "交易预测 = 门店基准 × 小时因子 × 星期因子 × 活动系数；样本少时向全局均值收缩。",
        "目标日期只使用预测起点之前的历史记录；活动与临时闭店按覆盖的小时调整。",
        "预测区间是基于历史残差的近似区间，不是统计保证。",
    ]
    result_issues.extend(issues.items)
    return ForecastResult(days=days, issues=result_issues, assumptions=assumptions)
