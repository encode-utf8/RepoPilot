"""Builds the unified assessment report.

The report always separates tool confirmed issues from unverified potential
problems, and it records every verification gap so a partial assessment can never
be mistaken for a clean one.
"""

from __future__ import annotations

from collections import Counter

from repopilot.models import (
    CheckerResult,
    CheckerStatus,
    Issue,
    ProjectProfile,
    RepoMetadata,
    Report,
    ReportSummary,
    TaskStatus,
    VerificationInfo,
    VerificationState,
    dedupe_issues,
    sort_issues,
    utcnow,
)


def _counts(issues: list[Issue], attribute: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for issue in issues:
        value = getattr(issue, attribute)
        key = value.value if hasattr(value, "value") else str(value)
        counter[key] += 1
    return dict(sorted(counter.items()))


def _verification_gaps(checkers: list[CheckerResult]) -> list[str]:
    gaps: list[str] = []
    for checker in checkers:
        if checker.status is CheckerStatus.unavailable:
            detail = checker.error or "; ".join(checker.notes) or "not executed"
            gaps.append(f"{checker.name}: {detail}")
        elif checker.status is CheckerStatus.error:
            gaps.append(f"{checker.name}: checker failed ({checker.error or 'unknown error'})")
        elif checker.required and not checker.ran:
            gaps.append(f"{checker.name}: required checker did not run")
    return gaps


def build_report(
    *,
    task_id: str,
    run_id: str,
    status: TaskStatus,
    repo: RepoMetadata,
    project: ProjectProfile,
    checkers: list[CheckerResult],
    failure_code: str | None = None,
    failure_reason: str | None = None,
    notes: list[str] | None = None,
) -> Report:
    """Assemble a fully populated, self describing report."""

    all_issues = dedupe_issues([issue for checker in checkers for issue in checker.issues])
    confirmed = sort_issues(
        [issue for issue in all_issues if issue.verification is VerificationState.confirmed]
    )
    unverified = sort_issues(
        [issue for issue in all_issues if issue.verification is VerificationState.unverified]
    )

    summary = ReportSummary(
        total_issues=len(all_issues),
        confirmed_issues=len(confirmed),
        unverified_issues=len(unverified),
        by_severity=_counts(confirmed, "severity"),
        by_tool=_counts(confirmed, "source_tool"),
        by_type=_counts(confirmed, "issue_type"),
        unverified_by_severity=_counts(unverified, "severity"),
        unverified_by_tool=_counts(unverified, "source_tool"),
        checkers_ran=[checker.name for checker in checkers if checker.ran],
        checkers_unavailable=[
            checker.name for checker in checkers if checker.status is CheckerStatus.unavailable
        ],
        checkers_not_applicable=[
            checker.name for checker in checkers if checker.status is CheckerStatus.not_applicable
        ],
    )

    gaps = _verification_gaps(checkers)
    unverified_reasons = [
        f"{checker.name} did not run, so its findings are unverified"
        for checker in checkers
        if not checker.ran and checker.status is not CheckerStatus.not_applicable
    ]

    verification = VerificationInfo(
        code_execution_enabled=repo.code_execution_enabled,
        fully_verified=status is TaskStatus.succeeded and not gaps,
        verification_gaps=gaps,
        unverified_reasons=unverified_reasons,
        notes=notes or [],
    )

    return Report(
        task_id=task_id,
        run_id=run_id,
        status=status,
        generated_at=utcnow(),
        repo=repo,
        project=project,
        checkers=checkers,
        confirmed_issues=confirmed,
        unverified_issues=unverified,
        summary=summary,
        verification=verification,
        failure_code=failure_code,
        failure_reason=failure_reason,
        notes=notes or [],
    )
