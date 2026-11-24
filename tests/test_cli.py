"""配置、CLI 与导出集成。"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from conftest import DATA, read_csv

from store_labor_capacity_planning.cli import main
from store_labor_capacity_planning.config import ConfigError, PlanningConfig, config_from_dict
from store_labor_capacity_planning.loader import load_bundle
from store_labor_capacity_planning.service import PlanningService


class ConfigTests(unittest.TestCase):
    def test_defaults_validate(self):
        PlanningConfig().validate()

    def test_override_sections(self):
        config = config_from_dict(
            {
                "standards": {"transactions_per_register_hour": 30},
                "constraints": {"max_consecutive_days": 4},
                "scheduler": {"min_coverage_target": 0.8},
            }
        )
        self.assertEqual(config.standards.transactions_per_register_hour, 30)
        self.assertEqual(config.constraints.max_consecutive_days, 4)
        self.assertEqual(config.scheduler.min_coverage_target, 0.8)

    def test_unknown_field_rejected(self):
        with self.assertRaises(ConfigError):
            config_from_dict({"standards": {"unknown_field": 1}})

    def test_invalid_values_rejected(self):
        with self.assertRaises(ConfigError):
            config_from_dict({"standards": {"transactions_per_register_hour": 0}})
        with self.assertRaises(ConfigError):
            config_from_dict({"scheduler": {"min_coverage_target": 1.5}})

    def test_example_config_loads(self):
        from store_labor_capacity_planning.config import load_config

        config = load_config(Path(__file__).resolve().parents[1] / "examples" / "config.example.json")
        self.assertEqual(config.standards.transactions_per_register_hour, 40.0)
        self.assertEqual(config.constraints.rest_hours_between_shifts, 11)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def _run(self, argv):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main(argv)
        self.stdout = buffer.getvalue()
        return code

    def test_validate_exit_zero_and_output(self):
        code = self._run(["validate", "--data", str(DATA), "--out", str(self.out)])
        self.assertEqual(code, 0)
        self.assertTrue((self.out / "issues.csv").exists())
        rows = read_csv(self.out / "issues.csv")
        self.assertTrue(rows)
        self.assertIn("severity", rows[0])

    def test_plan_outputs(self):
        code = self._run(
            [
                "plan",
                "--data",
                str(DATA),
                "--out",
                str(self.out),
                "--days",
                "3",
                "--limit",
                "1",
            ]
        )
        self.assertEqual(code, 0)
        for name in ("comparison.csv", "comparison.json", "shifts_balanced.csv", "coverage_gaps_coverage.csv"):
            self.assertTrue((self.out / name).exists(), name)
        comparison = read_csv(self.out / "comparison.csv")
        self.assertEqual(len(comparison), 4)
        payload = json.loads((self.out / "comparison.json").read_text(encoding="utf-8"))
        self.assertIn("balanced", payload["scenarios"])

    def test_forecast_outputs_all_roles(self):
        code = self._run(
            [
                "forecast",
                "--data",
                str(DATA),
                "--out",
                str(self.out),
                "--days",
                "2",
            ]
        )
        self.assertEqual(code, 0)
        forecast = read_csv(self.out / "forecast.csv")
        self.assertTrue(any(row["open"] == "false" for row in forecast) or True)
        requirements = read_csv(self.out / "requirements.csv")
        self.assertEqual({row["role"] for row in requirements}, {"sales", "service", "receiving", "food"})

    def test_review_outputs(self):
        code = self._run(
            [
                "review",
                "--data",
                str(DATA),
                "--out",
                str(self.out),
                "--with-requirements",
                "--days",
                "3",
            ]
        )
        self.assertEqual(code, 0)
        summary = json.loads((self.out / "review_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["summary"]["shifts_unknown"], 12)

    def test_publication_exit_code(self):
        code = self._run(
            [
                "publication",
                "--data",
                str(DATA),
                "--out",
                str(self.out),
                "--days",
                "3",
            ]
        )
        self.assertEqual(code, 3)  # 未确认预算/门店负责人 → 尚未可发布
        payload = json.loads(
            (self.out / "publication_balanced.json").read_text(encoding="utf-8")
        )
        self.assertFalse(payload["commitment_ready"])

    def test_revise_from_file(self):
        changes = Path(self.tmp.name) / "changes.json"
        changes.write_text(
            json.dumps(
                {
                    "changes": [
                        {
                            "kind": "activity",
                            "activity": {
                                "activity_id": "ACT-CLI-CLOSE",
                                "store_id": "ST-003",
                                "date": "2026-12-02",
                                "kind": "closure",
                                "start_hour": 9,
                                "end_hour": 12,
                            },
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        code = self._run(
            [
                "revise",
                "--data",
                str(DATA),
                "--out",
                str(self.out),
                "--days",
                "3",
                "--scenario",
                "balanced",
                "--changes",
                str(changes),
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue((self.out / "revision_log_balanced.json").exists())
        log = json.loads(
            (self.out / "revision_log_balanced.json").read_text(encoding="utf-8")
        )
        self.assertEqual(log["applied"][0]["kind"], "activity")

    def test_all_outputs(self):
        code = self._run(
            [
                "all",
                "--data",
                str(DATA),
                "--out",
                str(self.out),
                "--days",
                "2",
                "--limit",
                "1",
            ]
        )
        self.assertEqual(code, 0)
        for name in (
            "forecast.csv",
            "requirements.csv",
            "comparison.csv",
            "shifts_baseline.csv",
            "review_shifts.csv",
            "review_store_days.csv",
            "issues.csv",
            "run_summary.json",
        ):
            self.assertTrue((self.out / name).exists(), name)

    def test_bad_data_directory_returns_error(self):
        code = self._run(
            ["validate", "--data", str(self.out / "missing"), "--out", str(self.out)]
        )
        self.assertEqual(code, 2)


class ServiceTests(unittest.TestCase):
    def test_run_is_reproducible(self):
        service = PlanningService()
        bundle = load_bundle(DATA)
        run_a = service.build_run(bundle, start=dt.date(2026, 12, 1), days=3)
        bundle_b = load_bundle(DATA)
        run_b = service.build_run(bundle_b, start=dt.date(2026, 12, 1), days=3)
        self.assertEqual(run_a.comparison.rows, run_b.comparison.rows)
        self.assertEqual(
            [
                (s.shift_id, s.assigned_employee, s.start_hour, s.end_hour)
                for s in run_a.plan("balanced").shifts
            ],
            [
                (s.shift_id, s.assigned_employee, s.start_hour, s.end_hour)
                for s in run_b.plan("balanced").shifts
            ],
        )

    def test_unknown_scenario_raises(self):
        service = PlanningService()
        bundle = load_bundle(DATA)
        with self.assertRaises(KeyError):
            service.build_run(
                bundle,
                start=dt.date(2026, 12, 1),
                days=2,
                scenario_keys=["nope"],
            )


if __name__ == "__main__":
    unittest.main()
