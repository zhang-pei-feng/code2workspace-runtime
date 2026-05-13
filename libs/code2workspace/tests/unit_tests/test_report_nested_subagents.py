from langchain_core.messages import HumanMessage

from code2workspace.middleware.subagents import check_nested_delegation_allowed


def test_nested_delegation_rejected_without_scope_guard() -> None:
    error = check_nested_delegation_allowed(
        state={"messages": [HumanMessage(content="unrelated task")]},
        scope_guard="report",
        max_delegation_depth=3,
        delegation_call_budget=1,
    )

    assert error is not None
    assert "report" in error


def test_nested_delegation_rejected_at_depth_limit() -> None:
    error = check_nested_delegation_allowed(
        state={
            "messages": [HumanMessage(content="report lane")],
            "_delegation_depth": 3,
            "_delegation_calls": 0,
        },
        scope_guard="report",
        max_delegation_depth=3,
        delegation_call_budget=1,
    )

    assert error is not None
    assert "maximum delegation depth" in error


def test_nested_delegation_rejected_when_budget_used() -> None:
    error = check_nested_delegation_allowed(
        state={
            "messages": [HumanMessage(content="report lane")],
            "_delegation_depth": 1,
            "_delegation_calls": 1,
        },
        scope_guard="report",
        max_delegation_depth=3,
        delegation_call_budget=1,
    )

    assert error is not None
    assert "nested task budget" in error


def test_nested_delegation_allowed_within_limits() -> None:
    error = check_nested_delegation_allowed(
        state={
            "messages": [HumanMessage(content="report lane")],
            "_delegation_depth": 2,
            "_delegation_calls": 0,
        },
        scope_guard="report",
        max_delegation_depth=3,
        delegation_call_budget=1,
    )

    assert error is None
