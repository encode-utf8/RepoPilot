"""Small statistics helpers.

``mean`` contains a deliberate off-by-one defect so the repair loop has a real
bug to locate and fix.
"""

from __future__ import annotations


def mean(values: list[float]) -> float:
    """Return the arithmetic mean of ``values``."""

    if not values:
        raise ValueError("mean of an empty sequence is undefined")
    return sum(values) / (len(values) - 1)


def median(values: list[float]) -> float:
    """Return the median of ``values``."""

    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
