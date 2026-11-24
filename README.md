# 门店客流预测到劳动班次承诺与服务回看

本应用处理一条完整的门店排班业务链：

**历史客流 → 小时需求预测 → 分岗位工时需求 → 多个排班方案 → 方案比较 → 修订与确认 → 发布检查 → 实际覆盖与服务回看。**

全部记录都是**本地合成数据**，标识与数值不对应真实个人、企业或交易。程序只依赖 Python 3.11–3.14 标准库，不需要网络、数据库或第三方服务。

---

## 1. 业务规则如何落在代码里

| 业务事实 | 应用中的处理 |
| --- | --- |
| 小时客流缺失不等于没有顾客 | 缺失客流保持 `None`，不参与均值、不进入预测特征，也不显示为 0；预测客流由交易和转化率推算。 |
| 客流计数可能只覆盖部分时段 | 只有 `traffic_coverage > 0` 的记录才按覆盖率放大使用；覆盖率为 0 视作"未覆盖"，产生警告而不是 0 客流。 |
| 交易数受收银台开放数量影响 | 销售岗位需求 = 交易量 ÷ 单台小时处理能力，并设最低销售工时。 |
| 只按销售预测排人会让收货/清洁/服务无人承担 | 顾客服务、清洁、收货、食品安全是**独立岗位需求**，每个营业日固定存在，与销售高低无关；高峰时段另有服务岗位下限。 |
| 每家门店的岗位资质、合同工时、可用时段、休息规则不同 | 员工有门店、岗位、技能、合同周工时、区域；可用时段有状态（可用/优先/不可用/未知）和偏好；排班核实休息、连续天数、周工时、重叠。 |
| 已确认班次不能被静默删除 | 导入的 `accepted` 班次在所有方案中原样保留；删除、改派或直接覆盖都会被拒绝，必须走协商替换流程。 |
| 迟到活动变更只影响未确认时段 | 活动/闭店变更会重算预测与需求，回收受影响的**未确认**班次重新生成；受影响的已确认班次保留并记录 `renegotiate`。 |
| 发布前需要三级确认 | 区域经理确认预算、门店负责人接受安排、员工接受通知；三者缺一都不算承诺，发布检查会列出阻断项。 |
| 接受即承诺 | 员工接受前逐项核实岗位、可用窗口、休息、连续天数、周工时与重叠；有硬冲突时接受操作失败。 |
| 实际打卡与工作量用于评估 | 回看区分"有记录/未知"，计算出勤率、服务等待、任务负荷与差异原因，且不把相关当因果。 |

---

## 2. 快速开始

```sh
# 只校验资料，输出问题清单
python3 -m store_labor_capacity_planning validate --data data --out output

# 一次生成预测、三个方案、比较、回看与全部导出
python3 -m store_labor_capacity_planning all --data data --out output

# 运行测试（标准库 unittest）
python3 -m unittest discover -s tests -v
```

未安装为包时，先设置模块路径：

```sh
# Linux / macOS
PYTHONPATH=src python3 -m store_labor_capacity_planning all --out output
# Windows PowerShell
$env:PYTHONPATH="src"; python -m store_labor_capacity_planning all --out output
```

安装后可省略路径前缀：

```sh
python3 -m pip install -e .
store-labor all --data data --out output
```

默认规划期是 `2026-12-01` 起 20 天，对应资料中的班次和可用性日期；预测起点默认为规划首日减一天。

---

## 3. 子命令

| 命令 | 用途 | 主要输出 |
| --- | --- | --- |
| `validate` | 校验五个核心资料 + 可选活动/预算 | `issues.csv` |
| `forecast` | 小时客流/交易预测与岗位需求 | `forecast.csv`、`requirements.csv`、`forecast_summary.json` |
| `plan` | 生成并比较排班方案 | `comparison.csv/json`、`shifts_<方案>.csv`、`coverage_gaps_<方案>.csv`、`plan_<方案>.json` |
| `review` | 实际覆盖、出勤与服务效果回看 | `review_shifts.csv`、`review_store_days.csv`、`review_summary.json` |
| `revise` | 应用 JSON 变更（新增/删除/接受/活动变更） | 修订后的 `shifts_<方案>.csv`、`revision_log_<方案>.json` |
| `publication` | 发布前确认与阻断项 | `publication_<方案>.json` |
| `all` | 以上全部 | 上述所有文件 + `issues.csv`、`run_summary.json` |

常用参数：`--start 2026-12-01`、`--days 20`、`--as-of 2026-11-30`、`--scenarios balanced,cost`、`--config 配置文件.json`、`--limit N`（控制台最多显示多少条问题）。

退出码：`0` 正常；`2` 输入/配置错误；`3` 发布检查未通过（缺确认或预算超限）。

---

## 4. 方案与评分

应用默认生成三个方案，另加一个只读的导入基线：

| 方案 | 策略 | 跨店支援 | 加班上限 | 可用性未知 |
| --- | --- | --- | --- | --- |
| `baseline` 导入基线 | 原样保留导入班次，不自动修正 | — | — | — |
| `balanced` 均衡推荐 | 覆盖关键岗位、控制成本、照顾偏好 | 否 | 合同工时 100% | 允许，标记待确认 |
| `coverage` 覆盖优先 | 尽量消除缺口 | 是（同区域、同岗位） | 合同工时 120% | 允许，标记待确认 |
| `cost` 成本优先 | 只排必要班次，优先短班 | 否 | 合同工时 100% | 不允许 |

评分（0–100）由四部分组成：

* **计划覆盖**（60 分）：`已覆盖需求工时 / 需求工时`；
* **预算**（20 分）：有预算时按区域用量评分，无预算时按相对人力单价评分；
* **偏好**（10 分）与**公平**（10 分）；
* **扣分**：只对方案自己生成的班次违规扣分；导入已确认班次的冲突单独计入 `inherited_conflicts`，因为它只能协商解决，不能由新方案"修掉"。

`committed_coverage` 只统计员工已接受的班次，所以刚生成方案时它通常很低——这正是"方案 ≠ 承诺"的体现。

---

## 5. 变更文件格式

`revise` 读取一个 JSON，`changes` 数组按顺序执行；任一条失败即停止并返回 `2`。示例见 `examples/changes.example.json`。

```json
{
  "changes": [
    {
      "kind": "assign",
      "store_id": "ST-006", "date": "2026-12-05", "role": "service",
      "start_hour": 12, "end_hour": 16, "employee_id": "EMP-005"
    },
    { "kind": "acceptance", "shift_id": "BAL-0001", "state": "accepted", "actor": "employee" },
    {
      "kind": "activity",
      "activity": {
        "activity_id": "ACT-LATE-01", "store_id": "ST-003", "date": "2026-12-05",
        "kind": "closure", "start_hour": 12, "end_hour": 16,
        "note": "迟到通知：临时闭店检修"
      }
    },
    { "kind": "remove", "shift_id": "CHG-0001" }
  ]
}
```

* `assign`：可带 `shift_id` 表示改派未确认班次；不带则新增。新增/改派都不允许直接写成 `accepted`，也不允许改派已接受班次。
* `acceptance`：`state` 取 `accepted` / `declined` / `proposed`。接受前会核实全部硬约束。
* `activity`：`kind` 取 `promotion` / `closure` / `other`；促销需给 `uplift`（如 `0.25` 表示 +25%），闭店 `uplift` 留空。可用 `dates` 指定重算范围，默认覆盖方案内的所有日期。
* `remove`：只能删除未确认班次；已确认班次必须走协商替换。

---

## 6. 配置

`--config` 指向 JSON，未写的字段使用默认值；拼写错误的字段会被拒绝，避免"配置没生效却没人发现"。完整示例见 `examples/config.example.json`。

```json
{
  "standards": { "transactions_per_register_hour": 40, "customers_per_service_hour": 110 },
  "constraints": { "rest_hours_between_shifts": 11, "max_consecutive_days": 5, "allow_cross_store": false },
  "cost": { "region_rates": { "R-S": 32, "R-W": 30, "R-N": 31 }, "overtime_multiplier": 1.5 },
  "scheduler": { "min_coverage_target": 0.9 }
}
```

配置区段：`forecast`、`standards`、`constraints`、`cost`、`scheduler`、`review`。金额是合成演示币值，仅用于方案间比较。

---

## 7. 资料契约与问题分级

* `docs/资料契约.md`：五个核心文件及可选活动/预算文件的字段、记录数和关联关系。
* 问题分三级：**错误**（数据无法解释或引用断裂，可能阻断发布）、**警告**（可继续但需人工确认，例如跨店指派、覆盖率为 0 的客流）、**提示**（普通状态说明，例如历史活动编号只作为标签）。
* 所有问题包含 `code`、中文说明、来源文件、行号、关联实体，可用 `code` 做稳定筛选。

### 合成资料中的已知情况

这些是刻意保留的业务难点，程序会逐项报告，而不是静默修正：

* `ST-000` 有历史需求但没有员工；`ST-018` 有员工但没有历史需求（预测使用全局均值并降低置信度）。
* 客流有 13 行缺失、45 行覆盖率为 0 但仍有计数。
* 72 条导入班次中，全部由跨店员工承担，岗位与员工岗位全部不一致；24 条已确认班次还存在超出可用窗口的问题。
* 12 个班次没有实际覆盖记录，回看中标记为"未知"，不是 0 分钟。
* 实际覆盖率普遍偏低、服务等待接近目标上限、差异原因中缺勤/需求偏移/到货延迟各占三分之一——这些用于演示回看与解释，不应直接当作真实结论。

---

## 8. 目录

```
src/store_labor_capacity_planning/
  loader.py        读取、清洗、关联校验（缺失/异常不折叠为 0）
  models.py        业务实体与状态语义
  forecast.py      小时客流/交易预测（收缩、区间、活动、闭店）
  requirements.py  分岗位工时需求
  scheduler.py     方案生成、约束核实、覆盖与成本、方案比较
  revision.py      修订、接受流转、迟到活动变更
  publication.py   发布检查与承诺台账
  review.py        实际覆盖与服务回看
  export.py        CSV/JSON 导出
  service.py       服务层编排
  cli.py           中文命令行
tests/             76 个标准库测试
data/              合成资料
examples/          示例变更与配置
output/            默认输出目录（运行后生成）
```
