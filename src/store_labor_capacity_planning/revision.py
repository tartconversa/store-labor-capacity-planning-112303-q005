"""计划修订与状态流转。

规则：

* 已接受班次是承诺，修订不会删除；影响已接受班次的活动变更会生成"待协商替换"提示，
  需要门店负责人和员工重新确认。
* 迟到的活动变更只重算尚未确认的时段：未确认班次可以回收/重建，已确认班次原样保留。
* 手工调整（新增/移动/删除/改派）都会写入变更日志；删除已接受班次会被拒绝。
* 员工接受/拒绝、门店负责人接受、区域经理确认预算分别有独立动作。
"""

from __future__ import annotations

import datetime as dt
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Mapping

from .config import PlanningConfig
from .errors import IssueBag
from .models import Activity, ScenarioPlan, Shift
from .scheduler import (
    DEFAULT_PROFILES,
    ScenarioProfile,
    _consecutive_ok,
    check_plan,
    generate_scenario,
)

if TYPE_CHECKING:  # pragma: no cover
    from .loader import DataBundle


class RevisionError(ValueError):
    """修订请求不合法或会造成承诺被静默删除。"""


def _profile_for(plan: ScenarioPlan) -> ScenarioProfile:
    for profile in DEFAULT_PROFILES:
        if profile.key == plan.key:
            return profile
    return ScenarioProfile(
        key=plan.key,
        name=plan.name,
        description=plan.description,
        coverage_target=float(plan.metadata.get("coverage_target", 0.9)),
        cost_weight=0.18,
        preference_weight=0.15,
        allow_overtime_ratio=float(plan.metadata.get("allow_overtime_ratio", 1.0)),
        allow_cross_store=bool(plan.metadata.get("allow_cross_store", False)),
        allow_unknown_availability=True,
    )


def _overlaps(shift: Shift, *, store_id: str, date: dt.date, start: int, end: int, role: str | None) -> bool:
    if shift.store_id != store_id or shift.date != date:
        return False
    if role is not None and shift.role != role:
        return False
    return shift.start_hour < end and start < shift.end_hour


def _log(plan: ScenarioPlan, action: str, **payload) -> None:
    entry = {"action": action, "revision": plan.revision + 1}
    entry.update(payload)
    plan.change_log.append(entry)


def _next_shift_id(plan: ScenarioPlan, prefix: str = "CHG") -> str:
    number = 0
    for shift in plan.shifts:
        if shift.shift_id.startswith(prefix + "-"):
            try:
                number = max(number, int(shift.shift_id.rsplit("-", 1)[1]))
            except (IndexError, ValueError):
                continue
    return f"{prefix}-{number + 1:04d}"


def _bump(plan: ScenarioPlan) -> ScenarioPlan:
    plan.revision += 1
    return plan


# ---------------------------------------------------------------------------
# 单班次动作
# ---------------------------------------------------------------------------


def assign_shift(plan: ScenarioPlan, bundle: "DataBundle", config: PlanningConfig, payload: Mapping) -> ScenarioPlan:
    """新增一个待确认班次，或改派一个未确认班次。"""

    shift_id = payload.get("shift_id")
    if shift_id:
        existing = next((s for s in plan.shifts if s.shift_id == shift_id), None)
        if existing is None:
            raise RevisionError(f"方案中没有班次 {shift_id}")
        if existing.is_accepted:
            raise RevisionError(
                f"班次 {shift_id} 已被员工接受，不能直接改派；请先发起协商替换"
            )
        updates = {k: v for k, v in payload.items() if k not in {"shift_id", "kind"}}
        unknown = set(updates) - {
            field.name for field in Shift.__dataclass_fields__.values()
        }
        if unknown:
            raise RevisionError(f"未知班次字段: {', '.join(sorted(unknown))}")
        if "date" in updates and not isinstance(updates["date"], dt.date):
            updates["date"] = dt.date.fromisoformat(str(updates["date"]))
        for hour_field in ("start_hour", "end_hour"):
            if hour_field in updates:
                updates[hour_field] = int(updates[hour_field])
        plan.shifts = [s for s in plan.shifts if s.shift_id != shift_id]
        shift = replace(existing, **updates)
        action = "reassigned"
    else:
        required = ("store_id", "date", "role", "start_hour", "end_hour", "employee_id")
        missing = [field for field in required if field not in payload]
        if missing:
            raise RevisionError(f"新增班次缺少字段: {', '.join(missing)}")
        write_state = payload.get("acceptance_state", "proposed")
        if write_state == "accepted":
            raise RevisionError("新增班次不能直接标记为已接受；需经过员工确认流程")
        try:
            shift_date = payload["date"]
            if not isinstance(shift_date, dt.date):
                shift_date = dt.date.fromisoformat(str(shift_date))
            start_hour = int(payload["start_hour"])
            end_hour = int(payload["end_hour"])
        except (TypeError, ValueError) as exc:
            raise RevisionError(f"日期或小时字段不合法: {exc}") from exc
        shift = Shift(
            shift_id=_next_shift_id(plan),
            store_id=str(payload["store_id"]),
            date=shift_date,
            role=str(payload["role"]),
            start_hour=start_hour,
            end_hour=end_hour,
            assigned_employee=str(payload["employee_id"]),
            acceptance_state=write_state,
            origin="generated",
            scenario=plan.key,
            notes=tuple(payload.get("notes", ())),
        )
        action = "added"
    if shift.hours <= 0:
        raise RevisionError("班次结束必须晚于开始")
    if shift.assigned_employee not in bundle.staff_by_id:
        raise RevisionError(f"员工 {shift.assigned_employee} 不存在")
    plan.shifts = sorted(plan.shifts + [shift], key=lambda s: (s.date, s.start_hour, s.shift_id))
    _log(plan, action, shift_id=shift.shift_id, employee_id=shift.assigned_employee)
    _refresh_issues(plan, bundle, config)
    return _bump(plan)


def remove_shift(plan: ScenarioPlan, bundle: "DataBundle", config: PlanningConfig, shift_id: str) -> ScenarioPlan:
    shift = next((s for s in plan.shifts if s.shift_id == shift_id), None)
    if shift is None:
        raise RevisionError(f"方案中没有班次 {shift_id}")
    if shift.is_accepted:
        raise RevisionError(
            f"班次 {shift_id} 已被员工接受，不能静默删除；请新增替换班次并标记待协商"
        )
    plan.shifts = [s for s in plan.shifts if s.shift_id != shift_id]
    _log(plan, "removed", shift_id=shift_id, employee_id=shift.assigned_employee)
    _refresh_issues(plan, bundle, config)
    return _bump(plan)


def record_acceptance(
    plan: ScenarioPlan,
    bundle: "DataBundle",
    config: PlanningConfig,
    *,
    shift_id: str,
    state: str,
    actor: str = "employee",
) -> ScenarioPlan:
    shift = next((s for s in plan.shifts if s.shift_id == shift_id), None)
    if shift is None:
        raise RevisionError(f"方案中没有班次 {shift_id}")
    if state not in {"accepted", "declined", "proposed"}:
        raise RevisionError("接受状态只能是 proposed/accepted/declined")
    if state == "accepted":
        if shift.acceptance_state == "declined":
            raise RevisionError("已拒绝的班次需要重新提议后才能接受")
        blockers = _acceptance_blockers(plan, bundle, config, shift)
        if blockers:
            raise RevisionError(
                "班次存在未解决的硬约束冲突，不能确认：" + "；".join(blockers)
            )
    plan.shifts = [replace(s, acceptance_state=state) if s.shift_id == shift_id else s for s in plan.shifts]
    _log(plan, "acceptance", shift_id=shift_id, state=state, actor=actor)
    _refresh_issues(plan, bundle, config)
    return _bump(plan)


def _acceptance_blockers(
    plan: ScenarioPlan,
    bundle: "DataBundle",
    config: PlanningConfig,
    shift: Shift,
) -> list[str]:
    """接受即形成承诺，因此必须逐项核实，不受"导入/生成"来源影响。"""

    from .scheduler import (
        _rest_ok,
        _Ledger,
        paid_hours,
        week_key,
    )

    issues: list[str] = []
    employee = bundle.staff_by_id.get(shift.assigned_employee)
    if employee is None:
        return [f"员工 {shift.assigned_employee} 不存在"]
    constraints = config.constraints
    if employee.store_id != shift.store_id:
        same_region = employee.region and employee.region == next(
            (e.region for e in bundle.staff if e.store_id == shift.store_id), None
        )
        allowed = bool(plan.metadata.get("allow_cross_store")) and same_region
        if not allowed:
            issues.append(
                f"{employee.employee_id} 属于 {employee.store_id}，不能直接确认到 {shift.store_id}"
            )
    if employee.role != shift.role:
        issues.append(
            f"{employee.employee_id} 的岗位是 {employee.role}，与班次岗位 {shift.role} 不符"
        )
    if shift.hours < constraints.min_shift_hours or shift.hours > constraints.max_shift_hours:
        issues.append(
            f"班次时长 {shift.hours:g} 小时不在 {constraints.min_shift_hours:g}-{constraints.max_shift_hours:g} 范围内"
        )
    entries = bundle.availability_for(employee.employee_id, shift.date)
    if entries:
        unavailable = [e for e in entries if e.state == "unavailable"]
        if any(e.overlaps(shift.start_hour, shift.end_hour) for e in unavailable):
            issues.append(
                f"{employee.employee_id} 在 {shift.date.isoformat()} 该时段标记为不可用"
            )
        elif not any(
            e.is_available and e.covers(shift.start_hour, shift.end_hour)
            for e in entries
        ):
            issues.append(
                f"班次超出 {employee.employee_id} 在 {shift.date.isoformat()} 的可用窗口"
            )
    ledger = _Ledger()
    others = [
        s
        for s in plan.shifts
        if s.shift_id != shift.shift_id and not s.is_declined
    ]
    for other in others:
        ledger.add(other, constraints)
    if not _rest_ok(ledger, employee.employee_id, shift, constraints):
        issues.append("与其他班次重叠或休息时间不足")
    if not _consecutive_ok(
        ledger, employee.employee_id, shift.date, constraints.max_consecutive_days
    ):
        issues.append(f"连续工作天数会超过 {constraints.max_consecutive_days} 天")
    week = week_key(shift.date)
    used = ledger.weekly_hours.get((employee.employee_id, week), 0.0)
    cap = employee.contract_hours * float(plan.metadata.get("allow_overtime_ratio", 1.0))
    if used + paid_hours(shift, constraints) > cap + 1e-9:
        issues.append(
            f"当周工时会超过 {cap:.1f} 小时上限"
        )
    return issues


# ---------------------------------------------------------------------------
# 活动变更
# ---------------------------------------------------------------------------


def apply_activity_change(
    plan: ScenarioPlan,
    bundle: "DataBundle",
    config: PlanningConfig,
    *,
    activity: Activity,
    dates: list[dt.date],
) -> ScenarioPlan:
    """应用一个迟到的活动/闭店变更，只影响未确认时段。

    ``bundle`` 应当已经包含该活动（服务层会构造影子数据）。影响范围内的未确认班次
    会被回收后按新需求重排；已接受班次原样保留，并记录待协商条目。
    """

    affected = [
        s
        for s in plan.shifts
        if s.store_id == activity.store_id
        and s.date == activity.date
        and _overlaps(
            s,
            store_id=activity.store_id,
            date=activity.date,
            start=activity.start_hour,
            end=activity.end_hour,
            role=None,
        )
    ]
    accepted = [s for s in affected if s.is_accepted]
    unconfirmed = [s for s in affected if not s.is_accepted]
    # 新活动覆盖的小时内，未确认班次全部视为需要重算；闭店时段尤其不能保留。
    within_scope = [
        s
        for s in plan.shifts
        if s.store_id == activity.store_id
        and s.date == activity.date
        and not s.is_accepted
        and s.start_hour < activity.end_hour
        and activity.start_hour < s.end_hour
    ]
    drop_ids = {s.shift_id for s in within_scope}

    provisional = deepcopy(plan)
    provisional.shifts = [s for s in provisional.shifts if s.shift_id not in drop_ids]
    for shift in within_scope:
        provisional.change_log.append(
            {
                "shift_id": shift.shift_id,
                "action": "released",
                "reason": f"活动 {activity.activity_id} 改变了 {shift.date.isoformat()} 的时段需求，未确认班次回收重排",
            }
        )

    updated_requirements = _requirements_for(bundle, config, dates)
    profile = _profile_for(plan)
    fixed = [
        s
        for s in provisional.shifts
        if s.date in set(dates) and not s.is_declined
    ]
    regenerated = generate_scenario(
        bundle,
        updated_requirements,
        config,
        profile=profile,
        dates=dates,
        fixed_shifts=fixed,
        revision=plan.revision + 1,
        log_superseded=False,
    )
    previous_ids = {s.shift_id for s in plan.shifts}
    delta_added = [
        {
            "action": "added",
            "shift_id": s.shift_id,
            "store_id": s.store_id,
            "date": s.date.isoformat(),
            "role": s.role,
            "employee_id": s.assigned_employee,
            "start_hour": s.start_hour,
            "end_hour": s.end_hour,
        }
        for s in regenerated.shifts
        if s.origin == "generated" and s.shift_id not in previous_ids
    ]
    regenerated.change_log = (
        provisional.change_log
        + [
            {
                "action": "activity-replanned",
                "activity_id": activity.activity_id,
                "store_id": activity.store_id,
                "date": activity.date.isoformat(),
                "released_count": len(within_scope),
                "added_count": len(delta_added),
                "kept_confirmed": len(accepted),
            }
        ]
        + delta_added
    )
    for shift in accepted:
        regenerated.change_log.append(
            {
                "shift_id": shift.shift_id,
                "action": "renegotiate",
                "reason": (
                    f"活动 {activity.activity_id}（{activity.kind} {activity.start_hour}-{activity.end_hour}）"
                    "与已接受班次重叠，需门店负责人与员工协商替换；原班次保留"
                ),
            }
        )
    regenerated.description = plan.description
    regenerated.name = plan.name
    regenerated.metadata = {
        **plan.metadata,
        "last_activity_change": activity.activity_id,
        "released_count": len(within_scope),
        "renegotiation_count": len(accepted),
    }
    regenerated.issues = _refresh_issues(regenerated, bundle, config)
    return regenerated


def _requirements_for(bundle: "DataBundle", config: PlanningConfig, dates):
    from .forecast import build_forecast
    from .requirements import build_requirements

    forecast = build_forecast(bundle, config, dates=dates, as_of=min(dates) - dt.timedelta(days=1))
    return build_requirements(forecast, config)


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------


CHANGE_KINDS = {
    "assign": "新增/改派班次",
    "remove": "删除未确认班次",
    "acceptance": "记录接受状态",
    "activity": "应用活动/闭店变更",
}


def apply_change(
    plan: ScenarioPlan,
    bundle: "DataBundle",
    config: PlanningConfig,
    change: Mapping,
) -> ScenarioPlan:
    """按 ``kind`` 分派修订。输入来自 JSON 变更文件或 API 调用。"""

    if not isinstance(change, Mapping):
        raise RevisionError("变更必须是对象")
    kind = change.get("kind")
    if kind == "assign":
        return assign_shift(plan, bundle, config, change)
    if kind == "remove":
        shift_id = change.get("shift_id")
        if not shift_id:
            raise RevisionError("remove 需要 shift_id")
        return remove_shift(plan, bundle, config, shift_id)
    if kind == "acceptance":
        shift_id = change.get("shift_id")
        state = change.get("state")
        if not shift_id or not state:
            raise RevisionError("acceptance 需要 shift_id 和 state")
        return record_acceptance(
            plan,
            bundle,
            config,
            shift_id=shift_id,
            state=state,
            actor=str(change.get("actor", "employee")),
        )
    if kind == "activity":
        raw = change.get("activity")
        if not isinstance(raw, Mapping):
            raise RevisionError("activity 变更需要 activity 对象")
        from .models import normalize_activity_kind

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
        dates = change.get("dates")
        if dates:
            date_list = [dt.date.fromisoformat(str(d)) for d in dates]
        else:
            date_list = sorted({s.date for s in plan.shifts} | {activity.date})
        return apply_activity_change(
            plan, bundle, config, activity=activity, dates=date_list
        )
    raise RevisionError(f"未知变更类型 {kind!r}；可用: {', '.join(CHANGE_KINDS)}")


def _refresh_issues(plan: ScenarioPlan, bundle: "DataBundle", config: PlanningConfig) -> list:
    check = check_plan(
        plan.shifts,
        bundle,
        config,
        generated=plan.key != "baseline",
        allow_cross_store=bool(plan.metadata.get("allow_cross_store", False)),
        weekly_cap_ratio=float(plan.metadata.get("allow_overtime_ratio", 1.0)),
    )
    plan.issues = list(check.issues.items)
    return plan.issues
