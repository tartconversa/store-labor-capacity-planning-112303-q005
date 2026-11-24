"""门店劳动班次规划与回看应用。

模块只依赖 Python 标准库。主要入口见 :mod:`store_labor_capacity_planning.service`
与 :mod:`store_labor_capacity_planning.cli`。
"""

from .service import PlanningService

__all__ = ["PlanningService"]
__version__ = "0.2.0"
