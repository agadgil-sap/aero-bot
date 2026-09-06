"""Behavior tests for wallet-free unsigned planning and simulation."""

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from aero_bot.transactions import (
    UINT256_MAX,
    ExactAllowanceRequest,
    PlanStatus,
    SimulationBatch,
    SimulationObservation,
    SimulationStatus,
    SimulationUnavailableError,
    TransactionAction,
    TransactionPlanner,
    TransactionPolicy,
    TransactionSimulationBackend,
)

# Fixture owner is a public simulation identity and has no associated private key.
OWNER_ADDRESS = "0x1111111111111111111111111111111111111111"
# Fixture token matches a verified B20 contract without claiming live allowance state.
TOKEN_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"  # noqa: S105
# Fixture spender represents a reviewed-contract-shaped address for policy tests only.
SPENDER_ADDRESS = "0x2222222222222222222222222222222222222222"
# Fixed Base block makes the unsigned plan identifier deterministic.
BLOCK_NUMBER = 35_000_000


def enabled_planner(
    simulation_backend: TransactionSimulationBackend | None = None,
) -> TransactionPlanner:
    """Build a planner with explicit fixture allowlists and emergency halt disabled.

    Args:
        simulation_backend: Optional protocol-compatible read-only backend fixture.

    Returns:
        A planner enabled only for the fixture token and spender.
    """
    # Explicit policy prevents tests from weakening the safe production default.
    policy = TransactionPolicy(
        emergency_halt=False,
        allowed_token_addresses=frozenset({TOKEN_ADDRESS}),
        allowed_spender_addresses=frozenset({SPENDER_ADDRESS}),
    )
    return TransactionPlanner(policy, simulation_backend)


def allowance_request(**overrides: object) -> ExactAllowanceRequest:
    """Build a deterministic exact-allowance request.

    Args:
        **overrides: Fields changed to exercise one planner outcome.

    Returns:
        A validated immutable allowance request.
    """
    # Baseline evidence requests a positive allowance from a zero current state.
    values: dict[str, object] = {
        "owner_address": OWNER_ADDRESS,
        "token_address": TOKEN_ADDRESS,
        "spender_address": SPENDER_ADDRESS,
        "amount_raw": 1_250_000,
        "current_allowance_raw": 0,
        "block_number": BLOCK_NUMBER,
    }
    values.update(overrides)
    return ExactAllowanceRequest.model_validate(values)


def passing_batch(transaction_count: int = 1) -> SimulationBatch:
    """Build complete successful read-only observations for a planned sequence.

    Args:
        transaction_count: Number of ordered transaction results required.

    Returns:
        A source-stamped deterministic simulation batch.
    """
    # Each observation corresponds to exactly one plan index in order.
    observations = tuple(
        SimulationObservation(
            transaction_index=index,
            success=True,
            gas_used=45_000,
            return_data="0x",
            revert_reason=None,
        )
        for index in range(transaction_count)
    )
    return SimulationBatch(
        source="fixture:block-35000000",
        block_number=BLOCK_NUMBER,
        observed_at=datetime(2026, 9, 6, 11, 0, tzinfo=UTC),
        observations=observations,
    )


def test_safe_defaults_block_planning_and_expose_wallet_free_capabilities() -> None:
    """Unconfigured production defaults cannot create a transaction plan."""
    # Default planner has emergency halt enabled and no contract allowlists.
    planner = TransactionPlanner()

    assert planner.plan_exact_allowance(allowance_request()).status is PlanStatus.BLOCKED
    assert planner.capabilities().private_key_input_available is False
    assert planner.capabilities().signing_available is False
    assert planner.capabilities().broadcast_available is False
    assert planner.capabilities().wallet_onboarding_available is False
    assert planner.capabilities().simulation_backend_configured is False


def test_zero_current_allowance_produces_one_exact_unsigned_transaction() -> None:
    """A zero allowance needs one canonical approval for the requested amount."""
    # Enabled fixture policy permits only the expected token and spender.
    result = enabled_planner().plan_exact_allowance(allowance_request())

    assert result.status is PlanStatus.READY
    assert result.plan is not None
    assert len(result.plan.transactions) == 1
    assert result.plan.transactions[0].action is TransactionAction.SET_ALLOWANCE
    assert result.plan.transactions[0].value_wei == 0
    assert result.plan.transactions[0].data.startswith("0x095ea7b3")
    assert result.plan.transactions[0].data.endswith(format(1_250_000, "064x"))
    assert result.plan.signing_available is False
    assert result.plan.broadcast_available is False


def test_nonzero_allowance_uses_zero_reset_before_exact_replacement() -> None:
    """Changing a non-zero allowance produces a deterministic two-step sequence."""
    # Non-zero current evidence triggers the race-resistant zero-reset branch.
    result = enabled_planner().plan_exact_allowance(allowance_request(current_allowance_raw=99))

    assert result.plan is not None
    assert [transaction.action for transaction in result.plan.transactions] == [
        TransactionAction.REVOKE_ALLOWANCE,
        TransactionAction.SET_ALLOWANCE,
    ]
    assert result.plan.transactions[0].data.endswith("0" * 64)


def test_matching_exact_allowance_is_a_first_class_no_action_outcome() -> None:
    """An already exact allowance creates no redundant unsigned transaction."""
    # Equal current and requested values require no state-changing intent.
    result = enabled_planner().plan_exact_allowance(
        allowance_request(current_allowance_raw=1_250_000)
    )

    assert result.status is PlanStatus.NO_ACTION
    assert result.plan is None


def test_unlimited_allowance_request_is_rejected_at_input_boundary() -> None:
    """The ERC-20 maximum cannot be represented as an exact allowance request."""
    # Maximum uint256 conventionally represents an unlimited approval and is prohibited.
    with pytest.raises(ValidationError, match="less than"):
        allowance_request(amount_raw=UINT256_MAX)


def test_nonallowlisted_token_or_spender_blocks_complete_plan() -> None:
    """Either contract boundary failure prevents every transaction payload."""
    # Unrecognized contract addresses model caller attempts outside reviewed targets.
    other_address = "0x3333333333333333333333333333333333333333"
    # Both results must remain payload-free rather than returning partial plans.
    token_result = enabled_planner().plan_exact_allowance(
        allowance_request(token_address=other_address)
    )
    spender_result = enabled_planner().plan_exact_allowance(
        allowance_request(spender_address=other_address)
    )

    assert token_result.status is PlanStatus.BLOCKED
    assert token_result.plan is None
    assert spender_result.status is PlanStatus.BLOCKED
    assert spender_result.plan is None


def test_plan_identifier_is_deterministic_and_changes_with_content() -> None:
    """Identical plan inputs share an ID while an amount change produces a new ID."""
    # One planner ensures policy remains identical across all comparisons.
    planner = enabled_planner()
    # Repeated identical inputs should serialize into the same canonical digest.
    first = planner.plan_exact_allowance(allowance_request())
    second = planner.plan_exact_allowance(allowance_request())
    # Changed amount must be detectable in the integrity identifier.
    changed = planner.plan_exact_allowance(allowance_request(amount_raw=1_250_001))

    assert first.plan is not None
    assert second.plan is not None
    assert changed.plan is not None
    assert first.plan.plan_id == second.plan.plan_id
    assert first.plan.plan_id != changed.plan.plan_id


def test_missing_backend_returns_unavailable_without_external_calls() -> None:
    """A valid plan remains unsigned and reports absent read-only simulation capability."""
    # Enabled policy produces a valid plan while leaving the backend intentionally absent.
    planner = enabled_planner()
    # Ready plan is required before entering simulation revalidation.
    planned = planner.plan_exact_allowance(allowance_request())
    assert planned.plan is not None

    result = planner.simulate(planned.plan)

    assert result.status is SimulationStatus.UNAVAILABLE
    assert result.observations == ()
    assert "no calls were made" in result.diagnostics[0]


def test_complete_read_only_backend_result_passes_without_enabling_execution() -> None:
    """Successful eth_call evidence remains explicitly non-signing and non-broadcasting."""
    # Protocol-shaped mock returns one observation for the one-transaction plan.
    backend = Mock()
    backend.simulate.return_value = passing_batch()
    # Planner and backend share the same strict allowlist policy.
    planner = enabled_planner(backend)
    # Ready plan provides the immutable input to the simulation boundary.
    planned = planner.plan_exact_allowance(allowance_request())
    assert planned.plan is not None

    result = planner.simulate(planned.plan)

    assert result.status is SimulationStatus.PASSED
    assert len(result.observations) == 1
    assert "signing and broadcasting remain unavailable" in result.diagnostics[0]
    backend.simulate.assert_called_once_with(planned.plan)


def test_revert_or_incomplete_backend_evidence_blocks_complete_plan() -> None:
    """A revert or missing observation can never become partial simulation success."""
    # Revert batch preserves complete evidence with one failed EVM call.
    reverted_batch = passing_batch().model_copy(
        update={
            "observations": (
                SimulationObservation(
                    transaction_index=0,
                    success=False,
                    gas_used=30_000,
                    return_data="0x00",
                    revert_reason="fixture revert",
                ),
            )
        }
    )
    # Two calls return the distinct failure shapes under test.
    backend = Mock()
    backend.simulate.side_effect = [
        reverted_batch,
        passing_batch().model_copy(update={"observations": ()}),
    ]
    # One immutable plan is simulated against both backend outcomes.
    planner = enabled_planner(backend)
    planned = planner.plan_exact_allowance(allowance_request())
    assert planned.plan is not None

    reverted = planner.simulate(planned.plan)
    incomplete = planner.simulate(planned.plan)

    assert reverted.status is SimulationStatus.REVERTED
    assert incomplete.status is SimulationStatus.REJECTED
    assert incomplete.observations == ()


def test_backend_availability_error_becomes_explicit_diagnostic() -> None:
    """A read-only backend outage returns unavailable rather than an execution fallback."""
    # Explicit backend error models an RPC timeout without hiding programming exceptions.
    backend = Mock()
    backend.simulate.side_effect = SimulationUnavailableError("RPC timed out")
    # Enabled policy allows the fixture plan to reach the backend boundary.
    planner = enabled_planner(backend)
    planned = planner.plan_exact_allowance(allowance_request())
    assert planned.plan is not None

    result = planner.simulate(planned.plan)

    assert result.status is SimulationStatus.UNAVAILABLE
    assert "RPC timed out" in result.diagnostics[0]


def test_caller_tampering_is_blocked_before_simulation_backend() -> None:
    """Changed action sequence and stale integrity ID cannot reach eth_call."""
    # Backend mock must remain untouched when plan revalidation finds tampering.
    backend = Mock()
    # Enabled policy first creates a canonical one-transaction plan.
    planner = enabled_planner(backend)
    planned = planner.plan_exact_allowance(allowance_request())
    assert planned.plan is not None
    # Replacing set with revoke models caller mutation after the plan ID was calculated.
    tampered_transaction = planned.plan.transactions[0].model_copy(
        update={"action": TransactionAction.REVOKE_ALLOWANCE}
    )
    # Model copy simulates an already parsed untrusted plan arriving at the service boundary.
    tampered_plan = planned.plan.model_copy(update={"transactions": (tampered_transaction,)})

    result = planner.simulate(tampered_plan)

    assert result.status is SimulationStatus.BLOCKED
    assert any("identifier" in diagnostic for diagnostic in result.diagnostics)
    assert any("canonical" in diagnostic for diagnostic in result.diagnostics)
    backend.simulate.assert_not_called()
