"""预测与岗位需求。"""

from __future__ import annotations

import datetime as dt
import unittest

from conftest import DATA

from store_labor_capacity_planning.config import PlanningConfig
from store_labor_capacity_planning.forecast import build_forecast
from store_labor_capacity_planning.loader import load_bundle
from store_labor_capacity_planning.models import Activity
from store_labor_capacity_planning.requirements import build_requirements, requirement_totals


class ForecastTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(DATA)
        cls.config = PlanningConfig()
        cls.dates = [dt.date(2026, 12, 1) + dt.timedelta(days=i) for i in range(5)]
        cls.forecast = build_forecast(
            cls.bundle, cls.config, dates=cls.dates, as_of=dt.date(2026, 11, 30)
        )

    def test_each_store_and_date_has_cells(self):
        keys = {(day.store_id, day.date) for day in self.forecast.days}
        self.assertEqual(len(keys), len(self.bundle.stores) * len(self.dates))
        for day in self.forecast.days:
            self.assertTrue(day.cells)
            self.assertTrue(day.open_hours)
            self.assertGreater(day.expected_transactions, 0)

    def test_missing_traffic_is_estimated_from_transactions_not_zero(self):
        st000 = next(
            day
            for day in self.forecast.days
            if day.store_id == "ST-000" and day.date == self.dates[0]
        )
        self.assertIsNotNone(st000.cells[0].expected_traffic)
        self.assertGreater(st000.cells[0].expected_traffic or 0, 0)
        self.assertIn(
            st000.cells[0].traffic_basis,
            {"conversion-estimate", "observed-support"},
        )

    def test_store_without_history_uses_fallback_and_low_confidence(self):
        st018 = next(
            day
            for day in self.forecast.days
            if day.store_id == "ST-018" and day.date == self.dates[0]
        )
        self.assertLess(st018.confidence, 0.5)
        self.assertTrue(any("全局均值" in note for note in st018.notes))

    def test_closed_hours_are_explicit(self):
        closed = Activity(
            activity_id="ACT-TEST-CLOSE",
            store_id="ST-001",
            date=self.dates[1],
            kind="closure",
            start_hour=10,
            end_hour=14,
        )
        bundle = load_bundle(DATA)
        object.__setattr__(
            bundle,
            "activities",
            list(bundle.activities) + [closed],
        )
        forecast = build_forecast(
            bundle, self.config, dates=[self.dates[1]], as_of=dt.date(2026, 11, 30)
        )
        day = next(d for d in forecast.days if d.store_id == "ST-001")
        self.assertEqual(day.closed_hours, (10, 11, 12, 13))
        for cell in day.cells:
            if cell.hour in day.closed_hours:
                self.assertFalse(cell.open)
                self.assertIsNone(cell.expected_traffic)
                self.assertIsNone(cell.expected_transactions)

    def test_promotion_raises_forecast_only_inside_window(self):
        promo = Activity(
            activity_id="ACT-TEST-PROMO",
            store_id="ST-001",
            date=self.dates[2],
            kind="promotion",
            start_hour=10,
            end_hour=12,
            uplift=0.5,
        )
        bundle = load_bundle(DATA)
        object.__setattr__(bundle, "activities", [promo])
        forecast = build_forecast(
            bundle, self.config, dates=[self.dates[2]], as_of=dt.date(2026, 11, 30)
        )
        day = next(d for d in forecast.days if d.store_id == "ST-001")
        baseline = build_forecast(
            load_bundle(DATA), self.config, dates=[self.dates[2]], as_of=dt.date(2026, 11, 30)
        )
        base_day = next(d for d in baseline.days if d.store_id == "ST-001")
        promo_cell = next(c for c in day.cells if c.hour == 10)
        base_cell = next(c for c in base_day.cells if c.hour == 10)
        self.assertAlmostEqual(
            promo_cell.expected_transactions or 0,
            (base_cell.expected_transactions or 0) * 1.5,
            places=5,
        )
        outside = next(c for c in day.cells if c.hour == 15)
        base_outside = next(c for c in base_day.cells if c.hour == 15)
        self.assertAlmostEqual(
            outside.expected_transactions or 0,
            base_outside.expected_transactions or 0,
            places=5,
        )

    def test_forecast_does_not_use_future_history(self):
        as_of = dt.date(2026, 11, 20)
        bundle_all = load_bundle(DATA)
        bundle_truncated = load_bundle(DATA)
        object.__setattr__(
            bundle_truncated,
            "demand",
            [row for row in bundle_truncated.demand if row.date <= as_of],
        )
        self.assertLess(
            len(bundle_truncated.demand), len(bundle_all.demand)
        )
        a = build_forecast(
            bundle_all, self.config, dates=[self.dates[0]], as_of=as_of
        )
        b = build_forecast(
            bundle_truncated, self.config, dates=[self.dates[0]], as_of=as_of
        )
        day_a = next(d for d in a.days if d.store_id == "ST-003")
        day_b = next(d for d in b.days if d.store_id == "ST-003")
        self.assertAlmostEqual(
            day_a.expected_transactions, day_b.expected_transactions, places=6
        )


class RequirementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bundle = load_bundle(DATA)
        config = PlanningConfig()
        dates = [dt.date(2026, 12, 1) + dt.timedelta(days=i) for i in range(3)]
        forecast = build_forecast(bundle, config, dates=dates, as_of=dt.date(2026, 11, 30))
        cls.requirements = build_requirements(forecast, config)

    def test_all_roles_have_requirements(self):
        totals = requirement_totals(self.requirements)
        for role in ("sales", "service", "receiving", "food"):
            self.assertIn(role, totals)
            self.assertGreater(totals[role], 0)

    def test_receiving_and_food_do_not_depend_on_sales_volume(self):
        # 收货/食品安全任务在每个营业日固定存在，即使交易预测很低。
        for requirement in self.requirements.requirements:
            if requirement.role in {"receiving", "food"}:
                self.assertGreater(requirement.total_hours, 0)
                self.assertTrue(requirement.notes)

    def test_high_peak_service_minimum(self):
        for requirement in self.requirements.requirements:
            if requirement.role != "service":
                continue
            self.assertLessEqual(max(requirement.by_hour.values()), 1.0)


if __name__ == "__main__":
    unittest.main()
