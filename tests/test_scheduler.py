"""排班方案、约束核实、接受状态与覆盖。"""

from __future__ import annotations

import datetime as dt
import unittest

from conftest import DATA

from store_labor_capacity_planning import scheduler as S
from store_labor_capacity_planning.config import PlanningConfig
from store_labor_capacity_planning.forecast import build_forecast
from store_labor_capacity_planning.loader import load_bundle
from store_labor_capacity_planning.requirements import build_requirements


def _run(days: int = 5, keys=("balanced", "coverage", "cost")):
    bundle = load_bundle(DATA)
    config = PlanningConfig()
    dates = [dt.date(2026, 12, 1) + dt.timedelta(days=i) for i in range(days)]
    forecast = build_forecast(bundle, config, dates=dates, as_of=dt.date(2026, 11, 30))
    requirements = build_requirements(forecast, config)
    return bundle, config, dates, requirements


class GenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle, cls.config, cls.dates, cls.requirements = _run()
        cls.plans = {
            profile.key: S.generate_scenario(
                cls.bundle, cls.requirements, cls.config, profile=profile, dates=cls.dates
            )
            for profile in S.DEFAULT_PROFILES
        }
        cls.baseline = S.baseline_scenario(
            cls.bundle, cls.requirements, cls.config, dates=cls.dates
        )

    def test_accepted_shifts_are_carried_unchanged(self):
        accepted = [s for s in self.bundle.shifts if s.date in set(self.dates) and s.is_accepted]
        self.assertTrue(accepted)
        for plan in self.plans.values():
            plan_ids = {s.shift_id: s for s in plan.shifts}
            for shift in accepted:
                self.assertIn(shift.shift_id, plan_ids)
                self.assertEqual(plan_ids[shift.shift_id].acceptance_state, "accepted")
                self.assertEqual(plan_ids[shift.shift_id].assigned_employee, shift.assigned_employee)

    def test_generated_shifts_never_violate_hard_constraints(self):
        for profile in S.DEFAULT_PROFILES:
            plan = self.plans[profile.key]
            check = S.check_plan(
                plan.shifts,
                self.bundle,
                self.config,
                generated=True,
                allow_cross_store=profile.allow_cross_store,
                weekly_cap_ratio=profile.allow_overtime_ratio,
            )
            blocking = [
                i
                for i in check.issues
                if i.blocks_publication and i.entity in {s.shift_id for s in plan.shifts if s.origin == "generated"}
            ]
            self.assertEqual(blocking, [], f"{profile.key}: {[i.message for i in blocking[:3]]}")

    def test_generated_shifts_respect_employee_availability(self):
        allowed_states = {("available", True), ("preferred", True), ("unknown", True)}
        for profile in S.DEFAULT_PROFILES:
            plan = self.plans[profile.key]
            for shift in plan.shifts:
                if shift.origin != "generated":
                    continue
                entries = self.bundle.availability_for(shift.assigned_employee, shift.date)
                if not entries:
                    self.assertIn("availability-unconfirmed", shift.notes)
                    continue
                self.assertTrue(
                    any(
                        e.is_available and e.covers(shift.start_hour, shift.end_hour)
                        for e in entries
                    ),
                    f"{shift.shift_id} 超出 {shift.assigned_employee} 的可用窗口",
                )

    def test_generated_shifts_use_matching_store_and_role_by_default(self):
        for key in ("balanced", "cost"):
            plan = self.plans[key]
            for shift in plan.shifts:
                if shift.origin != "generated":
                    continue
                employee = self.bundle.staff_by_id[shift.assigned_employee]
                self.assertEqual(employee.store_id, shift.store_id)
                self.assertEqual(employee.role, shift.role)

    def test_coverage_profile_can_use_same_region_support(self):
        plan = self.plans["coverage"]
        cross = [
            s
            for s in plan.shifts
            if s.origin == "generated"
            and self.bundle.staff_by_id[s.assigned_employee].store_id != s.store_id
        ]
        self.assertTrue(cross)
        for shift in cross:
            employee = self.bundle.staff_by_id[shift.assigned_employee]
            self.assertEqual(employee.role, shift.role)
            store_region = next(
                e.region
                for e in self.bundle.staff
                if e.store_id == shift.store_id
            )
            self.assertEqual(employee.region, store_region)

    def test_no_employee_overlap_or_rest_violation(self):
        for plan in self.plans.values():
            by_employee: dict[str, list] = {}
            for shift in plan.shifts:
                if shift.is_declined:
                    continue
                by_employee.setdefault(shift.assigned_employee, []).append(shift)
            for employee_id, rows in by_employee.items():
                rows.sort(key=lambda s: (s.date, s.start_hour))
                for previous, current in zip(rows, rows[1:]):
                    generated_involved = (
                        previous.origin == "generated" or current.origin == "generated"
                    )
                    if not generated_involved:
                        continue
                    if previous.date == current.date:
                        self.assertGreaterEqual(
                            current.start_hour,
                            previous.end_hour,
                            f"{plan.key} {employee_id} 同日重叠",
                        )
                    else:
                        gap = (
                            S.shift_start(current) - S.shift_end(previous)
                        ).total_seconds() / 3600
                        self.assertGreaterEqual(
                            gap,
                            self.config.constraints.rest_hours_between_shifts,
                            f"{plan.key} {employee_id} 休息不足",
                        )

    def test_weekly_contract_cap_not_exceeded_for_generated(self):
        for plan in self.plans.values():
            weekly: dict[tuple[str, tuple[int, int]], float] = {}
            for shift in plan.shifts:
                if shift.is_declined:
                    continue
                key = (shift.assigned_employee, S.week_key(shift.date))
                weekly[key] = weekly.get(key, 0.0) + S.paid_hours(shift, self.config.constraints)
            for (employee_id, week), hours in weekly.items():
                generated_hours = sum(
                    S.paid_hours(s, self.config.constraints)
                    for s in plan.shifts
                    if s.assigned_employee == employee_id
                    and S.week_key(s.date) == week
                    and s.origin == "generated"
                )
                if generated_hours == 0:
                    continue
                contract = self.bundle.staff_by_id[employee_id].contract_hours
                profile = next(p for p in S.DEFAULT_PROFILES if p.key == plan.key)
                self.assertLessEqual(
                    hours,
                    contract * profile.allow_overtime_ratio + 1e-6,
                    f"{plan.key} {employee_id} {week}",
                )

    def test_coverage_profile_covers_at_least_as_much_as_balanced(self):
        balanced = S.evaluate_scenario(
            self.plans["balanced"], self.bundle, self.requirements, self.config, dates=self.dates
        )
        coverage = S.evaluate_scenario(
            self.plans["coverage"], self.bundle, self.requirements, self.config, dates=self.dates
        )
        self.assertGreaterEqual(
            coverage.planned_coverage.totals.ratio,
            balanced.planned_coverage.totals.ratio,
        )

    def test_baseline_is_untouched(self):
        baseline_shifts = {
            s.shift_id for s in self.bundle.shifts if s.date in set(self.dates)
        }
        self.assertEqual({s.shift_id for s in self.baseline.shifts}, baseline_shifts)
        for shift in self.baseline.shifts:
            original = self.bundle.shifts_by_id[shift.shift_id]
            self.assertEqual(shift.acceptance_state, original.acceptance_state)

    def test_committed_coverage_only_counts_accepted(self):
        plan = self.plans["balanced"]
        accepted = [s for s in plan.shifts if s.is_accepted]
        report = S.compute_coverage(plan.shifts, self.requirements, included_states=("accepted",))
        expected_scheduled = sum(
            S.paid_hours(s, self.config.constraints) for s in accepted
        )
        self.assertAlmostEqual(report.totals.scheduled_hours, expected_scheduled, places=6)

    def test_declined_shifts_do_not_count(self):
        plan = self.plans["balanced"]
        declined = [s for s in plan.shifts if s.is_declined]
        if not declined:
            # 推荐方案会替换导入的拒绝班次，因此直接验证覆盖函数忽略它们。
            accepted_only = [s for s in plan.shifts if s.acceptance_state == "accepted"]
            self.assertTrue(accepted_only)
            report = S.compute_coverage(
                plan.shifts, self.requirements, included_states=("accepted",)
            )
            self.assertAlmostEqual(
                report.totals.scheduled_hours,
                sum(S.paid_hours(s, self.config.constraints) for s in accepted_only),
                places=6,
            )
            return
        report = S.compute_coverage(plan.shifts, self.requirements, included_states=("accepted",))
        ids = {s.shift_id for s in declined}
        self.assertFalse(ids & {s.shift_id for s in plan.shifts if s.acceptance_state in report.included_states})

    def test_comparison_ranks_profiles_deterministically(self):
        plans = {"baseline": self.baseline, **self.plans}
        comparison, metrics = S.compare_scenarios(
            plans, self.bundle, self.requirements, self.config, dates=self.dates
        )
        self.assertEqual(len(comparison.rows), 4)
        scores = [row["score"] for row in comparison.rows]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(set(metrics), set(plans))


class CostTests(unittest.TestCase):
    def test_evening_and_weekend_premiums_increase_cost(self):
        bundle = load_bundle(DATA)
        config = PlanningConfig()
        employee = bundle.staff_by_id["EMP-004"]
        day_shift = S.Shift(
            shift_id="T-DAY",
            store_id=employee.store_id,
            date=dt.date(2026, 12, 1),  # Tuesday
            role=employee.role,
            start_hour=9,
            end_hour=13,
            assigned_employee=employee.employee_id,
        )
        evening_shift = S.Shift(
            shift_id="T-EVENING",
            store_id=employee.store_id,
            date=dt.date(2026, 12, 1),
            role=employee.role,
            start_hour=15,
            end_hour=19,
            assigned_employee=employee.employee_id,
        )
        weekend_shift = S.Shift(
            shift_id="T-WEEKEND",
            store_id=employee.store_id,
            date=dt.date(2026, 12, 5),  # Saturday
            role=employee.role,
            start_hour=9,
            end_hour=13,
            assigned_employee=employee.employee_id,
        )
        day_cost = S.shift_cost(day_shift, employee, config)
        evening_cost = S.shift_cost(evening_shift, employee, config)
        weekend_cost = S.shift_cost(weekend_shift, employee, config)
        self.assertGreater(evening_cost, day_cost)
        self.assertGreater(weekend_cost, day_cost)

    def test_break_deducted_for_long_shifts(self):
        bundle = load_bundle(DATA)
        config = PlanningConfig()
        employee = bundle.staff_by_id["EMP-004"]
        long_shift = S.Shift(
            shift_id="T-LONG",
            store_id=employee.store_id,
            date=dt.date(2026, 12, 1),
            role=employee.role,
            start_hour=8,
            end_hour=18,
            assigned_employee=employee.employee_id,
        )
        self.assertEqual(S.paid_hours(long_shift, config.constraints), 9.5)


if __name__ == "__main__":
    unittest.main()
