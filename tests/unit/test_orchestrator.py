"""Tests for orchestrator.py

Includes the fix for issue #54: the orchestrator now validates a plan
against `agent.tools.tool_dependencies` before executing anything, rejecting
plans where a tool's prerequisites weren't satisfied earlier in the same
plan. See docs/adr/003-agent-orchestration.md and
https://github.com/ascherj/pathreview/issues/54 for background.
"""

import pytest

from agent.orchestrator import Orchestrator
from agent.tools.base import BaseTool, ToolResult


class RecordingTool(BaseTool):
    """Fake tool that records whether it was invoked."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.description = f"Fake {name}"
        self.calls: list[dict] = []

    def execute(self, input_data: dict) -> ToolResult:
        self.calls.append(input_data)
        return ToolResult(success=True, data={"ran": self.name})


@pytest.fixture
def tools() -> dict[str, RecordingTool]:
    names = [
        "github_tool",
        "tech_detector",
        "readme_scorer",
        "skill_extractor",
        "market_analyzer",
    ]
    return {name: RecordingTool(name) for name in names}


@pytest.mark.unit
class TestOrchestratorPlanValidation:
    """Fix for issue #54: prerequisite validation before execution."""

    def test_market_analyzer_plan_rejected_without_tech_detector(
        self, tools: dict[str, RecordingTool]
    ) -> None:
        """market_analyzer's plan is rejected when tech_detector never ran.

        `_build_plan` itself is unchanged: it still appends `market_analyzer`
        unconditionally whenever the plan is non-empty (agent/orchestrator.py),
        regardless of whether `tech_detector` was scheduled -- profile_data
        only has `readme_content`, so `tech_detector` (which requires `files`)
        is never scheduled, yet `market_analyzer` still is.

        What changed: `Orchestrator.run()` now validates that plan against
        `agent.tools.tool_dependencies.TOOL_DEPENDENCIES` before executing
        anything. Since `market_analyzer` declares `tech_detector` as a
        prerequisite and it's absent from the plan, run() rejects the plan
        up front -- market_analyzer never actually executes with the
        hardcoded empty `detected_skills` it used to run with.
        """
        orchestrator = Orchestrator(tools=tools)
        profile_data = {"readme_content": "Some readme text with no files or resume."}

        plan = orchestrator._build_plan(profile_data)
        plan_tools = [name for name, _ in plan]

        assert "tech_detector" not in plan_tools
        assert "market_analyzer" in plan_tools, (
            "_build_plan is expected to still schedule market_analyzer here -- "
            "the fix rejects this plan in run(), it doesn't change _build_plan"
        )

        result = orchestrator.run(profile_id="p1", profile_data=profile_data)

        assert "error" in result
        assert result["tool_results"] == {}
        assert tools["market_analyzer"].calls == []

    def test_skill_extractor_plan_rejected_without_github_ingestion(
        self, tools: dict[str, RecordingTool]
    ) -> None:
        """skill_extractor's plan is rejected when github_tool never ran.

        `_build_plan` still schedules `skill_extractor` from `resume_text`
        alone, with no check that GitHub ingestion (`github_tool`) has
        completed -- profile_data has `resume_text` but no `github_username`,
        so `github_tool` never runs, yet `skill_extractor` is scheduled
        anyway. `Orchestrator.run()` now rejects this plan before execution
        instead of letting skill_extractor silently fall back to an empty
        `repo_metadata`.
        """
        orchestrator = Orchestrator(tools=tools)
        profile_data = {"resume_text": "Experienced Python developer."}

        plan = orchestrator._build_plan(profile_data)
        plan_tools = [name for name, _ in plan]

        assert "github_tool" not in plan_tools
        assert "skill_extractor" in plan_tools

        result = orchestrator.run(profile_id="p2", profile_data=profile_data)

        assert "error" in result
        assert result["tool_results"] == {}
        assert tools["skill_extractor"].calls == []

    def test_valid_plan_still_executes_normally(self, tools: dict[str, RecordingTool]) -> None:
        """A plan whose prerequisites are all satisfied in order still runs.

        Guards against the validation step being overly strict: github_username
        and a github_repo produce github_tool, files produce tech_detector,
        so skill_extractor and market_analyzer's prerequisites are both met.
        """
        orchestrator = Orchestrator(tools=tools)
        profile_data = {
            "github_username": "octocat",
            "projects": [{"github_repo": "octocat/hello-world"}],
            "files": ["main.py"],
            "resume_text": "Experienced Python developer.",
        }

        result = orchestrator.run(profile_id="p3", profile_data=profile_data)

        assert "error" not in result
        assert tools["github_tool"].calls
        assert tools["tech_detector"].calls
        assert tools["skill_extractor"].calls
        assert tools["market_analyzer"].calls

    def test_downstream_tool_skipped_when_prerequisite_fails_at_runtime(
        self, tools: dict[str, RecordingTool]
    ) -> None:
        """A prerequisite can be correctly *planned* but still fail at
        execution time; the dependent tool should be skipped, not run on a
        failed/empty upstream result.
        """

        class FailingTool(BaseTool):
            name = "tech_detector"
            description = "Fails on purpose"

            def execute(self, input_data: dict) -> ToolResult:
                raise RuntimeError("boom")

        tools["tech_detector"] = FailingTool()  # type: ignore[assignment]
        orchestrator = Orchestrator(tools=tools)
        profile_data = {"files": ["main.py"], "readme_content": "some readme"}

        result = orchestrator.run(profile_id="p4", profile_data=profile_data)

        assert result["tool_results"]["tech_detector"]["success"] is False
        assert result["tool_results"]["market_analyzer"]["success"] is False
        assert tools["market_analyzer"].calls == []
