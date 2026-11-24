"""业务服务层：编排读取、预测、需求、方案生成、修订与回看。"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .config import PlanningConfig
from .errors import IssueBag
from .forecast import build_forecast
from .loader import DataBundle, load_bundle, require_core_data
from .models import Comparison, ForecastResult, RequirementSet, ScenarioPlan
from .requirements import build_requirements
from .scheduler import (
    DEFAULT_PROFILES,
    ScenarioMetrics,
    ScenarioProfile,
    baseline_scenario,
    compare_scenarios,
    evaluate_scenario,
    generate_scenario,
)


@dataclass
class PlanningRun:
    bundle: DataBundle
    config: PlanningConfig
    dates: list[dt.date]
    as_of: dt.date | None
    forecast: ForecastResult
    requirements: RequirementSet
    plans: dict[str, ScenarioPlan] = field(default_factory=dict)
    metrics: dict[str, ScenarioMetrics] = field(default_factory=dict)
    comparison: Comparison | None = None
    issues: IssueBag = field(default_factory=IssueBag)

    def plan(self, key: str) -> ScenarioPlan:
        if key not in self.plans:
            raise KeyError(f"未知方案 {key}；可用: {', '.join(sorted(self.plans))}")
        return self.plans[key]

    def metrics_for(self, key: str) -> ScenarioMetrics:
        plan = self.plan(key)
        if key not in self.metrics:
            self.metrics[key] = evaluate_scenario(
                plan, self.bundle, self.requirements, self.config, dates=self.dates
            )
        return self.metrics[key]

    def refresh(self) -> None:
        self.comparison, self.metrics = compare_scenarios(
            self.plans,
            self.bundle,
            self.requirements,
            self.config,
            dates=self.dates,
        )


class PlanningService:
    """应用服务入口，供 CLI 和测试使用。"""

    def __init__(self, config: PlanningConfig | None = None) -> None:
        self.config = config or PlanningConfig()
        self.config.validate()

    # -- 读取与运行 -------------------------------------------------------

    def load(
        self,
        data_dir: str | Path,
        *,
        activities_path: str | Path | None = None,
        budgets_path: str | Path | None = None,
    ) -> DataBundle:
        return load_bundle(
            data_dir,
            activities_path=activities_path,
            budgets_path=budgets_path,
        )

    def build_run(
        self,
        bundle: DataBundle,
        *,
        start: dt.date,
        days: int = 14,
        as_of: dt.date | None = None,
        scenario_keys: Sequence[str] | None = None,
    ) -> PlanningRun:
        require_core_data(bundle)
        if days < 1:
            raise ValueError("规划天数必须至少为 1")
        dates = [start + dt.timedelta(days=offset) for offset in range(days)]
        issues = IssueBag()
        issues.extend(bundle.issues)
        if as_of is None:
            as_of = start - dt.timedelta(days=1)
        forecast = build_forecast(bundle, self.config, dates=dates, as_of=as_of, bag=issues)
        requirement_bag = IssueBag()
        requirements = build_requirements(forecast, self.config, bag=requirement_bag)
        issues.extend(requirement_bag)
        run = PlanningRun(
            bundle=bundle,
            config=self.config,
            dates=dates,
            as_of=as_of,
            forecast=forecast,
            requirements=requirements,
            issues=issues,
        )
        profiles = self._profiles(scenario_keys)
        run.plans["baseline"] = baseline_scenario(
            bundle, requirements, self.config, dates=dates
        )
        for profile in profiles:
            run.plans[profile.key] = generate_scenario(
                bundle,
                requirements,
                self.config,
                profile=profile,
                dates=dates,
            )
        run.refresh()
        return run

    def _profiles(self, keys: Sequence[str] | None) -> list[ScenarioProfile]:
        catalogue = {profile.key: profile for profile in DEFAULT_PROFILES}
        if not keys:
            return list(DEFAULT_PROFILES)
        profiles: list[ScenarioProfile] = []
        for key in keys:
            if key == "baseline":
                continue
            if key not in catalogue:
                raise KeyError(
                    f"未知方案 {key}；可用: {', '.join(sorted(catalogue))}"
                )
            profiles.append(catalogue[key])
        return profiles or list(DEFAULT_PROFILES)

    # -- 修订 -------------------------------------------------------------

    def apply_change(self, run: PlanningRun, key: str, change: Mapping) -> ScenarioPlan:
        """对方案应用一次显式修订，返回新版本的方案（不修改原对象）。"""

        if isinstance(change, Mapping) and change.get("kind") == "activity":
            return self._apply_activity_change(run, key, change)
        from .revision import apply_change as _apply

        plan = run.plan(key)
        before = {issue for issue in plan.issues}
        updated = _apply(plan, run.bundle, self.config, change)
        run.plans[key] = updated
        run.refresh()
        for issue in updated.issues:
            if issue not in before:
                run.issues.items.append(issue)
        return updated

    def _apply_activity_change(
        self, run: PlanningRun, key: str, change: Mapping
    ) -> ScenarioPlan:
        """迟到活动变更：只重算未确认时段，保留所有已接受班次。"""

        import datetime as dt
        from copy import deepcopy

        from .forecast import build_forecast
        from .models import Activity, normalize_activity_kind
        from .requirements import build_requirements
        from .revision import RevisionError, apply_activity_change

        raw = change.get("activity")
        if not isinstance(raw, Mapping):
            raise RevisionError("activity 变更需要 activity 对象")
        try:
            activity = Activity(
                activity_id=str(raw["activity_id"]),
                store_id=str(raw["store_id"]),
                date=dt.date.fromisoformat(str(raw["date"])),
                kind=normalize_activity_kind(raw.get("kind")),
                start_hour=int(raw["start_hour"]),
                end_hour=int(raw["end_hour"]),
                uplift=float(raw["uplift"]) if raw.get("uplift") is not None else None,
                note=str(raw.get("note", "")),
            )
        except (KeyError, ValueError) as exc:
            raise RevisionError(f"activity 字段不完整或不合法: {exc}") from exc
        if activity.end_hour <= activity.start_hour:
            raise RevisionError("活动结束小时必须晚于开始小时")

        shadow = deepcopy(run.bundle)
        shadow.activities = [
            a for a in shadow.activities if a.activity_id != activity.activity_id
        ] + [activity]
        forecast = build_forecast(
            shadow, self.config, dates=run.dates, as_of=run.as_of
        )
        requirements = build_requirements(forecast, self.config)

        plan = run.plan(key)
        updated = apply_activity_change(
            plan, shadow, self.config, activity=activity, dates=run.dates
        )
        updated.metadata = {
            **updated.metadata,
            "last_activity_change": activity.activity_id,
        }
        run.bundle = shadow
        run.forecast = forecast
        run.requirements = requirements
        run.plans[key] = updated

        # 其他方案仍需与同一份新需求可比：保留各自已接受班次，重算其余时段。
        for other_key in list(run.plans):
            if other_key in {key, "baseline"}:
                continue
            other = run.plans[other_key]
            profile = next(
                (p for p in DEFAULT_PROFILES if p.key == other_key), None
            )
            if profile is None:
                continue
            fixed = [s for s in other.shifts if s.is_accepted]
            run.plans[other_key] = generate_scenario(
                shadow,
                requirements,
                self.config,
                profile=profile,
                dates=run.dates,
                fixed_shifts=fixed,
                revision=other.revision + 1,
                log_superseded=False,
            )
        run.refresh()
        return updated

    # -- 发布检查 ---------------------------------------------------------

    def publication_readiness(
        self,
        run: PlanningRun,
        key: str,
        *,
        budget_confirmed: bool = False,
        manager_accepted: bool = False,
    ) -> dict:
        from .publication import assess_publication

        return assess_publication(
            run.plan(key),
            run.bundle,
            run.requirements,
            self.config,
            budget_confirmed=budget_confirmed,
            manager_accepted=manager_accepted,
        )
