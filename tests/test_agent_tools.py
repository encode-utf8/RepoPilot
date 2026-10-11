"""Controlled toolset behaviour: confinement, patching, tests and diffing."""

from __future__ import annotations

from pathlib import Path

from repopilot.agent.tools import TOOL_NAMES, WorkspaceTools
from repopilot.config import Settings
from repopilot.fixtures import FixtureRepo
from support import STATS_PATH, prepare_repo, stats_fix_diff


def make_tools(settings: Settings, repo: FixtureRepo) -> WorkspaceTools:
    prepared = prepare_repo(settings, repo.path, trusted=True)
    return WorkspaceTools(
        settings=settings,
        workspace=prepared.workspace,
        sandbox=prepared.sandbox,
        token=prepared.context.token,
        logger=prepared.context.logger,
    )


def test_documented_tool_set_is_exactly_the_five_controlled_tools() -> None:
    assert set(TOOL_NAMES) == {
        "read_file",
        "search_code",
        "apply_patch",
        "run_tests",
        "view_diff",
    }


def test_read_file_returns_numbered_window(settings: Settings, repair_repo: FixtureRepo) -> None:
    tools = make_tools(settings, repair_repo)
    result = tools.dispatch("read_file", {"path": STATS_PATH, "start_line": 10, "end_line": 16})

    assert result.ok
    assert "def mean(values: list[float]) -> float:" in result.output
    assert result.data["start_line"] == 10
    assert result.data["total_lines"] >= 16
    # raw content is provided for prompt building, without the line numbers
    assert "return sum(values) / (len(values) - 1)" in result.data["content"]


def test_read_file_lists_directories(settings: Settings, repair_repo: FixtureRepo) -> None:
    result = make_tools(settings, repair_repo).dispatch("read_file", {"path": "src"})
    assert result.ok
    assert "statskit/" in result.data["entries"]


def test_read_file_rejects_paths_outside_the_workspace(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    tools = make_tools(settings, repair_repo)
    for path in ("../outside.txt", "..\\outside.txt"):
        result = tools.dispatch("read_file", {"path": path})
        assert result.ok is False
        assert "escapes the workspace" in (result.error or "")


def test_read_file_reports_missing_paths(settings: Settings, repair_repo: FixtureRepo) -> None:
    result = make_tools(settings, repair_repo).dispatch("read_file", {"path": "src/nope.py"})
    assert result.ok is False
    assert "does not exist" in (result.error or "")


def test_search_code_finds_matches(settings: Settings, repair_repo: FixtureRepo) -> None:
    result = make_tools(settings, repair_repo).dispatch(
        "search_code", {"pattern": "len\\(values\\)", "glob": "*.py"}
    )
    assert result.ok
    assert result.data["count"] >= 1
    assert all(hit["path"].endswith(".py") for hit in result.data["matches"])
    assert any(hit["path"] == STATS_PATH for hit in result.data["matches"])


def test_search_code_rejects_invalid_regex(settings: Settings, repair_repo: FixtureRepo) -> None:
    result = make_tools(settings, repair_repo).dispatch("search_code", {"pattern": "("})
    assert result.ok is False
    assert "regular expression" in (result.error or "")


def test_apply_patch_applies_the_fix_and_updates_the_file(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    tools = make_tools(settings, repair_repo)
    result = tools.dispatch("apply_patch", {"diff": stats_fix_diff()})

    assert result.ok, result.error
    assert result.data["changed_files"] == [STATS_PATH]
    assert result.data["added_lines"] == 1
    assert result.data["removed_lines"] == 1
    assert result.data["diff_hash"]
    content = (tools.repo_dir / STATS_PATH).read_text(encoding="utf-8")
    assert "return sum(values) / len(values)" in content
    assert "(len(values) - 1)" not in content


def test_apply_patch_rejects_a_broken_patch(settings: Settings, repair_repo: FixtureRepo) -> None:
    tools = make_tools(settings, repair_repo)
    broken = (
        "--- a/src/statskit/stats.py\n+++ b/src/statskit/stats.py\n@@ -1,1 +1,1 @@\n-nope\n+nope\n"
    )
    result = tools.dispatch("apply_patch", {"diff": broken})
    assert result.ok is False
    assert result.error_code == "patch_apply_failed"


def test_apply_patch_rejects_an_empty_patch(settings: Settings, repair_repo: FixtureRepo) -> None:
    result = make_tools(settings, repair_repo).dispatch("apply_patch", {"diff": "   \n"})
    assert result.ok is False
    assert "empty" in (result.error or "")


def test_view_diff_shows_the_applied_change(settings: Settings, repair_repo: FixtureRepo) -> None:
    tools = make_tools(settings, repair_repo)
    tools.dispatch("apply_patch", {"diff": stats_fix_diff()})
    result = tools.dispatch("view_diff", {})

    assert result.ok
    assert STATS_PATH in result.data["diff"]
    assert "-    return sum(values) / (len(values) - 1)" in result.data["diff"]


def test_run_tests_returns_a_structured_snapshot(
    settings: Settings, repair_repo: FixtureRepo
) -> None:
    result = make_tools(settings, repair_repo).dispatch("run_tests", {})

    assert result.ok is False  # the fixture fails on the baseline
    snapshot = result.data["snapshot"]
    assert snapshot["ran"] is True
    assert snapshot["failed"] == 1
    assert snapshot["passed"] == 3


def test_dispatch_rejects_unknown_tools(settings: Settings, repair_repo: FixtureRepo) -> None:
    result = make_tools(settings, repair_repo).dispatch("run_shell", {"command": "rm -rf /"})
    assert result.ok is False
    assert result.error_code == "tool_not_allowed"


def test_dispatch_rejects_invalid_arguments(settings: Settings, repair_repo: FixtureRepo) -> None:
    result = make_tools(settings, repair_repo).dispatch("read_file", {"pth": "src"})
    assert result.ok is False
    assert result.error_code == "invalid_tool_arguments"


def test_tool_invocation_record_is_bounded(settings: Settings, repair_repo: FixtureRepo) -> None:
    tools = make_tools(settings, repair_repo)
    result = tools.dispatch("read_file", {"path": STATS_PATH})
    invocation = result.to_invocation(index=1, round_index=1, arguments={"path": STATS_PATH})
    assert invocation.tool == "read_file"
    assert invocation.round == 1
    assert invocation.output_excerpt
    assert invocation.output_bytes > 0


def test_settings_cap_tool_output(settings: Settings, repair_repo: FixtureRepo) -> None:
    from dataclasses import replace

    small = replace(settings, agent_max_tool_output_bytes=80)
    tools = make_tools(small, repair_repo)
    result = tools.dispatch("read_file", {"path": STATS_PATH})
    assert result.truncated is True
    assert "truncated" in result.output


def test_repo_dir_is_the_workspace_checkout(settings: Settings, repair_repo: FixtureRepo) -> None:
    tools = make_tools(settings, repair_repo)
    assert isinstance(tools.repo_dir, Path)
    assert (tools.repo_dir / STATS_PATH).is_file()
