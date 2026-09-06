"""Wallet-free exact-allowance planning and read-only transaction simulation."""

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol, Self, runtime_checkable

from pydantic import AfterValidator, BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress

# Base mainnet is the only chain supported by the first-release planning boundary.
BASE_CHAIN_ID: Literal[8453] = 8453
# The standard ERC-20 approve selector is the only transaction function supported in this slice.
ERC20_APPROVE_SELECTOR: Literal["0x095ea7b3"] = "0x095ea7b3"
# A uint256 bound prevents calldata encoding from overflowing an EVM word.
UINT256_MAX = 2**256 - 1
# Exact approvals contain a selector followed by two complete 32-byte ABI words.
APPROVE_CALLDATA_HEX_LENGTH = 2 + 8 + 64 + 64


def validate_hex_data(value: str) -> str:
    """Validate and normalize complete even-length EVM calldata.

    Args:
        value: Candidate hexadecimal calldata including its 0x prefix.

    Returns:
        Lowercase hexadecimal calldata suitable for deterministic comparison.

    Raises:
        ValueError: If the value is empty, odd-length, or contains non-hexadecimal characters.
    """
    # The payload excludes the prefix for byte-length and character validation.
    payload = value[2:] if value.startswith("0x") else ""
    if not payload or len(payload) % 2 != 0:
        raise ValueError("calldata must contain 0x followed by complete bytes")
    try:
        # Integer parsing provides a strict hexadecimal alphabet check.
        int(payload, 16)
    except ValueError as error:
        raise ValueError("calldata must contain only hexadecimal characters") from error
    return f"0x{payload.lower()}"


def validate_hex_bytes(value: str) -> str:
    """Validate and normalize possibly empty EVM return bytes.

    Args:
        value: Candidate hexadecimal bytes including the 0x prefix.

    Returns:
        Lowercase hexadecimal bytes, including the valid empty value 0x.

    Raises:
        ValueError: If bytes are odd-length, lack a prefix, or contain non-hex characters.
    """
    if not value.startswith("0x"):
        raise ValueError("EVM bytes must begin with 0x")
    # Return payload may be empty because successful EVM calls need not return data.
    payload = value[2:]
    if len(payload) % 2 != 0:
        raise ValueError("EVM bytes must contain complete bytes")
    try:
        # Parsing validates the hexadecimal alphabet when at least one byte is present.
        if payload:
            int(payload, 16)
    except ValueError as error:
        raise ValueError("EVM bytes must contain only hexadecimal characters") from error
    return f"0x{payload.lower()}"


# Validated calldata prevents malformed payloads from reaching a simulation backend.
HexData = Annotated[str, AfterValidator(validate_hex_data)]
# Validated return bytes preserve successful empty EVM responses.
HexBytes = Annotated[str, AfterValidator(validate_hex_bytes)]


class TransactionAction(StrEnum):
    """Identify the tightly scoped unsigned operations supported by this planner."""

    # Revoke allowance sets an existing non-zero approval to zero before replacement.
    REVOKE_ALLOWANCE = "revoke_allowance"
    # Set allowance approves exactly the requested raw token quantity.
    SET_ALLOWANCE = "set_allowance"


class PlanStatus(StrEnum):
    """Describe whether an unsigned exact-allowance plan was produced."""

    # Ready means a validated unsigned plan can proceed to read-only simulation.
    READY = "ready"
    # No action means the current allowance already equals the exact requested quantity.
    NO_ACTION = "no_action"
    # Blocked means a safety gate prevented any transaction plan from being created.
    BLOCKED = "blocked"


class SimulationStatus(StrEnum):
    """Describe one complete read-only plan simulation outcome."""

    # Passed means every unsigned transaction simulated successfully in order.
    PASSED = "passed"
    # Reverted means at least one transaction produced an EVM failure.
    REVERTED = "reverted"
    # Rejected means backend output was incomplete or internally inconsistent.
    REJECTED = "rejected"
    # Unavailable means no read-only simulation backend is configured or reachable.
    UNAVAILABLE = "unavailable"
    # Blocked means plan revalidation failed before the backend could be called.
    BLOCKED = "blocked"


class TransactionPolicy(BaseModel):
    """Define hard allowlists and emergency state for unsigned planning."""

    # Frozen strict fields keep all planning and simulation checks on one policy snapshot.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Chain ID is fixed to Base mainnet and cannot be configured to another network.
    chain_id: Literal[8453] = BASE_CHAIN_ID
    # Emergency halt defaults on so an unconfigured application cannot produce ready plans.
    emergency_halt: bool = True
    # Only verified token contracts may receive exact approval calls.
    allowed_token_addresses: frozenset[EvmAddress] = Field(default_factory=frozenset)
    # Only reviewed protocol contracts may become allowance spenders.
    allowed_spender_addresses: frozenset[EvmAddress] = Field(default_factory=frozenset)
    # Two transactions permit a safe zero-reset followed by one exact replacement approval.
    max_transactions_per_plan: Literal[2] = 2


class ExactAllowanceRequest(BaseModel):
    """Request an exact ERC-20 allowance without wallet or signing material."""

    # Frozen strict fields preserve the input snapshot used to derive the plan.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Owner is a public address used only as the eth_call sender during future simulation.
    owner_address: EvmAddress
    # Token must be a verified contract present in the transaction policy.
    token_address: EvmAddress
    # Spender must be a reviewed Aerodrome contract present in the policy.
    spender_address: EvmAddress
    # Requested allowance is the exact raw token quantity needed by the future action.
    amount_raw: Annotated[int, Field(gt=0, lt=UINT256_MAX)]
    # Current allowance is read-only evidence used to decide whether zero-reset is required.
    current_allowance_raw: Annotated[int, Field(ge=0, le=UINT256_MAX)]
    # Block number pins later simulation to the same Base state used for allowance evidence.
    block_number: Annotated[int, Field(gt=0)]


class UnsignedTransaction(BaseModel):
    """Represent one immutable transaction with no signature or broadcast metadata."""

    # Frozen strict fields prevent payload substitution after plan validation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Action explains whether this approval clears or sets allowance.
    action: TransactionAction
    # Chain ID is fixed to Base mainnet.
    chain_id: Literal[8453]
    # Sender is a public simulation identity and never a key-bearing wallet object.
    from_address: EvmAddress
    # Target is the allowlisted token contract receiving the approve call.
    to_address: EvmAddress
    # Native value is fixed to zero because ERC-20 approvals must not transfer ETH.
    value_wei: Literal[0]
    # Calldata is a complete ABI-encoded approve address and raw amount.
    data: HexData


class UnsignedTransactionPlan(BaseModel):
    """Collect a deterministic Base-state-pinned plan for simulation only."""

    # Frozen strict fields make the plan immutable after its identifier is calculated.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Plan ID is a deterministic SHA-256 integrity identifier, not a signature.
    plan_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    # Execution mode cannot be changed to a signing or broadcasting mode.
    execution_mode: Literal["simulation_only"]
    # Owner is the public address used as the eth_call sender.
    owner_address: EvmAddress
    # Block number pins every future eth_call in the sequence.
    block_number: Annotated[int, Field(gt=0)]
    # Transactions contain one exact approval or a zero-reset and exact approval pair.
    transactions: Annotated[tuple[UnsignedTransaction, ...], Field(min_length=1, max_length=2)]
    # Signing remains structurally unavailable in every serialized plan.
    signing_available: Literal[False]
    # Broadcasting remains structurally unavailable in every serialized plan.
    broadcast_available: Literal[False]
    # Diagnostics explain why each unsigned transaction exists.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class AllowancePlanResult(BaseModel):
    """Expose a ready plan or an explicit no-action or blocked result."""

    # Frozen strict fields preserve the relationship between status, plan, and diagnostics.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Status identifies the deterministic planning outcome.
    status: PlanStatus
    # Plan exists only when all policy checks pass and a state change would be required.
    plan: UnsignedTransactionPlan | None
    # Diagnostics provide ordered operator evidence for the outcome.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_status_plan_consistency(self) -> Self:
        """Reject planning output whose status contradicts plan presence."""
        # Ready is the sole status allowed to expose unsigned transaction payloads.
        if (self.status is PlanStatus.READY) is not (self.plan is not None):
            raise ValueError("a plan must exist exactly when planning status is ready")
        return self


class SimulationObservation(BaseModel):
    """Capture one backend eth_call result in plan order."""

    # Frozen strict fields prevent simulation evidence from changing after validation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Transaction index links this observation to the immutable plan sequence.
    transaction_index: Annotated[int, Field(ge=0)]
    # Success records whether the EVM call completed without reverting.
    success: bool
    # Gas used is informational and never authorizes a transaction.
    gas_used: Annotated[int, Field(ge=0)]
    # Return data preserves the complete read-only EVM response bytes.
    return_data: HexBytes
    # Revert reason is present when a backend can decode failure information.
    revert_reason: str | None


class SimulationBatch(BaseModel):
    """Carry one source-stamped backend response for an entire plan."""

    # Frozen strict fields preserve coherent simulation evidence.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Source identifies the read-only RPC backend used for eth_call.
    source: str
    # Block number must match the immutable plan state pin.
    block_number: Annotated[int, Field(gt=0)]
    # Observation time records when the backend completed the complete sequence.
    observed_at: datetime
    # Observations must cover every planned transaction exactly once and in order.
    observations: tuple[SimulationObservation, ...]

    @model_validator(mode="after")
    def require_timezone_aware_timestamp(self) -> Self:
        """Reject a naive simulation timestamp that lacks freshness meaning."""
        if self.observed_at.utcoffset() is None:
            raise ValueError("simulation observed_at must be timezone-aware")
        return self


class PlanSimulationResult(BaseModel):
    """Expose a complete read-only simulation result without an execution path."""

    # Frozen strict fields keep simulation status aligned with its evidence.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Status identifies success, revert, rejected evidence, or unavailability.
    status: SimulationStatus
    # Plan ID ties every result to an immutable unsigned plan.
    plan_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    # Source is absent when no backend produced an observation.
    source: str | None
    # Block number is always retained from the submitted plan.
    block_number: Annotated[int, Field(gt=0)]
    # Observation time is absent unless a backend completed the requested calls.
    observed_at: datetime | None
    # Observations are empty when validation or backend availability blocks simulation.
    observations: tuple[SimulationObservation, ...]
    # Diagnostics explain the result without implying authorization to execute.
    diagnostics: Annotated[tuple[str, ...], Field(min_length=1)]


class TransactionCapabilities(BaseModel):
    """Describe immutable wallet and transaction capabilities for UI consumers."""

    # Frozen strict fields make the public safety boundary unambiguous.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Execution mode is permanently simulation-only for this release.
    execution_mode: Literal["simulation_only"]
    # Private-key input is structurally unsupported.
    private_key_input_available: Literal[False]
    # Transaction signing is structurally unsupported.
    signing_available: Literal[False]
    # Transaction broadcasting is structurally unsupported.
    broadcast_available: Literal[False]
    # Wallet onboarding remains disabled until a separately reviewed release.
    wallet_onboarding_available: Literal[False]
    # Exact allowance is the sole currently supported unsigned action.
    supported_actions: tuple[Literal["exact_allowance"], ...]
    # Backend status distinguishes local planning from live read-only simulation.
    simulation_backend_configured: bool
    # Diagnostic explains the capability state to local operators.
    diagnostic: str


class SimulationUnavailableError(RuntimeError):
    """Signal that a read-only backend could not complete deterministic eth_call simulation."""


@runtime_checkable
class TransactionSimulationBackend(Protocol):
    """Define the only external transaction operation permitted by this release."""

    def simulate(self, plan: UnsignedTransactionPlan) -> SimulationBatch:
        """Run ordered eth_call operations without signing or broadcasting.

        Args:
            plan: Revalidated unsigned plan pinned to one Base block.

        Returns:
            Complete source-stamped observations for every planned call.
        """
        ...


class TransactionPlanner:
    """Plan and simulate exact approvals behind one immutable safety policy."""

    def __init__(
        self,
        policy: TransactionPolicy | None = None,
        simulation_backend: TransactionSimulationBackend | None = None,
    ) -> None:
        """Create a planner with an optional read-only simulation backend.

        Args:
            policy: Explicit target allowlists and emergency state.
            simulation_backend: Optional eth_call-only backend implementation.
        """
        # Default policy is emergency-halted with empty contract allowlists.
        self._policy = policy or TransactionPolicy()
        # Missing backend is a supported state with explicit unavailable diagnostics.
        self._simulation_backend = simulation_backend

    @property
    def policy(self) -> TransactionPolicy:
        """Return the immutable policy required to reproduce audited plans."""
        return self._policy

    def capabilities(self) -> TransactionCapabilities:
        """Return the immutable wallet-free execution boundary."""
        # Backend presence reports simulation availability without performing a network call.
        backend_configured = self._simulation_backend is not None
        # Diagnostic differentiates local planning support from external RPC availability.
        diagnostic = (
            "Read-only eth_call simulation backend is configured; signing and broadcasting "
            "remain unavailable."
            if backend_configured
            else "No read-only eth_call simulation backend is configured; unsigned planning "
            "remains local and signing, broadcasting, and wallet onboarding are unavailable."
        )
        return TransactionCapabilities(
            execution_mode="simulation_only",
            private_key_input_available=False,
            signing_available=False,
            broadcast_available=False,
            wallet_onboarding_available=False,
            supported_actions=("exact_allowance",),
            simulation_backend_configured=backend_configured,
            diagnostic=diagnostic,
        )

    def plan_exact_allowance(self, request: ExactAllowanceRequest) -> AllowancePlanResult:
        """Build an unsigned exact approval sequence or fail closed.

        Args:
            request: Public owner, trusted contracts, amounts, and Base block evidence.

        Returns:
            A ready immutable plan, explicit no-action outcome, or blocked diagnostic.
        """
        if self._policy.emergency_halt:
            return self._blocked_plan("Transaction planning is blocked by emergency halt.")
        if request.token_address not in self._policy.allowed_token_addresses:
            return self._blocked_plan("Token contract is outside the transaction allowlist.")
        if request.spender_address not in self._policy.allowed_spender_addresses:
            return self._blocked_plan("Spender contract is outside the transaction allowlist.")
        if request.current_allowance_raw == request.amount_raw:
            return AllowancePlanResult(
                status=PlanStatus.NO_ACTION,
                plan=None,
                diagnostics=("Current allowance already equals the exact requested amount.",),
            )

        # Transactions accumulate in their required simulation and future execution order.
        transactions: list[UnsignedTransaction] = []
        if request.current_allowance_raw != 0:
            transactions.append(
                self._approval_transaction(
                    TransactionAction.REVOKE_ALLOWANCE,
                    request.owner_address,
                    request.token_address,
                    request.spender_address,
                    0,
                )
            )
        transactions.append(
            self._approval_transaction(
                TransactionAction.SET_ALLOWANCE,
                request.owner_address,
                request.token_address,
                request.spender_address,
                request.amount_raw,
            )
        )
        # Immutable tuple is the exact content hashed into the deterministic plan identifier.
        planned_transactions = tuple(transactions)
        # Deterministic identifier detects any later mutation or payload substitution.
        plan_id = self._calculate_plan_id(
            request.owner_address, request.block_number, planned_transactions
        )
        # Diagnostic makes the zero-reset branch visible to an operator.
        diagnostic = (
            "Planned zero-reset followed by one exact allowance; simulation is required."
            if len(planned_transactions) == 2
            else "Planned one exact allowance; simulation is required."
        )
        # Ready does not mean approved for signing because no signing path exists.
        plan = UnsignedTransactionPlan(
            plan_id=plan_id,
            execution_mode="simulation_only",
            owner_address=request.owner_address,
            block_number=request.block_number,
            transactions=planned_transactions,
            signing_available=False,
            broadcast_available=False,
            diagnostics=(diagnostic,),
        )
        return AllowancePlanResult(status=PlanStatus.READY, plan=plan, diagnostics=(diagnostic,))

    def simulate(self, plan: UnsignedTransactionPlan) -> PlanSimulationResult:
        """Revalidate and simulate a plan through read-only eth_call only.

        Args:
            plan: Immutable unsigned transaction plan submitted for simulation.

        Returns:
            A complete simulation outcome with no signing or broadcast capability.
        """
        # Revalidation protects a future backend from caller-crafted targets or calldata.
        validation_diagnostics = self._validate_plan(plan)
        if validation_diagnostics:
            return PlanSimulationResult(
                status=SimulationStatus.BLOCKED,
                plan_id=plan.plan_id,
                source=None,
                block_number=plan.block_number,
                observed_at=None,
                observations=(),
                diagnostics=validation_diagnostics,
            )
        if self._simulation_backend is None:
            return PlanSimulationResult(
                status=SimulationStatus.UNAVAILABLE,
                plan_id=plan.plan_id,
                source=None,
                block_number=plan.block_number,
                observed_at=None,
                observations=(),
                diagnostics=(
                    "No read-only eth_call simulation backend is configured; no calls were made.",
                ),
            )
        try:
            # The external boundary receives only a fully revalidated immutable plan.
            batch = self._simulation_backend.simulate(plan)
        except SimulationUnavailableError as error:
            return PlanSimulationResult(
                status=SimulationStatus.UNAVAILABLE,
                plan_id=plan.plan_id,
                source=None,
                block_number=plan.block_number,
                observed_at=None,
                observations=(),
                diagnostics=(f"Read-only transaction simulation failed: {error}",),
            )

        # Exact indices prove that the backend returned every transaction once and in order.
        expected_indices = tuple(range(len(plan.transactions)))
        # Returned indices retain source order for direct completeness comparison.
        observed_indices = tuple(
            observation.transaction_index for observation in batch.observations
        )
        if batch.block_number != plan.block_number or observed_indices != expected_indices:
            return PlanSimulationResult(
                status=SimulationStatus.REJECTED,
                plan_id=plan.plan_id,
                source=batch.source,
                block_number=plan.block_number,
                observed_at=batch.observed_at,
                observations=(),
                diagnostics=(
                    "Simulation backend returned a mismatched block or incomplete transaction "
                    "sequence.",
                ),
            )
        # Any revert blocks the complete plan rather than accepting a successful prefix.
        simulation_status = (
            SimulationStatus.PASSED
            if all(observation.success for observation in batch.observations)
            else SimulationStatus.REVERTED
        )
        # Result diagnostic states that successful simulation is still not execution approval.
        diagnostic = (
            "Every transaction passed read-only simulation; signing and broadcasting remain "
            "unavailable."
            if simulation_status is SimulationStatus.PASSED
            else "At least one transaction reverted; the complete plan is blocked."
        )
        return PlanSimulationResult(
            status=simulation_status,
            plan_id=plan.plan_id,
            source=batch.source,
            block_number=plan.block_number,
            observed_at=batch.observed_at,
            observations=batch.observations,
            diagnostics=(diagnostic,),
        )

    def _validate_plan(self, plan: UnsignedTransactionPlan) -> tuple[str, ...]:
        """Return ordered diagnostics for any plan integrity or policy violation.

        Args:
            plan: Caller-supplied unsigned plan requiring complete revalidation.

        Returns:
            Empty tuple when every boundary passes, otherwise ordered failure evidence.
        """
        # Diagnostics accumulate in deterministic policy order.
        diagnostics: list[str] = []
        if self._policy.emergency_halt:
            diagnostics.append("Transaction simulation is blocked by emergency halt.")
        # Recalculated identifier detects mutation of owner, block, order, or payload fields.
        expected_plan_id = self._calculate_plan_id(
            plan.owner_address, plan.block_number, plan.transactions
        )
        if plan.plan_id != expected_plan_id:
            diagnostics.append("Plan identifier does not match its unsigned transaction content.")
        # Only the planner's one-step or zero-reset sequence is semantically valid.
        plan_actions = tuple(transaction.action for transaction in plan.transactions)
        # Canonical sequences prevent partial revocations or repeated allowance-set calls.
        allowed_action_sequences = (
            (TransactionAction.SET_ALLOWANCE,),
            (TransactionAction.REVOKE_ALLOWANCE, TransactionAction.SET_ALLOWANCE),
        )
        if plan_actions not in allowed_action_sequences:
            diagnostics.append("Transaction actions do not form a canonical exact-allowance plan.")
        for transaction in plan.transactions:
            if transaction.from_address != plan.owner_address:
                diagnostics.append("Transaction sender does not match the plan owner.")
            if transaction.to_address not in self._policy.allowed_token_addresses:
                diagnostics.append("Transaction target is outside the token allowlist.")
            # Decoded fields permit semantic checks beyond a selector-only allowlist.
            decoded_approval = self._decode_approval(transaction.data)
            if decoded_approval is None:
                diagnostics.append("Transaction calldata is not an exact ERC-20 approve call.")
                continue
            # Decoded spender and amount are safe only after complete ABI-shape validation.
            spender_address, amount_raw = decoded_approval
            if spender_address not in self._policy.allowed_spender_addresses:
                diagnostics.append("Encoded spender is outside the transaction allowlist.")
            if transaction.action is TransactionAction.REVOKE_ALLOWANCE and amount_raw != 0:
                diagnostics.append("Allowance-revocation transaction must encode zero amount.")
            if transaction.action is TransactionAction.SET_ALLOWANCE and (
                amount_raw == 0 or amount_raw == UINT256_MAX
            ):
                diagnostics.append("Exact allowance must be positive and cannot be unlimited.")
        return tuple(diagnostics)

    def _approval_transaction(
        self,
        action: TransactionAction,
        owner_address: str,
        token_address: str,
        spender_address: str,
        amount_raw: int,
    ) -> UnsignedTransaction:
        """Encode one standard ERC-20 approval transaction.

        Args:
            action: Whether the transaction revokes or sets allowance.
            owner_address: Public simulation sender address.
            token_address: Allowlisted ERC-20 contract receiving the call.
            spender_address: Allowlisted Aerodrome contract receiving allowance.
            amount_raw: Exact uint256 raw token amount encoded in calldata.

        Returns:
            A zero-value unsigned Base transaction.
        """
        # ABI address word is left-padded to the required 32-byte EVM word.
        spender_word = spender_address.removeprefix("0x").rjust(64, "0")
        # ABI integer word is the exact raw allowance left-padded to 32 bytes.
        amount_word = format(amount_raw, "064x")
        # Standard selector and words form complete deterministic approve calldata.
        calldata = f"{ERC20_APPROVE_SELECTOR}{spender_word}{amount_word}"
        return UnsignedTransaction(
            action=action,
            chain_id=self._policy.chain_id,
            from_address=owner_address,
            to_address=token_address,
            value_wei=0,
            data=calldata,
        )

    def _decode_approval(self, calldata: str) -> tuple[str, int] | None:
        """Decode a canonical approve call for semantic policy validation.

        Args:
            calldata: Validated hexadecimal transaction data.

        Returns:
            Normalized spender and raw amount, or None for a non-canonical payload.
        """
        if len(calldata) != APPROVE_CALLDATA_HEX_LENGTH or not calldata.startswith(
            ERC20_APPROVE_SELECTOR
        ):
            return None
        # Address word must use canonical zero padding before its final 20 bytes.
        spender_word = calldata[10:74]
        if spender_word[:24] != "0" * 24:
            return None
        # Final 20 bytes are reconstructed as a normalized EVM address.
        spender_address = f"0x{spender_word[-40:]}"
        # Second ABI word is the exact uint256 allowance amount.
        amount_raw = int(calldata[74:138], 16)
        return spender_address, amount_raw

    def _calculate_plan_id(
        self,
        owner_address: str,
        block_number: int,
        transactions: tuple[UnsignedTransaction, ...],
    ) -> str:
        """Calculate a stable integrity identifier for complete plan content.

        Args:
            owner_address: Public simulation sender captured by the plan.
            block_number: Base state pin captured by the plan.
            transactions: Ordered immutable unsigned transactions.

        Returns:
            Lowercase 64-character SHA-256 digest.
        """
        # Each field is separated explicitly so equivalent inputs hash identically.
        transaction_parts = [
            ":".join(
                (
                    transaction.action.value,
                    str(transaction.chain_id),
                    transaction.from_address,
                    transaction.to_address,
                    str(transaction.value_wei),
                    transaction.data,
                )
            )
            for transaction in transactions
        ]
        # Owner, block, and ordered payloads form the complete canonical plan representation.
        canonical_plan = "|".join((owner_address, str(block_number), *transaction_parts))
        # Digest is used only for integrity identification and never for cryptographic signing.
        return hashlib.sha256(canonical_plan.encode(), usedforsecurity=False).hexdigest()

    def _blocked_plan(self, diagnostic: str) -> AllowancePlanResult:
        """Build a consistent blocked planning result.

        Args:
            diagnostic: Human-readable policy failure evidence.

        Returns:
            Blocked result containing no transaction payloads.
        """
        return AllowancePlanResult(
            status=PlanStatus.BLOCKED,
            plan=None,
            diagnostics=(diagnostic,),
        )
