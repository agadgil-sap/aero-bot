"""Provider-neutral LLM action authority over audited Aero Bot primitives.

The model never emits calldata, contract addresses, or signatures. It chooses a
small typed instruction that is validated against reconciled state and canary
caps before the existing audited executor is allowed to build anything.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

LLM_MODE_ENV = "AERO_BOT_LLM_MODE"
LLM_BASE_URL_ENV = "AERO_BOT_LLM_BASE_URL"
LLM_API_KEY_ENV = "AERO_BOT_LLM_API_KEY"
LLM_MODEL_ENV = "AERO_BOT_LLM_MODEL"
LLM_TIMEOUT_ENV = "AERO_BOT_LLM_TIMEOUT_SECONDS"
DEFAULT_LLM_TIMEOUT_SECONDS = 45.0
DEFAULT_CANARY_ACTION_BUDGET_USDC = Decimal("100")


class LlmControlMode(StrEnum):
    """Control whether model decisions are disabled, observed, or executable."""

    OFF = "off"
    SHADOW = "shadow"
    LIVE = "live"


class LlmActionKind(StrEnum):
    """Every high-level primitive the model may request."""

    HOLD = "hold"
    ENTER = "enter"
    RECENTER = "recenter"
    SWITCH_POOL = "switch_pool"
    STAKE = "stake"
    UNSTAKE = "unstake"
    WITHDRAW = "withdraw"
    COLLECT = "collect"
    EXIT_SWAP = "exit_swap"
    EXIT_POSITION = "exit_position"


class LlmControlConfig(BaseModel):
    """Sealed provider configuration; credentials never enter audit payloads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: LlmControlMode = LlmControlMode.OFF
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: Annotated[float, Field(gt=0, le=120)] = DEFAULT_LLM_TIMEOUT_SECONDS
    max_action_budget_usdc: Annotated[Decimal, Field(gt=0)] = DEFAULT_CANARY_ACTION_BUDGET_USDC

    @model_validator(mode="after")
    def _require_provider_when_enabled(self) -> LlmControlConfig:
        if self.mode is not LlmControlMode.OFF:
            missing = [
                name
                for name, value in (
                    (LLM_BASE_URL_ENV, self.base_url),
                    (LLM_API_KEY_ENV, self.api_key),
                    (LLM_MODEL_ENV, self.model),
                )
                if not value
            ]
            if missing:
                raise ValueError(f"LLM control requires {', '.join(missing)}")
        return self

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> LlmControlConfig:
        """Parse the dark-by-default sealed environment configuration."""
        env = os.environ if environment is None else environment
        raw_mode = env.get(LLM_MODE_ENV, LlmControlMode.OFF.value).strip().lower()
        try:
            mode = LlmControlMode(raw_mode)
        except ValueError as error:
            choices = ", ".join(item.value for item in LlmControlMode)
            raise ValueError(f"{LLM_MODE_ENV} must be one of: {choices}") from error
        raw_timeout = env.get(LLM_TIMEOUT_ENV, str(DEFAULT_LLM_TIMEOUT_SECONDS))
        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise ValueError(f"{LLM_TIMEOUT_ENV} must be a number") from error
        return cls(
            mode=mode,
            base_url=env.get(LLM_BASE_URL_ENV) or None,
            api_key=env.get(LLM_API_KEY_ENV) or None,
            model=env.get(LLM_MODEL_ENV) or None,
            timeout_seconds=timeout,
        )


class LlmExecutionState(BaseModel):
    """Minimal reconciled state needed to validate a model instruction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_symbols: tuple[str, ...]
    tracked_symbol: str | None = None
    tracked_token_id: Annotated[int, Field(ge=0)] | None = None
    tracked_staked: bool = False
    held_inventory_symbol: str | None = None
    safe_usdc: Annotated[Decimal, Field(ge=0)]


class LlmActionDecision(BaseModel):
    """One model-selected instruction before deterministic validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: LlmActionKind
    rationale: Annotated[str, Field(min_length=1, max_length=1200)]
    symbol: str | None = None
    target_symbol: str | None = None
    token_id: Annotated[int, Field(ge=0)] | None = None
    budget_usdc: Annotated[Decimal, Field(gt=0)] | None = None
    width_spacings: Annotated[int, Field(gt=0, le=500)] | None = None


class LlmInstructionRefusedError(ValueError):
    """A syntactically valid model instruction violated deterministic authority."""


class ValidatedLlmAction(BaseModel):
    """Instruction proven compatible with reconciled state and canary caps."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: LlmActionDecision


def validate_llm_action(
    decision: LlmActionDecision,
    state: LlmExecutionState,
    *,
    max_action_budget_usdc: Decimal = DEFAULT_CANARY_ACTION_BUDGET_USDC,
) -> ValidatedLlmAction:
    """Fail closed unless the requested primitive is valid for current state."""
    candidate_symbols = set(state.candidate_symbols)
    action = decision.action

    if decision.budget_usdc is not None and decision.budget_usdc > max_action_budget_usdc:
        raise LlmInstructionRefusedError(
            f"budget {decision.budget_usdc} exceeds canary cap {max_action_budget_usdc} USDC"
        )

    if action is LlmActionKind.HOLD:
        if any(
            value is not None
            for value in (
                decision.symbol,
                decision.target_symbol,
                decision.token_id,
                decision.budget_usdc,
                decision.width_spacings,
            )
        ):
            raise LlmInstructionRefusedError("hold must not carry execution parameters")
        return ValidatedLlmAction(decision=decision)

    if action is LlmActionKind.ENTER:
        if state.tracked_token_id is not None:
            raise LlmInstructionRefusedError("enter refused while a position is tracked")
        if decision.symbol not in candidate_symbols:
            raise LlmInstructionRefusedError("enter symbol is not on the verified candidate board")
        if decision.budget_usdc is None or decision.width_spacings is None:
            raise LlmInstructionRefusedError("enter requires budget_usdc and width_spacings")
        if decision.budget_usdc > state.safe_usdc:
            raise LlmInstructionRefusedError("enter budget exceeds reconciled Safe USDC")
        return ValidatedLlmAction(decision=decision)

    if action is LlmActionKind.SWITCH_POOL:
        if state.tracked_symbol is None or state.tracked_token_id is None:
            raise LlmInstructionRefusedError("switch_pool requires a tracked position")
        if decision.target_symbol not in candidate_symbols:
            raise LlmInstructionRefusedError("switch target is not on the verified candidate board")
        if decision.target_symbol == state.tracked_symbol:
            raise LlmInstructionRefusedError("switch target must differ from the tracked pool")
        if decision.budget_usdc is None or decision.width_spacings is None:
            raise LlmInstructionRefusedError("switch_pool requires budget_usdc and width_spacings")
        return ValidatedLlmAction(decision=decision)

    if action in {
        LlmActionKind.RECENTER,
        LlmActionKind.STAKE,
        LlmActionKind.UNSTAKE,
        LlmActionKind.WITHDRAW,
        LlmActionKind.COLLECT,
    }:
        if state.tracked_symbol is None or state.tracked_token_id is None:
            raise LlmInstructionRefusedError(f"{action.value} requires a tracked position")
        if decision.symbol != state.tracked_symbol:
            raise LlmInstructionRefusedError(f"{action.value} must name the tracked symbol")
        if decision.token_id != state.tracked_token_id:
            raise LlmInstructionRefusedError(f"{action.value} must name the tracked token id")
        if action is LlmActionKind.RECENTER and (
            decision.width_spacings is None or decision.budget_usdc is None
        ):
            raise LlmInstructionRefusedError("recenter requires budget_usdc and width_spacings")
        if action is LlmActionKind.STAKE and state.tracked_staked:
            raise LlmInstructionRefusedError(
                "stake refused because the tracked position is already staked"
            )
        if action is LlmActionKind.UNSTAKE and not state.tracked_staked:
            raise LlmInstructionRefusedError(
                "unstake refused because the tracked position is not staked"
            )
        return ValidatedLlmAction(decision=decision)

    if action is LlmActionKind.EXIT_SWAP:
        if state.held_inventory_symbol is None or decision.symbol != state.held_inventory_symbol:
            raise LlmInstructionRefusedError(
                "exit_swap requires reconciled held stock inventory for the named symbol"
            )
        return ValidatedLlmAction(decision=decision)

    if action is LlmActionKind.EXIT_POSITION:
        if state.tracked_symbol is None or state.tracked_token_id is None:
            raise LlmInstructionRefusedError("exit_position requires a tracked position")
        if decision.symbol != state.tracked_symbol or decision.token_id != state.tracked_token_id:
            raise LlmInstructionRefusedError(
                "exit_position must name the reconciled tracked symbol and token id"
            )
        return ValidatedLlmAction(decision=decision)

    raise LlmInstructionRefusedError(f"unsupported LLM action {action.value}")


class OpenAICompatibleDecisionClient:
    """Call any OpenAI-compatible chat-completions endpoint for strict JSON."""

    def __init__(
        self, config: LlmControlConfig, transport: httpx.BaseTransport | None = None
    ) -> None:
        """Bind one enabled provider configuration and optional test transport."""
        if config.mode is LlmControlMode.OFF:
            raise ValueError("cannot construct an LLM client while control mode is off")
        if config.base_url is None or config.api_key is None or config.model is None:
            raise ValueError("enabled LLM configuration is incomplete")
        self._config = config
        self._base_url = config.base_url
        self._api_key = config.api_key
        self._model = config.model
        self._client = httpx.Client(transport=transport, timeout=config.timeout_seconds)

    def decide(self, context: Mapping[str, Any]) -> LlmActionDecision:
        """Request exactly one typed action from the configured model."""
        endpoint = self._base_url.rstrip("/") + "/chat/completions"
        response = self._client.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self._model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": _system_prompt()},
                    {
                        "role": "user",
                        "content": json.dumps(context, sort_keys=True, separators=(",", ":")),
                    },
                ],
            },
        )
        response.raise_for_status()
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            decoded = json.loads(content)
            return LlmActionDecision.model_validate(decoded)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValidationError) as error:
            raise ValueError("LLM response did not contain one valid action object") from error


def _system_prompt() -> str:
    """Return the stable authority contract sent to every decision model."""
    schema = LlmActionDecision.model_json_schema()
    return (
        "You are the decision layer for an Aerodrome Slipstream LP canary. "
        "Aerodrome on-chain state in the supplied context is authoritative for execution. "
        "External/reference prices are risk diagnostics only. Choose exactly one action. "
        "Never invent addresses, calldata, balances, token ids, symbols, or transaction data. "
        "Use only values present in context. A deterministic validator and audited executor "
        "will reject unsafe requests. Return JSON only matching this schema: "
        + json.dumps(schema, sort_keys=True, separators=(",", ":"))
    )
