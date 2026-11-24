"""输入读取、缺失语义与关联校验。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from conftest import DATA, copy_data, read_csv, write_csv

from store_labor_capacity_planning.loader import (
    DataError,
    load_bundle,
    require_core_data,
)
from store_labor_capacity_planning.models import (
    normalize_acceptance_state,
    normalize_availability_state,
)


class BundledDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_bundle(DATA)

    def test_counts_and_indexes(self):
        bundle = self.bundle
        self.assertEqual(len(bundle.demand), 150)
        self.assertEqual(len(bundle.staff), 54)
        self.assertEqual(len(bundle.availability), 100)
        self.assertEqual(len(bundle.shifts), 72)
        self.assertEqual(len(bundle.actual), 60)
        self.assertEqual(len(bundle.activities), 6)
        self.assertEqual(len(bundle.budgets), 9)
        self.assertEqual(bundle.staff_by_id["EMP-001"].store_id, "ST-002")

    def test_trailing_missing_traffic_is_unknown_not_zero(self):
        missing = [row for row in self.bundle.demand if row.traffic_count is None]
        self.assertEqual(len(missing), 13)
        for row in missing:
            # 缺失客流不能被解释为 0；估计值也必须是未知。
            self.assertIsNone(row.estimated_traffic)
            self.assertEqual(row.traffic_state, "missing")

    def test_zero_coverage_traffic_does_not_estimate(self):
        uncovered = [
            row
            for row in self.bundle.demand
            if row.traffic_coverage == 0 and row.traffic_count is not None
        ]
        self.assertTrue(uncovered)
        for row in uncovered:
            self.assertIsNone(row.estimated_traffic)
            self.assertEqual(row.traffic_state, "uncovered")

    def test_traffic_estimate_scales_by_coverage(self):
        row = next(
            r
            for r in self.bundle.demand
            if r.traffic_count is not None and r.traffic_coverage == 0.5
        ) if any(
            r.traffic_count is not None and r.traffic_coverage == 0.5
            for r in self.bundle.demand
        ) else next(
            r
            for r in self.bundle.demand
            if r.traffic_count is not None and r.traffic_coverage
        )
        self.assertAlmostEqual(
            row.estimated_traffic, row.traffic_count / row.traffic_coverage
        )

    def test_imported_shifts_carry_business_warnings_not_errors(self):
        codes = {issue.code for issue in self.bundle.issues}
        self.assertIn("shift-cross-store", codes)
        self.assertIn("shift-role-mismatch", codes)
        self.assertIn("store-without-staff", codes)
        self.assertIn("store-without-demand", codes)
        self.assertEqual(len(self.bundle.issues.errors), 0)

    def test_actual_coverage_missing_for_some_shifts(self):
        covered = {record.shift_id for record in self.bundle.actual}
        missing = {shift.shift_id for shift in self.bundle.shifts} - covered
        self.assertEqual(len(missing), 12)
        pending = [
            issue for issue in self.bundle.issues if issue.code == "actual-pending"
        ]
        self.assertEqual(len(pending), 12)

    def test_require_core_data_passes(self):
        require_core_data(self.bundle)


class InvalidDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = copy_data(Path(self.tmp.name))

    def test_missing_traffic_row_is_not_zero(self):
        rows = read_csv(self.data / "hourly_demand.csv")
        rows[0]["traffic_count"] = ""
        rows[0]["traffic_coverage"] = "0.5"
        write_csv(
            self.data / "hourly_demand.csv",
            list(rows[0].keys()),
            rows,
        )
        bundle = load_bundle(self.data)
        row = next(
            r
            for r in bundle.demand
            if r.store_id == rows[0]["store_id"]
            and r.date.isoformat() == rows[0]["date"]
            and str(r.hour) == rows[0]["hour"]
        )
        self.assertIsNone(row.traffic_count)
        self.assertIsNone(row.estimated_traffic)

    def test_invalid_number_is_reported_and_row_skipped(self):
        rows = read_csv(self.data / "hourly_demand.csv")
        rows[0]["transactions"] = "abc"
        write_csv(self.data / "hourly_demand.csv", list(rows[0].keys()), rows)
        bundle = load_bundle(self.data)
        self.assertTrue(any(issue.code == "number-invalid" for issue in bundle.issues))
        self.assertEqual(len(bundle.demand), 149)

    def test_duplicate_demand_key_rejected(self):
        rows = read_csv(self.data / "hourly_demand.csv")
        rows[1]["store_id"] = rows[0]["store_id"]
        rows[1]["date"] = rows[0]["date"]
        rows[1]["hour"] = rows[0]["hour"]
        write_csv(self.data / "hourly_demand.csv", list(rows[0].keys()), rows)
        bundle = load_bundle(self.data)
        self.assertTrue(any(issue.code == "demand-duplicate" for issue in bundle.issues))
        self.assertEqual(len(bundle.demand), 149)

    def test_availability_end_before_start_rejected(self):
        rows = read_csv(self.data / "availability.csv")
        rows[0]["start_hour"] = "15"
        rows[0]["end_hour"] = "9"
        write_csv(self.data / "availability.csv", list(rows[0].keys()), rows)
        bundle = load_bundle(self.data)
        self.assertTrue(any(issue.code == "window-invalid" for issue in bundle.issues))
        self.assertEqual(len(bundle.availability), 99)

    def test_unknown_employee_in_shift_is_error(self):
        rows = read_csv(self.data / "shift_plans.csv")
        rows[0]["assigned_employee"] = "EMP-999"
        write_csv(self.data / "shift_plans.csv", list(rows[0].keys()), rows)
        bundle = load_bundle(self.data)
        self.assertTrue(
            any(issue.code == "shift-unknown-employee" for issue in bundle.issues)
        )
        self.assertGreaterEqual(len(bundle.issues.errors), 1)

    def test_missing_core_file_blocks_publication(self):
        (self.data / "availability.csv").unlink()
        bundle = load_bundle(self.data)
        blocking = [issue for issue in bundle.issues if issue.blocks_publication]
        self.assertTrue(blocking)
        self.assertTrue(any(issue.code == "file-missing" for issue in blocking))

    def test_missing_directory_raises(self):
        with self.assertRaises(DataError):
            load_bundle(Path(self.tmp.name) / "does-not-exist")

    def test_empty_core_data_cannot_run(self):
        for name in ("hourly_demand.csv", "staff.csv", "availability.csv", "shift_plans.csv", "actual_coverage.csv"):
            (self.data / name).unlink()
        bundle = load_bundle(self.data)
        with self.assertRaises(DataError):
            require_core_data(bundle)


class NormalizationTests(unittest.TestCase):
    def test_availability_aliases(self):
        self.assertEqual(normalize_availability_state("preferred"), "preferred")
        self.assertEqual(normalize_availability_state("未知"), "unknown")
        self.assertEqual(normalize_availability_state(""), "unknown")
        self.assertEqual(normalize_availability_state("旷工"), "unknown")

    def test_acceptance_aliases(self):
        self.assertEqual(normalize_acceptance_state("pending"), "proposed")
        self.assertEqual(normalize_acceptance_state("confirmed"), "accepted")
        self.assertEqual(normalize_acceptance_state(""), "unknown")
        self.assertEqual(normalize_acceptance_state("maybe"), "unknown")


if __name__ == "__main__":
    unittest.main()
