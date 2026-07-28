## Solution plan

**Issue:** Add a plan validation step that checks tool prerequisites before executing the plan — [#54](https://github.com/ascherj/pathreview/issues/54)

### Understand

**Expected behavior:** Before the orchestrator executes a plan, it should verify that every tool's prerequisites appear earlier in that same plan (and, ideally, that those prerequisite tools actually succeeded). A plan that violates this should be rejected or reordered before any tool runs.

**Actual behavior:** `Orchestrator._build_plan()` (`agent/orchestrator.py:78-134`) decides which tools to schedule based purely on which raw fields exist in `profile_data` (`files`, `readme_content`, `resume_text`, `github_username`). It never checks whether a tool's actual dependency already ran. Two concrete, reproduced examples:

1. `market_analyzer` is appended whenever the plan is non-empty at all (`agent/orchestrator.py:127-131`), regardless of whether `tech_detector` was scheduled. If `profile_data` has `readme_content` but no `files`, the plan is `[readme_scorer, market_analyzer]` — `market_analyzer` runs with no tech data ever produced. Its input is even hardcoded to `{"detected_skills": {}}` (line 130), so it can never actually consume `tech_detector`'s real output regardless of ordering.
2. `skill_extractor` is scheduled whenever `resume_text` is present (`agent/orchestrator.py:117-124`), with no check that GitHub ingestion (`github_tool`) ran or succeeded — it silently falls back to `repo_metadata: {}`.

I reproduced both in `tests/unit/test_orchestrator.py` (`TestOrchestratorPlanValidation`): the tests currently *pass* against today's code, which is itself the bug — they document that the orchestrator happily builds/executes these invalid plans with no rejection or reordering.

**Root cause:** `_build_plan` conflates "raw input data is available" with "the tool's dependency has run," and there is no separate validation step between planning and execution to catch the mismatch — which is exactly the gap issue #54 asks to close.

### Map

- `agent/orchestrator.py` — `_build_plan()` needs to hand its output to a validation step before `run()` starts executing tools in the loop at line 53; `run()` needs to handle a rejected/reordered plan.
- `agent/tools/tool_dependencies.py` (new) — defines the static dependency graph for the 5 tools (`github_tool`, `tech_detector`, `readme_scorer`, `skill_extractor`, `market_analyzer`) and exposes a `validate_plan(plan) -> plan` (or raises) function implementing the DAG check/reorder.
- `tests/unit/test_orchestrator.py` — already has the two reproduction tests; will be updated so they assert the *fixed* behavior (rejection/reorder) instead of documenting the bug.
- `tests/unit/test_tool_dependencies.py` (new) — unit tests for the DAG validator in isolation (valid plan, missing prerequisite, unknown tool, cycle safety).
- `agent/tools/base.py` — read-only reference; no changes expected, but `ToolResult.success` is what "failed upstream result" checks will need to inspect at runtime.

### Plan

1. **Define the dependency graph** in `agent/tools/tool_dependencies.py`: a small `dict[str, set[str]]` mapping each of the 5 tool names to its direct prerequisites (`market_analyzer -> {tech_detector}`, `skill_extractor -> {github_tool}` as the ingestion stand-in — see Risks below), plus a `validate_plan()` function that walks the planned tool order and either raises a `PlanValidationError` naming the missing prerequisite, or returns a topologically-sorted plan.
2. **Decide reject vs. reorder default.** Issue text allows either; I'll default to *reject with a clear error* (simpler, safer, matches the "plan-execute" ADR's emphasis on inspectable/predictable execution — see `docs/adr/003-agent-orchestration.md`) and only add reordering if rejecting turns out to break realistic profile-data combinations during testing.
3. **Wire validation into `Orchestrator.run()`**: call `tool_dependencies.validate_plan(plan)` right after `_build_plan()` (`agent/orchestrator.py:44`) and before the session-state load/execute loop, so nothing executes on an invalid plan.
4. **Handle the "failed upstream result" case at runtime**, not just plan-build time: since a prerequisite tool can be scheduled correctly but still fail during execution (`results[tool_name] = {"error": ..., "success": False}` at line 62), add a runtime check in `_execute_tool` (or the main loop) that skips/short-circuits a downstream tool if its prerequisite's recorded result has `success is False`.
5. **Update tests**: flip the two existing reproduction tests in `test_orchestrator.py` to assert `PlanValidationError` (or the reordered plan) instead of the current bug-documenting assertions, and add `test_tool_dependencies.py` covering the DAG validator directly.

### Inputs & outputs

- **Input:** the `list[tuple[str, dict]]` plan produced by `_build_plan()`, and the static dependency graph in `tool_dependencies.py`.
- **Output:** either (a) the same plan, unchanged, if all prerequisites are satisfied in order; (b) a re-ordered plan satisfying the DAG; or (c) a raised `PlanValidationError` identifying the tool and its unmet prerequisite, caught by `run()` before any tool executes. At runtime, a downstream tool whose prerequisite result has `success: False` is skipped with a recorded `{"error": "prerequisite failed", "success": False}` entry rather than being executed.

### Risks & unknowns

- **"Ingestion" isn't one of the 5 tools.** The issue names `skill_extractor` as depending on "ingestion being complete," but ingestion lives outside `agent/tools/` entirely (see `ingestion/pipeline.py`) and the issue explicitly scopes this fix to the agent/orchestration subsystem, not ingestion. I'm treating `github_tool`'s presence-and-success in the plan as the in-subsystem stand-in for "ingestion complete," but this is an interpretation, not something stated outright in the issue — worth confirming with a mentor before finalizing.
- **Reject vs. reorder** changes orchestrator control flow differently: rejecting means `run()` needs a new failure path/return shape; reordering is transparent to callers but riskier if it silently changes which tool runs when. Need to pick one and be explicit about it in code comments/PR description.
- **The hardcoded `detected_skills: {}` in `_build_plan` (line 130)** is a second, adjacent bug — even with correct ordering, `market_analyzer` never actually receives real skill data today. Fixing *that* wiring (passing `context_manager`'s cached results into downstream tool inputs) may be out of scope for #54 but is worth flagging since it affects whether the fix is meaningfully testable end-to-end.
- **No existing caller instantiates `Orchestrator`** anywhere in the codebase yet (confirmed via grep) — so there's no integration test or API route to validate against; all verification will be at the unit level until the orchestrator is actually wired into `api/`.
- **Cycle safety:** the DAG validator must not infinite-loop if the graph is ever misconfigured with a cycle — needs an explicit cycle check with a bounded traversal.

### Edge cases

- Empty plan (`profile_data` has no usable fields at all) — validation should no-op and return `[]`, not error.
- A plan containing a tool name absent from the dependency graph (e.g. a future tool added without updating `tool_dependencies.py`) — should be treated as "no prerequisites" rather than crashing, but should log a warning so the gap is visible.
- A prerequisite tool present in the plan but *after* its dependent (wrong order, not just missing) — must be caught by the same check, not just a simple "is X anywhere in the plan" presence test.
- A prerequisite tool present and correctly ordered, but its execution result comes back `success: False` — the dependent tool should be skipped at runtime, not just at plan-validation time, since validation only knows about planned order, not actual outcomes.
- Multiple tools sharing the same prerequisite (e.g. if a future tool also depends on `tech_detector`) — the validator should handle fan-out, not just a single linear chain.
