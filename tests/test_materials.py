import csv
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {'hourly_demand.csv': (['store_id', 'date', 'hour', 'transactions', 'traffic_count', 'traffic_coverage', 'activity_id'], 150), 'staff.csv': (['employee_id', 'store_id', 'role', 'skill', 'contract_hours', 'region'], 54), 'availability.csv': (['employee_id', 'date', 'start_hour', 'end_hour', 'availability_state', 'preference'], 100), 'shift_plans.csv': (['shift_id', 'store_id', 'date', 'role', 'start_hour', 'end_hour', 'assigned_employee', 'acceptance_state'], 72), 'actual_coverage.csv': (['shift_id', 'clocked_minutes', 'task_load', 'service_wait_minutes', 'variance_reason'], 60)}

OPTIONAL = {
    'activities.csv': (
        ['activity_id', 'store_id', 'date', 'kind', 'start_hour', 'end_hour', 'uplift', 'note'],
        6,
    ),
    'budgets.csv': (
        ['region', 'period_start', 'period_end', 'amount', 'currency'],
        9,
    ),
}

class MaterialTests(unittest.TestCase):
    def test_declared_csv_contracts(self):
        for filename, (columns, count) in EXPECTED.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertEqual(len(rows), count)
                self.assertTrue(all(None not in row for row in rows))

    def test_optional_csv_contracts(self):
        for filename, (columns, count) in OPTIONAL.items():
            with self.subTest(filename=filename):
                with (ROOT / "data" / filename).open(encoding="utf-8-sig", newline="") as stream:
                    reader = csv.DictReader(stream)
                    rows = list(reader)
                self.assertEqual(reader.fieldnames, columns)
                self.assertEqual(len(rows), count)

if __name__ == "__main__":
    unittest.main()
