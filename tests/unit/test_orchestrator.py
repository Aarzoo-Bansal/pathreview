"""Tests for orchestrator.py

Includes a reproduction of issue #54: the orchestrator builds and executes
a plan without ever validating that a tool's prerequisites were satisfied
earlier in that same plan. See docs/adr/003-agent-orchestration.md and
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
    """Reproduction for issue #54: no prerequisite validation before execution."""

    def test_market_analyzer_runs_without_tech_detector(
        self, tools: dict[str, RecordingTool]
    ) -> None:
        """market_analyzer is scheduled even when tech_detector never runs.

        Reproduction: profile_data only has `readme_content`, so `_build_plan`
        never schedules `tech_detector` (it requires `files`). But
        `market_analyzer` is appended unconditionally whenever the plan is
        non-empty (agent/orchestrator.py:127), so it runs anyway -- with
        `detected_skills` hardcoded to `{}` (agent/orchestrator.py:130) since
        tech_detector's output was never produced.

        This currently PASSES, which is the bug: nothing rejects or reorders
        a plan where market_analyzer's declared prerequisite (tech_detector)
        is absent. Once a DAG-based validation step (agent/tools/tool_dependencies.py)
        is added, this plan should instead be rejected before execution, and
        this test should be updated to assert that rejection.
        """
        orchestrator = Orchestrator(tools=tools)
        profile_data = {"readme_content": "Some readme text with no files or resume."}

        plan = orchestrator._build_plan(profile_data)
        plan_tools = [name for name, _ in plan]

        assert "tech_detector" not in plan_tools
        assert "market_analyzer" in plan_tools, (
            "reproduction assumption violated: market_analyzer is expected to "
            "be scheduled here despite its prerequisite (tech_detector) being absent"
        )

    def test_skill_extractor_runs_without_github_ingestion(
        self, tools: dict[str, RecordingTool]
    ) -> None:
        """skill_extractor is scheduled from resume_text alone, with no check
        that GitHub ingestion (github_tool) has completed.

        Reproduction: profile_data has `resume_text` but no `github_username`,
        so `github_tool` never runs, yet `skill_extractor` is scheduled anyway
        (agent/orchestrator.py:117-124) and silently falls back to an empty
        `repo_metadata` (agent/orchestrator.py:122).
        """
        orchestrator = Orchestrator(tools=tools)
        profile_data = {"resume_text": "Experienced Python developer."}

        plan = orchestrator._build_plan(profile_data)
        plan_tools = [name for name, _ in plan]

        assert "github_tool" not in plan_tools
        assert "skill_extractor" in plan_tools
