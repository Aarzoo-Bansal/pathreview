"""Static dependency graph and plan validation for agent tools.

Closes issue #54: `Orchestrator._build_plan()` decides which tools to run
based only on which raw fields are present in `profile_data`, never on
whether a tool's actual prerequisite already ran. This module defines the
declared dependency graph for the 5 agent tools and a `validate_plan()`
step that `Orchestrator.run()` calls before executing anything, so a plan
that violates the graph is rejected up front instead of silently running
tools with missing or empty upstream data.
"""

import structlog

logger = structlog.get_logger()

# Direct prerequisites for each tool, keyed by tool name. A tool with an
# empty set has no prerequisites in this subsystem. `github_tool` stands in
# for "ingestion complete" for `skill_extractor`'s purposes, since ingestion
# itself lives outside `agent/tools/` and is out of scope for this fix.
TOOL_DEPENDENCIES: dict[str, set[str]] = {
    "github_tool": set(),
    "tech_detector": set(),
    "readme_scorer": set(),
    "skill_extractor": {"github_tool"},
    "market_analyzer": {"tech_detector"},
}


class PlanValidationError(Exception):
    """Raised when a plan violates the tool dependency graph.

    Attributes:
        tool_name: The tool whose prerequisite was missing or out of order.
        missing_prerequisite: The prerequisite tool that was not satisfied.
    """

    def __init__(self, tool_name: str, missing_prerequisite: str) -> None:
        """Initialize the error.

        Args:
            tool_name: The tool whose prerequisite was missing or out of order.
            missing_prerequisite: The prerequisite tool that was not satisfied.
        """
        self.tool_name = tool_name
        self.missing_prerequisite = missing_prerequisite
        super().__init__(
            f"Plan invalid: '{tool_name}' requires '{missing_prerequisite}' to run "
            f"earlier in the plan, but it is missing or scheduled out of order."
        )


def validate_plan(plan: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Validate that every tool's prerequisites appear earlier in the plan.

    Walks the plan in order, tracking which tools have already been
    scheduled. A tool passes validation only if every prerequisite named in
    `TOOL_DEPENDENCIES` was scheduled at an earlier position in the same
    list. Because this is a single linear pass (not a recursive graph
    traversal), a misconfigured cyclical entry in `TOOL_DEPENDENCIES` cannot
    cause an infinite loop -- it simply fails validation the first time the
    cycle is encountered.

    Args:
        plan: Ordered list of (tool_name, tool_input) tuples produced by
            `Orchestrator._build_plan()`.

    Returns:
        The same plan, unchanged, if every tool's prerequisites are
        satisfied by tools scheduled earlier in the list. An empty plan is
        a no-op and is returned as-is.

    Raises:
        PlanValidationError: If a tool's prerequisite is missing from the
            plan entirely, or present but scheduled after the tool that
            depends on it.
    """
    if not plan:
        return plan

    scheduled: set[str] = set()
    for tool_name, _ in plan:
        prerequisites = TOOL_DEPENDENCIES.get(tool_name)

        if prerequisites is None:
            logger.warning("tool_dependencies_unknown_tool", tool=tool_name)
            prerequisites = set()

        missing = prerequisites - scheduled
        if missing:
            raise PlanValidationError(tool_name, sorted(missing)[0])

        scheduled.add(tool_name)

    return plan
