"""发布前检查与承诺台账。

计划发布形成承诺需要三步，各自独立：

1. 区域经理确认预算范围；
2. 门店负责人接受班次安排；
3. 员工收到通知并接受，形成个人承诺。

任何一步缺失都只表示"尚未确认"，不能视为成功。发布检查只读，不修改计划。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .config import PlanningConfig
from .models import ScenarioPlan
from .scheduler import check_plan

if TYPE_CHECKING:  # pragma: no cover
    from .loader import DataBundle
    from .models import RequirementSet


@dataclass
class PublicationAssessment:
    scenario: str
    revision: int
    budget_confirmed: bool
    manager_accepted: bool
    commitment_ready: bool
    blockers: list[str]
    warnings: list[str]
    pending_employee_confirmations: list[dict]
    accepted_commitments: list[dict]
    budget: dict
    unmet_requirements: list[dict]

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario,
            "revision": self.revision,
            "budget_confirmed": self.budget_confirmed,
            "manager_accepted": self.manager_accepted,
            "commitment_ready": self.commitment_ready,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "pending_employee_confirmations": self.pending_employee_confirmations,
            "accepted_commitments": self.accepted_commitments,
            "budget": self.budget,
            "unmet_requirements": self.unmet_requirements,
        }


def assess_publication(
    plan: ScenarioPlan,
    bundle: "DataBundle",
    requirements: "RequirementSet",
    config: PlanningConfig,
    *,
    budget_confirmed: bool = False,
    manager_accepted: bool = False,
) -> dict:
    check = check_plan(
        plan.shifts,
        bundle,
        config,
        requirements=requirements,
        generated=plan.key != "baseline",
        allow_cross_store=bool(plan.metadata.get("allow_cross_store", False)),
        weekly_cap_ratio=float(plan.metadata.get("allow_overtime_ratio", 1.0)),
    )
    blockers: list[str] = []
    warnings: list[str] = []
    if not budget_confirmed:
        blockers.append("区域经理尚未确认预算范围")
    if not manager_accepted:
        blockers.append("门店负责人尚未接受班次安排")
    for issue in check.issues:
        if issue.blocks_publication:
            blockers.append(f"[{issue.code}] {issue.message}")
        elif issue.severity.value == "warning":
            warnings.append(f"[{issue.code}] {issue.message}")

    pending = []
    commitments = []
    for shift in sorted(plan.shifts, key=lambda s: (s.date, s.start_hour)):
        employee = bundle.staff_by_id.get(shift.assigned_employee)
        entry = {
            "shift_id": shift.shift_id,
            "date": shift.date.isoformat(),
            "store_id": shift.store_id,
            "role": shift.role,
            "start_hour": shift.start_hour,
            "end_hour": shift.end_hour,
            "employee_id": shift.assigned_employee,
            "employee_store": employee.store_id if employee else None,
            "acceptance_state": shift.acceptance_state,
        }
        if shift.is_accepted:
            commitments.append(entry)
        elif shift.is_proposed:
            pending.append(entry)

    from .scheduler import _budget_status

    budget = _budget_status(
        check.cost_by_region, bundle, sorted({s.date for s in plan.shifts})
    )
    budget_over = any(entry.get("state") == "over" for entry in budget.values())
    if budget_over:
        blockers.append("至少一个区域的预计成本超出预算")
    elif any(entry.get("state") == "no-budget" for entry in budget.values()):
        warnings.append("部分区域没有预算数据，需区域经理确认范围")

    gaps = []
    from .scheduler import compute_coverage

    coverage = compute_coverage(
        plan.shifts, requirements, included_states=("accepted", "proposed", "unknown")
    )
    for gap in coverage.gaps:
        if gap["role"] in config.constraints.critical_roles and gap["coverage_ratio"] < 0.5:
            gaps.append(gap)
    if gaps:
        warnings.append(
            f"有 {len(gaps)} 个关键岗位/日缺口超过一半，发布前应说明或补充"
        )

    assessment = PublicationAssessment(
        scenario=plan.key,
        revision=plan.revision,
        budget_confirmed=budget_confirmed,
        manager_accepted=manager_accepted,
        commitment_ready=not blockers,
        blockers=blockers,
        warnings=warnings,
        pending_employee_confirmations=pending,
        accepted_commitments=commitments,
        budget=budget,
        unmet_requirements=gaps,
    )
    return assessment.to_dict()
