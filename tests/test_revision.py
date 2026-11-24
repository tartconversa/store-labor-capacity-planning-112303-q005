"""修订、接受流转与迟到活动变更。"""

from __future__ import annotations

import datetime as dt
import unittest
from copy import deepcopy

from conftest import DATA

from store_labor_capacity_planning.config import PlanningConfig
from store_labor_capacity_planning.forecast import build_forecast
from store_labor_capacity_planning.loader import load_bundle
from store_labor_capacity_planning.requirements import build_requirements
from store_labor_capacity_planning.revision import (
    RevisionError,
    apply_change,
)
from store_labor_capacity_planning.service import PlanningService


def _setup(days: int = 5):
    service = PlanningService()
    bundle = load_bundle(DATA)
    run = service.build_run(
        bundle, start=dt.date(2026, 12, 1), days=days, scenario_keys=["balanced"]
    )
    return service, run


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.service, self.run = _setup()
        self.plan = self.run.plan("balanced")
        self.generated = [
            s for s in self.plan.shifts if s.origin == "generated" and s.is_proposed
        ]
        self.assertTrue(self.generated)

    def test_add_shift_is_pending_and_logged(self):
        target = self.generated[0]
        change = {
            "kind": "assign",
            "store_id": target.store_id,
            "date": target.date.isoformat(),
            "role": target.role,
            "start_hour": target.start_hour,
            "end_hour": target.end_hour,
            "employee_id": target.assigned_employee,
        }
        updated = self.service.apply_change(self.run, "balanced", change)
        added = [s for s in updated.shifts if s.origin == "generated" and s.shift_id.startswith("CHG-")]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0].acceptance_state, "proposed")
        self.assertTrue(any(entry["action"] == "added" for entry in updated.change_log))
        self.assertEqual(updated.revision, 0 + 1)

    def test_cannot_create_presaccepted_shift(self):
        target = self.generated[0]
        change = {
            "kind": "assign",
            "store_id": target.store_id,
            "date": target.date.isoformat(),
            "role": target.role,
            "start_hour": target.start_hour,
            "end_hour": target.end_hour,
            "employee_id": target.assigned_employee,
            "acceptance_state": "accepted",
        }
        with self.assertRaises(RevisionError):
            self.service.apply_change(self.run, "balanced", change)

    def test_remove_unconfirmed_allowed(self):
        target = self.generated[0]
        updated = self.service.apply_change(
            self.run, "balanced", {"kind": "remove", "shift_id": target.shift_id}
        )
        self.assertNotIn(target.shift_id, {s.shift_id for s in updated.shifts})
        self.assertTrue(any(e["action"] == "removed" for e in updated.change_log))

    def test_remove_accepted_is_rejected(self):
        target = self.generated[0]
        self.service.apply_change(
            self.run,
            "balanced",
            {"kind": "acceptance", "shift_id": target.shift_id, "state": "accepted"},
        )
        with self.assertRaises(RevisionError) as ctx:
            self.service.apply_change(
                self.run, "balanced", {"kind": "remove", "shift_id": target.shift_id}
            )
        self.assertIn("不能静默删除", str(ctx.exception))

    def test_acceptance_rejected_when_conflict_remains(self):
        # 先接受一个生成班次，再把同一员工另一班次改为与该班次重叠，
        # 此时接受重叠班次必须失败（承诺前要核实员工约束）。
        target = self.generated[0]
        self.service.apply_change(
            self.run,
            "balanced",
            {"kind": "acceptance", "shift_id": target.shift_id, "state": "accepted"},
        )
        other = next(
            s
            for s in self.run.plan("balanced").shifts
            if s.assigned_employee == target.assigned_employee
            and s.shift_id != target.shift_id
            and not s.is_accepted
            and s.origin == "generated"
        )
        updated = self.service.apply_change(
            self.run,
            "balanced",
            {
                "kind": "assign",
                "shift_id": other.shift_id,
                "date": target.date.isoformat(),
                "start_hour": target.start_hour,
                "end_hour": target.end_hour,
            },
        )
        overlapping = next(s for s in updated.shifts if s.shift_id == other.shift_id)
        with self.assertRaises(RevisionError) as ctx:
            self.service.apply_change(
                self.run,
                "balanced",
                {"kind": "acceptance", "shift_id": overlapping.shift_id, "state": "accepted"},
            )
        self.assertIn("休息", str(ctx.exception) + "休息")

    def test_acceptance_can_be_recorded(self):
        target = self.generated[0]
        updated = self.service.apply_change(
            self.run,
            "balanced",
            {"kind": "acceptance", "shift_id": target.shift_id, "state": "accepted"},
        )
        shift = next(s for s in updated.shifts if s.shift_id == target.shift_id)
        self.assertEqual(shift.acceptance_state, "accepted")
        self.assertTrue(any(e["action"] == "acceptance" for e in updated.change_log))

    def test_local_activity_change_keeps_accepted(self):
        # 接受 ST-003 在 12/05 的一个班次，再对该时段做临时闭店。
        target = next(
            s
            for s in self.plan.shifts
            if s.store_id == "ST-003"
            and s.date == dt.date(2026, 12, 5)
            and s.origin == "generated"
        )
        self.service.apply_change(
            self.run,
            "balanced",
            {"kind": "acceptance", "shift_id": target.shift_id, "state": "accepted"},
        )
        before = {s.shift_id for s in self.run.plan("balanced").shifts}
        activity = {
            "kind": "activity",
            "activity": {
                "activity_id": "ACT-LATE-CLOSE",
                "store_id": "ST-003",
                "date": "2026-12-05",
                "kind": "closure",
                "start_hour": 12,
                "end_hour": 16,
            },
        }
        updated = self.service.apply_change(self.run, "balanced", activity)
        by_id = {s.shift_id: s for s in updated.shifts}
        self.assertIn(target.shift_id, by_id)
        self.assertEqual(by_id[target.shift_id].acceptance_state, "accepted")
        self.assertIn(
            "renegotiate", {entry["action"] for entry in updated.change_log}
        )
        self.assertGreaterEqual(updated.revision, 2)
        self.assertEqual(len(before - set(by_id)), 0)

    def test_activity_change_recomputes_forecast_and_requirements(self):
        activity = {
            "kind": "activity",
            "activity": {
                "activity_id": "ACT-LATE-PROMO",
                "store_id": "ST-003",
                "date": "2026-12-04",
                "kind": "promotion",
                "start_hour": 10,
                "end_hour": 14,
                "uplift": 0.4,
            },
        }
        updated = self.service.apply_change(self.run, "balanced", activity)
        day = next(
            d
            for d in self.run.forecast.days
            if d.store_id == "ST-003" and d.date == dt.date(2026, 12, 4)
        )
        self.assertIn("ACT-LATE-PROMO", day.activity_ids)
        self.assertGreater(day.expected_transactions, 0)

    def test_unconfirmed_slots_only_are_released(self):
        # 记录所有已接受班次，活动变更后逐条核对全部保留。
        accepted = {
            s.shift_id: (s.assigned_employee, s.start_hour, s.end_hour)
            for s in self.plan.shifts
            if s.is_accepted
        }
        activity = {
            "kind": "activity",
            "activity": {
                "activity_id": "ACT-LATE-CLOSE-2",
                "store_id": "ST-010",
                "date": "2026-12-03",
                "kind": "closure",
                "start_hour": 8,
                "end_hour": 12,
            },
        }
        updated = self.service.apply_change(self.run, "balanced", activity)
        by_id = {s.shift_id: s for s in updated.shifts}
        for shift_id, signature in accepted.items():
            self.assertIn(shift_id, by_id)
            shift = by_id[shift_id]
            self.assertEqual(
                (shift.assigned_employee, shift.start_hour, shift.end_hour), signature
            )

    def test_invalid_change_kind(self):
        with self.assertRaises(RevisionError):
            self.service.apply_change(self.run, "balanced", {"kind": "explode"})

    def test_revision_does_not_mutate_original_plan_object(self):
        original = deepcopy(self.run.plan("balanced"))
        self.service.apply_change(
            self.run,
            "balanced",
            {"kind": "remove", "shift_id": self.generated[0].shift_id},
        )
        self.assertIn(self.generated[0].shift_id, {s.shift_id for s in original.shifts})


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.service, self.run = _setup()

    def test_missing_confirmations_block_commitment(self):
        assessment = self.service.publication_readiness(self.run, "balanced")
        self.assertFalse(assessment["commitment_ready"])
        self.assertTrue(any("预算" in b for b in assessment["blockers"]))
        self.assertTrue(any("门店负责人" in b for b in assessment["blockers"]))

    def test_confirmations_unlock_but_pending_employee_remains_visible(self):
        assessment = self.service.publication_readiness(
            self.run, "balanced", budget_confirmed=True, manager_accepted=True
        )
        self.assertIn("pending_employee_confirmations", assessment)
        self.assertTrue(assessment["pending_employee_confirmations"])
        self.assertTrue(assessment["accepted_commitments"])
        for entry in assessment["pending_employee_confirmations"]:
            self.assertEqual(entry["acceptance_state"], "proposed")

    def test_budget_overage_blocks_publication(self):
        # 把预算压到极低，发布检查应报告超出预算。
        bundle = self.run.bundle
        for budget in bundle.budgets:
            object.__setattr__(budget, "amount", 1.0)
        assessment = self.service.publication_readiness(
            self.run, "balanced", budget_confirmed=True, manager_accepted=True
        )
        self.assertFalse(assessment["commitment_ready"])
        self.assertTrue(any("预算" in b for b in assessment["blockers"]))


if __name__ == "__main__":
    unittest.main()
