"""Unit tests for agent.tools.tool_dependencies (issue #54).

Covers the DAG-based plan validator in isolation, separately from the
end-to-end Orchestrator.run() behavior exercised in test_orchestrator.py.
"""

from typing import Any

import pytest

from agent.tools.tool_dependencies import (
    TOOL_DEPENDENCIES,
    PlanValidationError,
    validate_plan,
)

Plan = list[tuple[str, dict[str, Any]]]


@pytest.mark.unit
class TestValidatePlan:
    """Tests for validate_plan()."""

    def test_valid_plan_passes_through_unchanged(self) -> None:
        """A plan whose prerequisites are all satisfied earlier is returned as-is."""
        plan: Plan = [
            ("github_tool", {"github_username": "octocat"}),
            ("tech_detector", {"files": ["main.py"]}),
            ("skill_extractor", {"resume_text": "..."}),
            ("market_analyzer", {"detected_skills": {}}),
        ]

        assert validate_plan(plan) == plan

    def test_empty_plan_is_a_noop(self) -> None:
        """An empty plan (no usable profile data at all) should not error."""
        assert validate_plan([]) == []

    def test_missing_prerequisite_raises(self) -> None:
        """market_analyzer with no tech_detector anywhere in the plan is rejected."""
        plan: Plan = [("market_analyzer", {"detected_skills": {}})]

        with pytest.raises(PlanValidationError) as exc_info:
            validate_plan(plan)

        assert exc_info.value.tool_name == "market_analyzer"
        assert exc_info.value.missing_prerequisite == "tech_detector"

    def test_prerequisite_present_but_out_of_order_raises(self) -> None:
        """A prerequisite listed *after* its dependent is still invalid --
        presence alone isn't enough, order matters.
        """
        plan: Plan = [
            ("market_analyzer", {"detected_skills": {}}),
            ("tech_detector", {"files": ["main.py"]}),
        ]

        with pytest.raises(PlanValidationError):
            validate_plan(plan)

    def test_skill_extractor_requires_github_tool(self) -> None:
        """skill_extractor without github_tool having run is rejected."""
        plan: Plan = [("skill_extractor", {"resume_text": "..."})]

        with pytest.raises(PlanValidationError) as exc_info:
            validate_plan(plan)

        assert exc_info.value.tool_name == "skill_extractor"
        assert exc_info.value.missing_prerequisite == "github_tool"

    def test_unknown_tool_treated_as_no_prerequisites(self) -> None:
        """A tool absent from TOOL_DEPENDENCIES (e.g. a future tool added
        without updating the graph) shouldn't crash -- it's treated as
        having no prerequisites.
        """
        plan: Plan = [("some_future_tool", {})]

        assert validate_plan(plan) == plan

    def test_fan_out_multiple_dependents_share_prerequisite(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Multiple tools depending on the same prerequisite are all satisfied
        once that prerequisite is scheduled once, earlier in the plan.
        """
        monkeypatch.setitem(TOOL_DEPENDENCIES, "readme_scorer", {"tech_detector"})
        plan: Plan = [
            ("tech_detector", {"files": ["main.py"]}),
            ("market_analyzer", {"detected_skills": {}}),
            ("readme_scorer", {"readme_content": "..."}),
        ]

        assert validate_plan(plan) == plan

    def test_self_referential_dependency_does_not_hang(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Guards against a misconfigured cyclical graph entry causing an
        infinite loop. validate_plan is a single linear pass, so a tool that
        (misconfigured) depends on itself simply fails validation instead of
        looping.
        """
        monkeypatch.setitem(TOOL_DEPENDENCIES, "tech_detector", {"tech_detector"})

        with pytest.raises(PlanValidationError):
            validate_plan([("tech_detector", {"files": ["main.py"]})])
