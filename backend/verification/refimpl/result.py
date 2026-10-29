"""Expected-result container that separates strict from uncertain records."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Expected:
    """IDs the reference is certain about, plus IDs it cannot decide.

    ``uncertain`` maps report/request ID -> reason (e.g. 'time_ambiguous',
    'radius_boundary'). Those IDs may legitimately be present or absent in the
    observed result, and are reported separately rather than asserted on.
    """

    ids: set[str] = field(default_factory=set)
    uncertain: dict[str, str] = field(default_factory=dict)
    ordered: list[str] | None = None  # retrieval order, when the query defines one

    @property
    def count_range(self) -> tuple[int, int]:
        return len(self.ids), len(self.ids) + len(self.uncertain)

    def is_exact(self) -> bool:
        return not self.uncertain


def merge_reasons(*parts: tuple[object, str]) -> str:
    """Join reasons for which the flag is truthy."""
    return ",".join(reason for flag, reason in parts if flag)
