"""业务枚举与中文标签。"""

from __future__ import annotations

ROLES = ("sales", "service", "receiving", "food")

ROLE_LABELS = {
    "sales": "销售/收银",
    "service": "顾客服务/清洁",
    "receiving": "收货",
    "food": "食品与安全",
}

ROLE_SKILLS = {
    "sales": "register",
    "service": "repair",
    "receiving": "stock",
    "food": "safety",
}

SKILL_LABELS = {
    "register": "收银",
    "repair": "维修",
    "stock": "库存",
    "safety": "食品安全",
}

REGION_LABELS = {
    "R-S": "南区",
    "R-W": "西区",
    "R-N": "北区",
}

KIND_LABELS = {
    "promotion": "促销活动",
    "closure": "临时闭店",
    "other": "其他活动",
}

VARIANCE_LABELS = {
    "absence": "缺勤",
    "demand-shift": "需求偏移",
    "late-delivery": "到货延迟",
    "none": "无差异",
}


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def region_label(region: str | None) -> str:
    if region is None:
        return "未知区域"
    return REGION_LABELS.get(region, region)
