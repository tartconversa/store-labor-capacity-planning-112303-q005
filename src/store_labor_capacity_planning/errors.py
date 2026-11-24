"""结构化问题与严重级别。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Iterator


class Severity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def label(self) -> str:
        return {"error": "错误", "warning": "警告", "info": "提示"}[self.value]

    @property
    def rank(self) -> int:
        return {"error": 0, "warning": 1, "info": 2}[self.value]


@dataclass(frozen=True)
class Issue:
    """一条可定位的数据或业务问题。

    ``entity`` 用于携带主要业务标识（门店、员工、班次等），便于下游分组。
    ``blocks_publication`` 表示该问题是否阻止计划发布；数据质量提示通常为 False。
    """

    severity: Severity
    code: str
    message: str
    source: str | None = None
    row: int | None = None
    entity: str | None = None
    blocks_publication: bool = False

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "severity_label": self.severity.label,
            "code": self.code,
            "message": self.message,
            "source": self.source,
            "row": self.row,
            "entity": self.entity,
            "blocks_publication": self.blocks_publication,
        }


@dataclass
class IssueBag:
    items: list[Issue] = field(default_factory=list)

    def add(
        self,
        severity: Severity,
        code: str,
        message: str,
        *,
        source: str | None = None,
        row: int | None = None,
        entity: str | None = None,
        blocks_publication: bool = False,
    ) -> None:
        self.items.append(
            Issue(
                severity=severity,
                code=code,
                message=message,
                source=source,
                row=row,
                entity=entity,
                blocks_publication=blocks_publication,
            )
        )

    def error(self, code: str, message: str, **kwargs) -> None:
        self.add(Severity.ERROR, code, message, **kwargs)

    def warning(self, code: str, message: str, **kwargs) -> None:
        self.add(Severity.WARNING, code, message, **kwargs)

    def info(self, code: str, message: str, **kwargs) -> None:
        self.add(Severity.INFO, code, message, **kwargs)

    def extend(self, other: "IssueBag") -> None:
        self.items.extend(other.items)

    def __iter__(self) -> Iterator[Issue]:
        return iter(self.items)

    def __len__(self) -> int:
        return len(self.items)

    def by_severity(self, severity: Severity) -> list[Issue]:
        return [i for i in self.items if i.severity is severity]

    @property
    def errors(self) -> list[Issue]:
        return self.by_severity(Severity.ERROR)

    @property
    def warnings(self) -> list[Issue]:
        return self.by_severity(Severity.WARNING)

    @property
    def infos(self) -> list[Issue]:
        return self.by_severity(Severity.INFO)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)

    @property
    def has_blocking(self) -> bool:
        return any(i.blocks_publication for i in self.items)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "error": len(self.errors),
            "warning": len(self.warnings),
            "info": len(self.infos),
            "total": len(self.items),
        }

    def code_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for issue in self.items:
            result[issue.code] = result.get(issue.code, 0) + 1
        return dict(sorted(result.items()))

    def sorted_items(self) -> list[Issue]:
        return sorted(
            self.items,
            key=lambda i: (
                i.severity.rank,
                i.source or "",
                i.row if i.row is not None else 10**9,
                i.code,
            ),
        )

    def to_dict(self) -> dict:
        return {
            "counts": self.counts,
            "code_counts": self.code_counts(),
            "items": [i.to_dict() for i in self.sorted_items()],
        }

    @classmethod
    def merge(cls, bags: Iterable["IssueBag"]) -> "IssueBag":
        merged = cls()
        for bag in bags:
            merged.extend(bag)
        return merged
