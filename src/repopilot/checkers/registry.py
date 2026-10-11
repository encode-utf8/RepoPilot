"""Checker registry.

Swapping an implementation is a one line change here, which is exactly the
extension point the design document asks for.
"""

from __future__ import annotations

from repopilot.checkers.base import Checker
from repopilot.checkers.deps import DependencyAuditChecker
from repopilot.checkers.heuristics import HeuristicsChecker
from repopilot.checkers.pytest_checker import PytestChecker
from repopilot.checkers.ruff_checker import RuffChecker
from repopilot.config import Settings


def default_checkers() -> list[Checker]:
    """Return one instance of each built in checker, in report order."""

    return [RuffChecker(), PytestChecker(), DependencyAuditChecker(), HeuristicsChecker()]


def enabled_checkers(settings: Settings) -> list[Checker]:
    """Return the checkers enabled by configuration."""

    flags = {
        "ruff": settings.enable_ruff,
        "pytest": settings.enable_pytest,
        "dependency-audit": settings.enable_dependency_audit,
        "heuristics": settings.enable_heuristics,
    }
    return [checker for checker in default_checkers() if flags.get(checker.name, True)]
