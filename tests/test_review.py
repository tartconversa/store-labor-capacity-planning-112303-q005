"""实际覆盖回看与服务效果。"""

from __future__ import annotations

import datetime as dt
import unittest

from conftest import DATA

from store_labor_capacity_planning.config import PlanningConfig
from store_labor_capacity_planning.forecast import build_forecast
from store_labor_capacity_planning.loader import load_bundle
from store_labor_capacity_planning.requirements import build_requirements
from store_labor_capacity_planning.review import build_review


class ReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(DATA)
        cls.config = PlanningConfig()
        dates = [dt.date(2026, 12, 1) + dt.timedelta(days=i) for i in range(20)]
        forecast = build_forecast(
            cls.bundle, cls.config, dates=dates, as_of=dt.date(2026, 11, 30)
        )
        cls.requirements = build_requirements(forecast, cls.config)
        cls.review = build_review(
            cls.bundle, cls.config, requirements=cls.requirements
        )

    def test_missing_actual_is_unknown_not_zero(self):
        unknown = [row for row in self.review.shifts if row.state == "unknown"]
        self.assertEqual(len(unknown), 12)
        for row in unknown:
            self.assertIsNone(row.clocked_minutes)
            self.assertIsNone(row.clocked_hours)
            self.assertIsNone(row.attendance_ratio)
        summary = self.review.summary
        self.assertEqual(summary["shifts_unknown"], 12)
        self.assertEqual(summary["shifts_observed"], 60)

    def test_observed_shift_attendance_ratio(self):
        row = next(r for r in self.review.shifts if r.state == "observed")
        self.assertIsNotNone(row.clocked_minutes)
        expected = row.clocked_minutes / 60 / row.planned_paid_hours
        self.assertAlmostEqual(row.attendance_ratio, expected, places=6)

    def test_summary_counts_and_shares(self):
        summary = self.review.summary
        self.assertEqual(
            summary["shifts_total"],
            summary["shifts_observed"] + summary["shifts_unknown"],
        )
        self.assertEqual(summary["accepted_shifts"], 24)
        self.assertEqual(summary["declined_shifts"], 24)
        self.assertEqual(summary["proposed_shifts"], 24)
        self.assertEqual(
            sum(summary["variance_reasons"].values()), summary["shifts_observed"]
        )
        self.assertAlmostEqual(summary["absence_share"], 1 / 3, places=3)

    def test_store_day_rows_have_explicit_missing_state(self):
        rows = self.review.store_day_rows()
        self.assertTrue(rows)
        missing = [r for r in rows if r["shifts_missing_actual"] != "0"]
        self.assertTrue(missing)
        for row in missing:
            if row["shifts_observed"] == "0":
                self.assertEqual(row["clocked_hours"], "")
                self.assertEqual(row["attendance_ratio"], "")

    def test_service_wait_is_reported_not_attributed(self):
        summary = self.review.summary
        self.assertIsNotNone(summary["avg_wait_minutes"])
        codes = {issue.code for issue in self.review.issues}
        if summary["avg_wait_minutes"] > self.config.standards.service_wait_target_minutes:
            self.assertIn("review-wait-target", codes)
        assumptions = " ".join(self.review.assumptions)
        self.assertIn("不能仅凭相关", assumptions)

    def test_overtime_warning_for_clocked_over_plan(self):
        codes = {issue.code for issue in self.review.issues}
        # 数据集中有若干打卡超过计划付费工时的班次，应当被标记。
        self.assertIn("review-overtime", codes)


if __name__ == "__main__":
    unittest.main()
