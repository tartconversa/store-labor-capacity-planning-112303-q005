"""命令行界面（中文）。

子命令：

* ``validate``：只校验资料并列出问题；
* ``forecast``：导出小时预测；
* ``plan``：生成方案、比较并导出班次与缺口；
* ``review``：用实际覆盖回看计划与服务效果；
* ``revise``：对方案应用 JSON 变更（新增/删除/接受/活动变更）；
* ``publication``：检查发布前的三项确认与阻断项；
* ``all``：一次完成预测、方案、比较、回看和导出。

所有输出目录默认 ``output/``，不存在时自动创建。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config
from .errors import IssueBag
from .export import (
    export_comparison,
    export_coverage,
    export_forecast,
    export_issues,
    export_requirements,
    export_review_shifts,
    export_review_store_days,
    export_shifts,
    write_json,
)
from .loader import DataError, load_bundle
from .models import ScenarioPlan
from .review import build_review
from .service import PlanningRun, PlanningService


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"日期格式应为 YYYY-MM-DD: {value}") from exc


def _issue_line(issue) -> str:
    where = ""
    if issue.source:
        where = issue.source
        if issue.row:
            where += f":{issue.row}"
        where += " "
    return f"[{issue.severity.label}] {where}{issue.code} {issue.message}"


def _print_issues(issues: IssueBag, *, limit: int | None = None, title: str | None = None) -> None:
    counts = issues.counts
    if title:
        print(title)
    print(
        f"问题汇总：错误 {counts['error']}，警告 {counts['warning']}，提示 {counts['info']}"
    )
    shown = 0
    for issue in issues.sorted_items():
        if limit is not None and shown >= limit:
            remaining = len(issues) - shown
            if remaining > 0:
                print(f"…… 其余 {remaining} 条见 issues.csv")
            break
        print("  " + _issue_line(issue))
        shown += 1


def _write_plan_outputs(
    out_dir: Path,
    run: PlanningRun,
    key: str,
    *,
    service: PlanningService,
) -> dict:
    plan = run.plan(key)
    metrics = run.metrics_for(key)
    export_shifts(out_dir / f"shifts_{key}.csv", plan, bundle=run.bundle, config=service.config)
    export_coverage(out_dir / f"coverage_gaps_{key}.csv", plan, metrics.planned_coverage)
    export_issues(out_dir / f"issues_{key}.csv", IssueBag(plan.issues))
    write_json(out_dir / f"plan_{key}.json", _plan_payload(plan, metrics))
    return {"plan": plan, "metrics": metrics}


def _plan_payload(plan: ScenarioPlan, metrics) -> dict:
    return {
        "scenario": plan.key,
        "name": plan.name,
        "description": plan.description,
        "revision": plan.revision,
        "metadata": plan.metadata,
        "metrics": metrics.to_dict(),
        "change_log": plan.change_log,
    }


def _resolve_key(run: PlanningRun, key: str) -> str:
    if key in run.plans:
        return key
    lowered = key.lower()
    aliases = {
        "recommended": "balanced",
        "均衡": "balanced",
        "覆盖": "coverage",
        "成本": "cost",
        "基线": "baseline",
    }
    if lowered in aliases and aliases[lowered] in run.plans:
        return aliases[lowered]
    raise SystemExit(
        f"未知方案 {key}；可用: {', '.join(sorted(run.plans))}"
    )


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
    bundle = load_bundle(
        args.data,
        activities_path=args.activities,
        budgets_path=args.budgets,
    )
    _print_issues(bundle.issues, title="资料校验")
    out = Path(args.out)
    export_issues(out / "issues.csv", bundle.issues)
    print(f"已写出 {out / 'issues.csv'}")
    print(
        f"读取：需求 {len(bundle.demand)} 行，员工 {len(bundle.staff)} 人，可用性 {len(bundle.availability)} 行，"
        f"导入班次 {len(bundle.shifts)} 行，实际覆盖 {len(bundle.actual)} 行，活动 {len(bundle.activities)} 行"
    )
    return 1 if bundle.issues.has_errors else 0


def _load_and_run(args: argparse.Namespace, service: PlanningService) -> PlanningRun:
    bundle = load_bundle(
        args.data,
        activities_path=getattr(args, "activities", None),
        budgets_path=getattr(args, "budgets", None),
    )
    start = getattr(args, "start", None) or dt.date(2026, 12, 1)
    days = getattr(args, "days", 20)
    as_of = getattr(args, "as_of", None)
    scenarios = getattr(args, "scenarios", None)
    if scenarios:
        scenarios = [s.strip() for s in scenarios.split(",") if s.strip()]
    return service.build_run(
        bundle, start=start, days=days, as_of=as_of, scenario_keys=scenarios
    )


def cmd_forecast(args: argparse.Namespace) -> int:
    service = PlanningService(load_config(args.config))
    run = _load_and_run(args, service)
    out = Path(args.out)
    export_forecast(out / "forecast.csv", run.forecast)
    export_requirements(out / "requirements.csv", run.requirements)
    write_json(
        out / "forecast_summary.json",
        {
            "as_of": run.as_of.isoformat() if run.as_of else None,
            "dates": [d.isoformat() for d in run.dates],
            "assumptions": run.forecast.assumptions,
            "total_requirement_hours": round(run.requirements.total_hours, 2),
            "stores": len({day.store_id for day in run.forecast.days}),
        },
    )
    _print_issues(run.issues, limit=args.limit, title="预测与需求")
    print(f"已写出 {out / 'forecast.csv'} 和 {out / 'requirements.csv'}")
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    service = PlanningService(load_config(args.config))
    run = _load_and_run(args, service)
    out = Path(args.out)
    export_comparison(out / "comparison.csv", run.comparison)
    for key in run.plans:
        _write_plan_outputs(out, run, key, service=service)
    write_json(
        out / "comparison.json",
        {
            "rows": run.comparison.rows,
            "baseline": run.comparison.baseline_key,
            "scenarios": {
                key: run.metrics_for(key).to_dict() for key in run.plans
            },
        },
    )
    _print_comparison(run)
    _print_issues(run.issues, limit=args.limit)
    print(f"已写出 {out / 'comparison.csv'} 及各方案 shifts_*.csv / coverage_gaps_*.csv")
    return 0


def _print_comparison(run: PlanningRun) -> None:
    print("方案比较（按综合分排序）：")
    header = (
        f"{'方案':<10}{'分':>6}{'计划覆盖':>10}{'已确认覆盖':>12}"
        f"{'工时':>9}{'成本':>11}{'违规':>6}{'阻断':>6}{'待协商':>8}"
    )
    print(header)
    print("-" * len(header))
    name_map = {key: run.plans[key].name for key in run.plans}
    for row in run.comparison.rows:
        print(
            f"{name_map.get(row['scenario'], row['scenario']):<10}"
            f"{row['score']:>6.1f}"
            f"{row['planned_coverage']*100:>9.1f}%"
            f"{row['committed_coverage']*100:>11.1f}%"
            f"{row['scheduled_hours']:>9.0f}"
            f"{row['cost']:>11,.0f}"
            f"{row['violations']:>6d}"
            f"{row['blocking']:>6d}"
            f"{row['requires_renegotiation']:>8d}"
        )
    print("说明：已确认覆盖只统计员工已接受的班次；待协商为导入已确认班次与当前可用性/岗位冲突的数量。")


def cmd_review(args: argparse.Namespace) -> int:
    service = PlanningService(load_config(args.config))
    bundle = load_bundle(
        args.data,
        activities_path=getattr(args, "activities", None),
        budgets_path=getattr(args, "budgets", None),
    )
    run = None
    if args.with_requirements:
        run = service.build_run(bundle, start=args.start, days=args.days, as_of=args.as_of)
        requirements = run.requirements
    else:
        requirements = None
    review = build_review(bundle, service.config, requirements=requirements)
    out = Path(args.out)
    export_review_shifts(out / "review_shifts.csv", review)
    export_review_store_days(out / "review_store_days.csv", review)
    export_issues(out / "review_issues.csv", IssueBag(review.issues))
    write_json(out / "review_summary.json", {"summary": review.summary, "assumptions": review.assumptions})
    print("回看摘要：")
    for key, value in review.summary.items():
        print(f"  {key}: {value}")
    _print_issues(IssueBag(review.issues), limit=args.limit, title="回看提示")
    print(f"已写出 {out / 'review_shifts.csv'} 和 {out / 'review_store_days.csv'}")
    return 0


def cmd_revise(args: argparse.Namespace) -> int:
    service = PlanningService(load_config(args.config))
    run = service.build_run(
        load_bundle(args.data, activities_path=args.activities, budgets_path=args.budgets),
        start=args.start,
        days=args.days,
        as_of=args.as_of,
        scenario_keys=args.scenarios.split(",") if args.scenarios else None,
    )
    key = _resolve_key(run, args.scenario)
    changes = json.loads(Path(args.changes).read_text(encoding="utf-8"))
    if isinstance(changes, dict):
        changes = changes.get("changes", [changes])
    applied = []
    for change in changes:
        try:
            plan = service.apply_change(run, key, change)
        except Exception as exc:  # 修订错误直接报告，不中断其他变更
            print(f"变更失败：{change.get('kind')} → {exc}")
            return 2
        applied.append({"kind": change.get("kind"), "revision": plan.revision})
    out = Path(args.out)
    _write_plan_outputs(out, run, key, service=service)
    if run.comparison:
        export_comparison(out / "comparison.csv", run.comparison)
    write_json(out / f"revision_log_{key}.json", {"applied": applied, "log": run.plan(key).change_log})
    print(f"已应用 {len(applied)} 个变更，方案 {key} 当前修订 {run.plan(key).revision}")
    _print_comparison(run)
    return 0


def cmd_publication(args: argparse.Namespace) -> int:
    service = PlanningService(load_config(args.config))
    run = service.build_run(
        load_bundle(args.data, activities_path=args.activities, budgets_path=args.budgets),
        start=args.start,
        days=args.days,
        as_of=args.as_of,
        scenario_keys=args.scenarios.split(",") if args.scenarios else None,
    )
    key = _resolve_key(run, args.scenario)
    assessment = service.publication_readiness(
        run,
        key,
        budget_confirmed=args.budget_confirmed,
        manager_accepted=args.manager_accepted,
    )
    out = Path(args.out)
    write_json(out / f"publication_{key}.json", assessment)
    print(f"方案 {key} 发布检查：{'可形成承诺' if assessment['commitment_ready'] else '尚不可发布'}")
    for blocker in assessment["blockers"]:
        print(f"  阻断：{blocker}")
    for warning in assessment["warnings"][:10]:
        print(f"  警告：{warning}")
    print(
        f"  已接受承诺 {len(assessment['accepted_commitments'])} 班，"
        f"待员工确认 {len(assessment['pending_employee_confirmations'])} 班"
    )
    print(f"已写出 {out / f'publication_{key}.json'}")
    return 0 if assessment["commitment_ready"] else 3


def cmd_all(args: argparse.Namespace) -> int:
    service = PlanningService(load_config(args.config))
    run = _load_and_run(args, service)
    out = Path(args.out)
    export_forecast(out / "forecast.csv", run.forecast)
    export_requirements(out / "requirements.csv", run.requirements)
    export_comparison(out / "comparison.csv", run.comparison)
    for key in run.plans:
        _write_plan_outputs(out, run, key, service=service)
    review = build_review(run.bundle, service.config, requirements=run.requirements)
    export_review_shifts(out / "review_shifts.csv", review)
    export_review_store_days(out / "review_store_days.csv", review)
    write_json(
        out / "review_summary.json",
        {"summary": review.summary, "assumptions": review.assumptions},
    )
    all_issues = IssueBag()
    all_issues.extend(run.issues)
    all_issues.extend(IssueBag(review.issues))
    export_issues(out / "issues.csv", all_issues)
    write_json(
        out / "run_summary.json",
        {
            "as_of": run.as_of.isoformat() if run.as_of else None,
            "dates": [d.isoformat() for d in run.dates],
            "scenarios": list(run.plans),
            "comparison": run.comparison.rows,
            "forecast_assumptions": run.forecast.assumptions,
            "review_summary": review.summary,
            "review_assumptions": review.assumptions,
            "issue_counts": all_issues.counts,
        },
    )
    _print_comparison(run)
    _print_issues(run.issues, limit=args.limit)
    print(f"全部结果已写出到 {out.resolve()}")
    return 0


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser, *, with_run: bool = True) -> None:
    parser.add_argument("--data", default="data", help="资料目录（默认 data）")
    parser.add_argument("--activities", default=None, help="活动 CSV（默认 <data>/activities.csv）")
    parser.add_argument("--budgets", default=None, help="预算 CSV（默认 <data>/budgets.csv）")
    parser.add_argument("--config", default=None, help="JSON 配置（可选）")
    parser.add_argument("--out", default="output", help="输出目录（默认 output）")
    parser.add_argument("--limit", type=int, default=20, help="控制台最多显示的问题条数")
    if with_run:
        parser.add_argument("--start", type=_parse_date, default=dt.date(2026, 12, 1), help="规划起始日期")
        parser.add_argument("--days", type=int, default=20, help="规划天数")
        parser.add_argument("--as-of", dest="as_of", type=_parse_date, default=None, help="预测起点（默认起始日减一天）")
        parser.add_argument("--scenarios", default=None, help="逗号分隔的方案键，例如 balanced,cost")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="store-labor",
        description="门店客流预测、岗位需求、排班方案比较与回看（本地合成资料）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("validate", help="校验资料并输出问题清单")
    _add_common(p, with_run=False)
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("forecast", help="导出小时预测与岗位需求")
    _add_common(p)
    p.set_defaults(func=cmd_forecast)

    p = sub.add_parser("plan", help="生成并比较排班方案")
    _add_common(p)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("review", help="回看实际覆盖与服务效果")
    _add_common(p)
    p.add_argument(
        "--with-requirements",
        action="store_true",
        help="同时计算规划期岗位需求，用于计划覆盖对比",
    )
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("revise", help="应用 JSON 变更")
    _add_common(p)
    p.add_argument("--scenario", required=True, help="要修订的方案键")
    p.add_argument("--changes", required=True, help="变更 JSON 文件")
    p.set_defaults(func=cmd_revise)

    p = sub.add_parser("publication", help="检查发布前确认与阻断项")
    _add_common(p)
    p.add_argument("--scenario", default="balanced", help="方案键（默认 balanced）")
    p.add_argument("--budget-confirmed", action="store_true", help="区域经理已确认预算")
    p.add_argument("--manager-accepted", action="store_true", help="门店负责人已接受安排")
    p.set_defaults(func=cmd_publication)

    p = sub.add_parser("all", help="一次完成预测、方案、比较、回看与导出")
    _add_common(p)
    p.set_defaults(func=cmd_all)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (DataError, ConfigError) as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
