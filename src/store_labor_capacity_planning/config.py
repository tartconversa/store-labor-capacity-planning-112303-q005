"""规划参数、服务标准与成本模型。

所有默认值都是合成演示值，可在 JSON 配置中覆盖。配置按区段读取，未知字段会被拒绝，
避免拼写错误悄悄改变排班结果。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping

from .constants import ROLES


class ConfigError(ValueError):
    """配置文件不合法。"""


@dataclass(frozen=True)
class ForecastConfig:
    """需求预测参数。"""

    lookback_days: int = 56
    min_store_observations: int = 3
    # 门店小时样本极少时，向"门店均值 × 全局小时形状"收缩。
    store_hour_shrink_observations: float = 4.0
    weekday_shrink_observations: float = 6.0
    activity_shrink_observations: float = 3.0
    max_growth_adjustment: float = 0.25
    min_hour_factor: float = 0.15
    default_conversion_rate: float = 0.60
    min_conversion_rate: float = 0.10
    max_conversion_rate: float = 0.95
    default_open_hour: int = 8
    default_close_hour: int = 20
    interval_z: float = 1.28
    min_relative_interval: float = 0.12


@dataclass(frozen=True)
class ServiceStandards:
    """把客流与交易转换为各岗位工时的服务标准。"""

    transactions_per_register_hour: float = 45.0
    customers_per_service_hour: float = 120.0
    cleaning_hours_per_open_hour: float = 0.08
    min_sales_hours_per_open_hour: float = 0.50
    min_service_hours_per_open_hour: float = 0.25
    receiving_hours_per_open_day: float = 1.00
    receiving_window_hours: int = 2
    food_safety_hours_per_open_day: float = 0.50
    food_window_hours: int = 2
    closure_task_hours: float = 1.00
    service_wait_target_minutes: float = 8.0
    # 高峰时段（客流最高的若干个开放小时）服务岗位至少保留一人。
    peak_hours: int = 4
    peak_min_service_hours: float = 0.75

    def validate(self) -> None:
        positive = {
            "transactions_per_register_hour": self.transactions_per_register_hour,
            "customers_per_service_hour": self.customers_per_service_hour,
            "receiving_window_hours": self.receiving_window_hours,
            "food_window_hours": self.food_window_hours,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ConfigError(f"服务标准 {name} 必须大于 0")
        for name in (
            "cleaning_hours_per_open_hour",
            "min_sales_hours_per_open_hour",
            "min_service_hours_per_open_hour",
            "receiving_hours_per_open_day",
            "food_safety_hours_per_open_day",
            "closure_task_hours",
            "peak_min_service_hours",
        ):
            if getattr(self, name) < 0:
                raise ConfigError(f"服务标准 {name} 不能为负")
        if self.service_wait_target_minutes <= 0:
            raise ConfigError("service_wait_target_minutes 必须大于 0")


@dataclass(frozen=True)
class ConstraintConfig:
    """排班约束。"""

    rest_hours_between_shifts: float = 11.0
    max_consecutive_days: int = 5
    max_shift_hours: float = 10.0
    min_shift_hours: float = 2.0
    max_shifts_per_role_day: int = 4
    break_minutes: int = 30
    break_after_hours: float = 6.0
    max_weekly_hours: float | None = None  # 为空时使用员工合同工时
    weekly_overtime_tolerance: float = 0.0
    allow_cross_store: bool = False
    # 全局能力开关：允许方案把"没有可用性记录"的时段作为待确认安排。
    # 具体方案是否使用由 ScenarioProfile 决定；未知仍会标记并降低评分，不会当作已确认。
    allow_unknown_availability: bool = True
    coverage_tolerance_hours: float = 0.25
    critical_roles: tuple[str, ...] = ("sales", "service")

    def validate(self) -> None:
        if self.rest_hours_between_shifts < 0:
            raise ConfigError("rest_hours_between_shifts 不能为负")
        if self.max_consecutive_days < 1:
            raise ConfigError("max_consecutive_days 必须至少为 1")
        if self.min_shift_hours <= 0 or self.max_shift_hours < self.min_shift_hours:
            raise ConfigError("班次时长范围不合法")
        if self.break_minutes < 0:
            raise ConfigError("break_minutes 不能为负")
        if not set(self.critical_roles) <= set(ROLES):
            raise ConfigError(f"critical_roles 只能取 {ROLES}")


@dataclass(frozen=True)
class CostConfig:
    """成本估算。金额为合成演示币值。"""

    region_rates: Mapping[str, float] = field(
        default_factory=lambda: {"R-S": 32.0, "R-W": 30.0, "R-N": 31.0}
    )
    role_multipliers: Mapping[str, float] = field(
        default_factory=lambda: {
            "sales": 1.00,
            "service": 1.05,
            "receiving": 1.00,
            "food": 1.08,
        }
    )
    default_rate: float = 31.0
    overtime_multiplier: float = 1.50
    evening_start_hour: int = 18
    evening_premium: float = 0.10
    weekend_premium: float = 0.05
    currency: str = "元"

    def validate(self) -> None:
        if self.default_rate < 0 or any(v < 0 for v in self.region_rates.values()):
            raise ConfigError("时薪不能为负")
        if any(v <= 0 for v in self.role_multipliers.values()):
            raise ConfigError("岗位系数必须大于 0")
        if self.overtime_multiplier < 1:
            raise ConfigError("加班系数不能小于 1")


@dataclass(frozen=True)
class SchedulerConfig:
    """方案生成与比较的权重。"""

    coverage_weight: float = 1.0
    cost_weight: float = 0.18
    preference_bonus: float = 0.20
    confirmed_availability_bonus: float = 0.35
    tentative_penalty: float = 0.25
    fairness_weight: float = 0.10
    min_coverage_target: float = 0.90
    budget_weight: float = 1.0

    def validate(self) -> None:
        for name in (
            "coverage_weight",
            "cost_weight",
            "preference_bonus",
            "confirmed_availability_bonus",
            "tentative_penalty",
            "fairness_weight",
            "budget_weight",
        ):
            if getattr(self, name) < 0:
                raise ConfigError(f"排班权重 {name} 不能为负")
        if not 0 <= self.min_coverage_target <= 1:
            raise ConfigError("min_coverage_target 必须在 0 到 1 之间")


@dataclass(frozen=True)
class ReviewConfig:
    overtime_tolerance_minutes: int = 15
    undercoverage_ratio: float = 0.15
    high_wait_share: float = 0.25
    task_load_high_per_hour: float = 2.5


@dataclass(frozen=True)
class PlanningConfig:
    forecast: ForecastConfig = field(default_factory=ForecastConfig)
    standards: ServiceStandards = field(default_factory=ServiceStandards)
    constraints: ConstraintConfig = field(default_factory=ConstraintConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)

    def validate(self) -> None:
        for section in (
            self.standards,
            self.constraints,
            self.cost,
            self.scheduler,
        ):
            section.validate()

    def with_updates(self, **sections: Any) -> "PlanningConfig":
        """返回替换了若干区段的新配置。"""

        unknown = set(sections) - {f.name for f in fields(self)}
        if unknown:
            raise ConfigError(f"未知配置区段: {', '.join(sorted(unknown))}")
        return replace(self, **sections)

    def to_dict(self) -> dict:
        return asdict(self)


def _filter_fields(cls, payload: Mapping[str, Any]) -> dict:
    allowed = {f.name for f in fields(cls)}
    unknown = set(payload) - allowed
    if unknown:
        raise ConfigError(f"{cls.__name__} 中存在未知字段: {', '.join(sorted(unknown))}")
    return dict(payload)


def config_from_dict(payload: Mapping[str, Any]) -> PlanningConfig:
    if not isinstance(payload, Mapping):
        raise ConfigError("配置根节点必须是对象")
    known = {f.name: f.type for f in fields(PlanningConfig)}
    unknown = set(payload) - set(known)
    if unknown:
        raise ConfigError(f"未知配置区段: {', '.join(sorted(unknown))}")
    sections: dict[str, Any] = {}
    types = {
        "forecast": ForecastConfig,
        "standards": ServiceStandards,
        "constraints": ConstraintConfig,
        "cost": CostConfig,
        "scheduler": SchedulerConfig,
        "review": ReviewConfig,
    }
    for name, cls in types.items():
        if name in payload:
            raw = payload[name]
            if not isinstance(raw, Mapping):
                raise ConfigError(f"配置区段 {name} 必须是对象")
            data = _filter_fields(cls, raw)
            if name == "constraints" and "critical_roles" in data:
                data["critical_roles"] = tuple(data["critical_roles"])
            sections[name] = cls(**data)
    config = PlanningConfig(**sections)
    config.validate()
    return config


def load_config(path: str | Path | None) -> PlanningConfig:
    if path is None:
        config = PlanningConfig()
        config.validate()
        return config
    file_path = Path(path)
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"配置文件不存在: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是有效 JSON: {exc}") from exc
    return config_from_dict(payload)
