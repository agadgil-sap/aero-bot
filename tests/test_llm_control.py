"""Pin the model authority boundary before any live provider is armed."""

import json
from decimal import Decimal

import httpx
import pytest

from aero_bot.llm_control import (
    LLM_API_KEY_ENV,
    LLM_BASE_URL_ENV,
    LLM_MODE_ENV,
    LLM_MODEL_ENV,
    LlmActionDecision,
    LlmActionKind,
    LlmControlConfig,
    LlmControlMode,
    LlmExecutionState,
    LlmInstructionRefusedError,
    OpenAICompatibleDecisionClient,
    validate_llm_action,
)


def state(**updates: object) -> LlmExecutionState:
    """Build one reconciled-state fixture."""
    base: dict[str, object] = {
        "candidate_symbols": ("AAPLc", "MSTRc"),
        "tracked_symbol": None,
        "tracked_token_id": None,
        "tracked_staked": False,
        "held_inventory_symbol": None,
        "safe_usdc": Decimal("100"),
    }
    base.update(updates)
    return LlmExecutionState.model_validate(base)


def decision(action: LlmActionKind, **updates: object) -> LlmActionDecision:
    """Build one model-decision fixture."""
    base: dict[str, object] = {"action": action, "rationale": "fixture rationale"}
    base.update(updates)
    return LlmActionDecision.model_validate(base)


def test_configuration_is_dark_by_default() -> None:
    """LLM authority cannot activate accidentally."""
    config = LlmControlConfig.from_environment({})
    assert config.mode is LlmControlMode.OFF
    assert config.api_key is None


def test_enabled_configuration_names_missing_sealed_variables() -> None:
    """Enabled modes fail closed when provider settings are absent."""
    with pytest.raises(ValueError, match=LLM_BASE_URL_ENV):
        LlmControlConfig.from_environment({LLM_MODE_ENV: "shadow"})


def test_live_configuration_parses_provider_without_echoing_key() -> None:
    """A complete live provider config parses without exposing its credential."""
    config = LlmControlConfig.from_environment(
        {
            LLM_MODE_ENV: "live",
            LLM_BASE_URL_ENV: "https://llm.example/v1",
            LLM_API_KEY_ENV: "super-secret",
            LLM_MODEL_ENV: "glm-example",
        }
    )
    assert config.mode is LlmControlMode.LIVE
    assert config.model == "glm-example"


def test_enter_requires_verified_symbol_budget_width_and_available_cash() -> None:
    """Entries are board-bound, sized, ranged, and cash-backed."""
    valid = decision(
        LlmActionKind.ENTER,
        symbol="AAPLc",
        budget_usdc=Decimal("80"),
        width_spacings=12,
    )
    assert validate_llm_action(valid, state()).decision == valid
    with pytest.raises(LlmInstructionRefusedError, match="candidate board"):
        validate_llm_action(valid.model_copy(update={"symbol": "FAKE"}), state())
    with pytest.raises(LlmInstructionRefusedError, match="exceeds reconciled Safe USDC"):
        validate_llm_action(
            valid.model_copy(update={"budget_usdc": Decimal("90")}), state(safe_usdc=Decimal("50"))
        )


def test_canary_budget_cap_is_absolute() -> None:
    """The initial one-hundred-dollar cap cannot be exceeded by the model."""
    request = decision(
        LlmActionKind.ENTER,
        symbol="AAPLc",
        budget_usdc=Decimal("100.01"),
        width_spacings=12,
    )
    with pytest.raises(LlmInstructionRefusedError, match="canary cap"):
        validate_llm_action(request, state(safe_usdc=Decimal("200")))


def test_tracked_actions_are_bound_to_reconciled_symbol_and_token() -> None:
    """Position actions cannot target invented or stale NFT identities."""
    tracked = state(tracked_symbol="AAPLc", tracked_token_id=77, tracked_staked=True)
    request = decision(LlmActionKind.UNSTAKE, symbol="AAPLc", token_id=77)
    assert validate_llm_action(request, tracked).decision == request
    with pytest.raises(LlmInstructionRefusedError, match="tracked token id"):
        validate_llm_action(request.model_copy(update={"token_id": 78}), tracked)
    with pytest.raises(LlmInstructionRefusedError, match="already staked"):
        validate_llm_action(decision(LlmActionKind.STAKE, symbol="AAPLc", token_id=77), tracked)


def test_switch_requires_a_real_different_board_target() -> None:
    """Cross-pool switches stay inside the verified board."""
    tracked = state(tracked_symbol="AAPLc", tracked_token_id=77, tracked_staked=True)
    request = decision(
        LlmActionKind.SWITCH_POOL,
        target_symbol="MSTRc",
        budget_usdc=Decimal("80"),
        width_spacings=10,
    )
    assert validate_llm_action(request, tracked).decision == request
    with pytest.raises(LlmInstructionRefusedError, match="must differ"):
        validate_llm_action(request.model_copy(update={"target_symbol": "AAPLc"}), tracked)


def test_exit_swap_requires_explicit_held_inventory() -> None:
    """A model cannot sell stock merely because an LP position exists."""
    request = decision(LlmActionKind.EXIT_SWAP, symbol="AAPLc")
    with pytest.raises(LlmInstructionRefusedError, match="held stock inventory"):
        validate_llm_action(
            request, state(tracked_symbol="AAPLc", tracked_token_id=77, tracked_staked=False)
        )
    held = state(held_inventory_symbol="AAPLc")
    assert validate_llm_action(request, held).decision == request


def test_exit_position_is_bound_to_the_tracked_nft() -> None:
    """The composite full exit can target only the reconciled tracked NFT."""
    tracked = state(tracked_symbol="AAPLc", tracked_token_id=77, tracked_staked=True)
    request = decision(LlmActionKind.EXIT_POSITION, symbol="AAPLc", token_id=77)
    assert validate_llm_action(request, tracked).decision == request
    with pytest.raises(LlmInstructionRefusedError, match="tracked symbol and token id"):
        validate_llm_action(request.model_copy(update={"token_id": 78}), tracked)


def test_hold_cannot_smuggle_execution_parameters() -> None:
    """A hold is parameter-free and cannot hide a transaction request."""
    with pytest.raises(LlmInstructionRefusedError, match="must not carry"):
        validate_llm_action(decision(LlmActionKind.HOLD, symbol="AAPLc"), state())


def test_openai_compatible_client_sends_no_execution_authority_or_secret_in_prompt() -> None:
    """Provider calls keep the key in headers and return one typed action."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("Authorization")
        payload = json.loads(request.content)
        seen["payload"] = payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"action": "hold", "rationale": "no edge", "symbol": None}
                            )
                        }
                    }
                ]
            },
        )

    config = LlmControlConfig(
        mode=LlmControlMode.SHADOW,
        base_url="https://llm.example/v1",
        api_key="secret-token",
        model="glm-example",
    )
    result = OpenAICompatibleDecisionClient(config, httpx.MockTransport(handler)).decide(
        {"candidate_symbols": ["AAPLc"], "safe_usdc": "100"}
    )
    assert result.action is LlmActionKind.HOLD
    assert seen["authorization"] == "Bearer secret-token"
    encoded = json.dumps(seen["payload"])
    assert "secret-token" not in encoded
    assert "calldata" in encoded  # system contract explicitly forbids model-generated calldata
