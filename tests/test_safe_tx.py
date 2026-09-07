"""Behavior tests for the Safe transaction layer."""

import json
from typing import Any

import httpx
import pytest
from eth_account import Account

from aero_bot.safe_tx import (
    CHECK_SIGNATURES_SELECTOR,
    SAFE_DOMAIN_SEPARATOR_SELECTOR,
    SAFE_NONCE_SELECTOR,
    BuiltSafeTransaction,
    SafeOwnerSignature,
    SafeSignatureValidation,
    SafeTransaction,
    SafeTransactionRpcBackend,
    SafeTransactionRpcRevertError,
    SafeTransactionUnavailableError,
    SignatureMismatchError,
    StaleNonceError,
    build_exec_transaction_calldata,
    build_safe_transaction,
    compute_safe_domain_separator,
    compute_safe_tx_hash,
    ensure_usable_nonce,
    sign_safe_tx_hash,
)

# The canary Safe and its live-verified contract facts; every value below was
# observed read-only on Base and against the Safe Transaction Service records.
SAFE_ADDRESS = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
LIVE_DOMAIN_SEPARATOR = "0x2401ab93a5043f9b56993751c9d5ffd546082acf131c93442d16df07c3ba7340"
# The Safe Transaction Service's recorded safeTxHash for the executed exact
# 1 USDC approval to the Aerodrome universal router at nonce 0.
REFERENCE_APPROVE_SAFE_TX_HASH = (
    "0x5ab40d913f5825392676b380eb1d3c1b6e7012b718f275f7e0243c1b5d6e3360"
)
# The exact approve calldata of that reference transaction: the router spender
# with an exact 1,000,000-unit (1.00 USDC) allowance.
REFERENCE_APPROVE_CALLDATA = (
    "0x095ea7b3000000000000000000000000caf22ce3"
    "1298cf2bf1d152862f80216478ad7c6700000000000000000000000000000000000000000000000000000000000f4240"
)

# The Aerodrome universal router the reference swap executed through.
ROUTER_ADDRESS = "0xcaf22ce31298cf2bf1d152862f80216478ad7c67"
# The Safe Transaction Service's recorded safeTxHash for the executed 1 USDC
# swap through the router at nonce 3.
REFERENCE_SWAP_SAFE_TX_HASH = "0x33007ff9944159ce233a2df4b2b69f34a0abb7b40af4a90530a20e5c91382af3"
# The complete router execute calldata of that reference swap, byte-identical
# to the on-chain transaction.
REFERENCE_SWAP_CALLDATA = (
    "0x3593564c000000000000000000000000000000000000000000000000000000000000"
    "006000000000000000000000000000000000000000000000000000000000000000a0"
    "000000000000000000000000000000000000000000000000000000006a9e34e00000"
    "00000000000000000000000000000000000000000000000000000000000100000000"
    "00000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000010000000000000000"
    "00000000000000000000000000000000000000000000002000000000000000000000"
    "00000000000000000000000000000000000000000120000000000000000000000000"
    "b69ab6c7e73f711d5f2d10fed8f0d09b1d028c280000000000000000000000000000"
    "0000000000000000000000000000000f424000000000000000000000000000000000"
    "0000000000000000000000000004c055000000000000000000000000000000000000"
    "00000000000000000000000000c00000000000000000000000000000000000000000"
    "00000000000000000000000100000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000002b833589fcd6edb6e08f4c7c32d4f71b54bda0291308000ab20000"
    "0000000000000000c2e324d24d7eecd1fb0000000000000000000000000000000000"
    "000000008779ce964b87d3f896438a7c62e262635f7239717a66386f700b00802180"
    "21802180218021802180218021"
)
# A fixture block identifier shared by every mocked read-only call.
FIXTURE_NONCE = 4


class FixtureSafeTransport(httpx.MockTransport):
    """Serve deterministic Safe JSON-RPC responses without touching the network."""

    def __init__(
        self,
        nonce: int = FIXTURE_NONCE,
        domain_separator: str = LIVE_DOMAIN_SEPARATOR,
        failures_before_success: int = 0,
        error_code: int = -32016,
        check_signatures_reverts: bool = False,
    ) -> None:
        """Configure the fixture endpoint with optional transient failures.

        Args:
            nonce: The live nonce value served by nonce().
            domain_separator: The domain separator served by domainSeparator().
            failures_before_success: Rate-limit failures served before success.
            error_code: The JSON-RPC error code used for transient failures.
            check_signatures_reverts: Serve a contract revert for checkSignatures.
        """
        # Request counting lets tests assert bounded retry behavior exactly.
        self.calls: list[dict[str, Any]] = []
        self._nonce = nonce
        self._domain_separator = domain_separator
        self._remaining_failures = failures_before_success
        self._error_code = error_code
        self._check_signatures_reverts = check_signatures_reverts
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one JSON-RPC request with configured fixture evidence."""
        payload = json.loads(request.content.decode("utf-8"))
        self.calls.append(payload)
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            status = 429 if self._error_code == -32016 else 200
            if status == 429:
                return httpx.Response(429, json={"error": {"code": self._error_code}})
            transient_message = "rate limited" if self._error_code == -32016 else "broken"
            return httpx.Response(
                200, json={"error": {"code": self._error_code, "message": transient_message}}
            )
        call_data = str(payload["params"][0]["data"])
        if call_data == f"0x{SAFE_NONCE_SELECTOR}":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + format(self._nonce, "064x")}
            )
        if call_data == f"0x{SAFE_DOMAIN_SEPARATOR_SELECTOR}":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": self._domain_separator}
            )
        if call_data.startswith(f"0x{CHECK_SIGNATURES_SELECTOR}"):
            if self._check_signatures_reverts:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {"code": 3, "message": "execution reverted: GS021"},
                    },
                )
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x"})
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "nope"}}
        )


def reference_approve_transaction() -> SafeTransaction:
    """Build the exact reference nonce-0 approval transaction."""
    return SafeTransaction(
        to_address="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        data=REFERENCE_APPROVE_CALLDATA,
        nonce=0,
    )


def reference_swap_transaction() -> SafeTransaction:
    """Build the exact reference nonce-3 router swap transaction."""
    return SafeTransaction(to_address=ROUTER_ADDRESS, data=REFERENCE_SWAP_CALLDATA, nonce=3)


def make_backend(transport: httpx.MockTransport) -> SafeTransactionRpcBackend:
    """Create one backend over a fixture transport with no real delays."""
    return SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=transport,
        sleep=lambda seconds: None,
    )


def test_domain_separator_matches_live_contract() -> None:
    """The minimal domain reproduces the canary Safe's on-chain separator."""
    assert compute_safe_domain_separator(SAFE_ADDRESS) == LIVE_DOMAIN_SEPARATOR


def test_domain_separator_rejects_other_chains() -> None:
    """Only Base mainnet may anchor Safe transaction hashing."""
    with pytest.raises(ValueError, match="chain id 8453"):
        compute_safe_domain_separator(SAFE_ADDRESS, chain_id=8454)


def test_safe_tx_hash_matches_recorded_reference_approve() -> None:
    """The computed hash equals the service's recorded hash for nonce 0."""
    assert (
        compute_safe_tx_hash(reference_approve_transaction(), SAFE_ADDRESS)
        == REFERENCE_APPROVE_SAFE_TX_HASH
    )


def test_safe_tx_hash_matches_recorded_reference_swap() -> None:
    """The computed hash equals the service's recorded hash for the swap."""
    assert (
        compute_safe_tx_hash(reference_swap_transaction(), SAFE_ADDRESS)
        == REFERENCE_SWAP_SAFE_TX_HASH
    )


def test_safe_tx_hash_binds_every_field() -> None:
    """Any field change produces a different domain-separated hash."""
    base = compute_safe_tx_hash(reference_approve_transaction(), SAFE_ADDRESS)
    changed_nonce = reference_approve_transaction().model_copy(update={"nonce": 1})
    assert compute_safe_tx_hash(changed_nonce, SAFE_ADDRESS) != base


def test_build_safe_transaction_carries_domain_and_hash() -> None:
    """The built model carries the same separator and hash as the functions."""
    built = build_safe_transaction(reference_swap_transaction(), SAFE_ADDRESS)
    assert built.domain_separator == LIVE_DOMAIN_SEPARATOR
    assert built.chain_id == 8453
    assert built.safe_tx_hash == REFERENCE_SWAP_SAFE_TX_HASH


def test_model_rejects_non_zero_gas_parameters() -> None:
    """The gas parameter fields are structurally locked to the reference shape."""
    with pytest.raises(ValueError):
        SafeTransaction(
            to_address=ROUTER_ADDRESS,
            data=REFERENCE_APPROVE_CALLDATA,
            nonce=0,
            safe_tx_gas=1,  # type: ignore[arg-type]
        )


def test_model_rejects_malformed_calldata() -> None:
    """Only complete hexadecimal calldata may enter a Safe transaction."""
    with pytest.raises(ValueError):
        SafeTransaction(to_address=ROUTER_ADDRESS, data="0xzz", nonce=0)


def test_ensure_usable_nonce_rejects_stale_nonce() -> None:
    """A nonce older than the live one is refused before building."""
    with pytest.raises(StaleNonceError, match="older than the live nonce"):
        ensure_usable_nonce(2, 4)
    # The live nonce and any future nonce remain usable.
    ensure_usable_nonce(4, 4)
    ensure_usable_nonce(5, 4)


def test_sign_safe_tx_hash_round_trips_to_signer() -> None:
    """A produced signature recovers to exactly the injected key's address."""
    account = Account.create()
    safe_tx_hash = compute_safe_tx_hash(reference_approve_transaction(), SAFE_ADDRESS)
    signature = sign_safe_tx_hash(bytes(account.key), safe_tx_hash)
    assert signature.signer_address == account.address.lower()
    assert signature.v in (27, 28)
    encoded = bytes.fromhex(signature.encoded[2:])
    assert len(encoded) == 65
    assert encoded[-1] == signature.v


def test_sign_safe_tx_hash_is_deterministic_per_key() -> None:
    """The same key and hash produce the identical signature twice."""
    account = Account.create()
    safe_tx_hash = compute_safe_tx_hash(reference_swap_transaction(), SAFE_ADDRESS)
    first = sign_safe_tx_hash(bytes(account.key), safe_tx_hash)
    second = sign_safe_tx_hash(bytes(account.key), safe_tx_hash)
    assert first == second


def test_sign_safe_tx_hash_rejects_wrong_key_length() -> None:
    """Only exactly 32 raw key bytes may reach the signing call."""
    account = Account.create()
    safe_tx_hash = compute_safe_tx_hash(reference_approve_transaction(), SAFE_ADDRESS)
    with pytest.raises(ValueError, match="exactly 32 raw bytes"):
        sign_safe_tx_hash(bytes(account.key)[:-1], safe_tx_hash)


def test_sign_safe_tx_hash_rejects_malformed_hash() -> None:
    """Only a complete 32-byte hash may be signed."""
    account = Account.create()
    with pytest.raises(ValueError, match="32 bytes"):
        sign_safe_tx_hash(bytes(account.key), "0xabcd")
    with pytest.raises(ValueError, match="hexadecimal"):
        sign_safe_tx_hash(bytes(account.key), "0x" + "z" * 64)


def test_sign_safe_tx_hash_detects_recovery_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A corrupted signature that cannot recover fails closed."""
    account = Account.create()
    other_key = bytes(Account.create().key)
    safe_tx_hash = compute_safe_tx_hash(reference_approve_transaction(), SAFE_ADDRESS)
    original_sign_hash = Account.unsafe_sign_hash
    monkeypatch.setattr(
        "aero_bot.safe_tx.Account.unsafe_sign_hash",
        lambda message_hash, key: original_sign_hash(message_hash, other_key),
    )
    with pytest.raises(SignatureMismatchError, match="recovery produced"):
        sign_safe_tx_hash(bytes(account.key), safe_tx_hash)


def test_exec_transaction_calldata_matches_cast_canonical_vector() -> None:
    """The complete encoding matches cast for one fixed approval and signature."""
    transaction = SafeTransaction(
        to_address="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        data=(
            "0x095ea7b3000000000000000000000000caf22ce31298cf2bf1d152862f80216478ad7c67"
            "0000000000000000000000000000000000000000000000000000000001312d00"
        ),
        nonce=4,
    )
    signature = SafeOwnerSignature(
        v=27,
        r=1,
        s=2,
        signer_address="0x0000000000000000000000000000000000000001",
        encoded="0x" + "0" * 63 + "1" + "0" * 63 + "2" + "1b",
    )
    expected = (
        "0x6a761202000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda02913"
        "0000000000000000000000000000000000000000000000000000000000000000"
        "0000000000000000000000000000000000000000000000000000000000000140"
        + "0" * (6 * 64)
        + "00000000000000000000000000000000000000000000000000000000000001c0"
        + "0000000000000000000000000000000000000000000000000000000000000044"
        + transaction.data[2:]
        + "0" * 56
        + "0000000000000000000000000000000000000000000000000000000000000041"
        + signature.encoded[2:]
        + "0" * 62
    )
    assert build_exec_transaction_calldata(transaction, signature) == expected


def test_backend_fetch_live_nonce_reads_contract() -> None:
    """The nonce read decodes the live nonce() return word."""
    transport = FixtureSafeTransport(nonce=7)
    backend = make_backend(transport)
    assert backend.fetch_live_nonce() == 7
    assert transport.calls[0]["params"][0]["to"] == SAFE_ADDRESS


def test_backend_reads_live_domain_separator() -> None:
    """The domain separator read returns the contract's exact word."""
    backend = make_backend(FixtureSafeTransport())
    assert backend.read_domain_separator() == LIVE_DOMAIN_SEPARATOR


def test_backend_exposes_normalized_safe_address() -> None:
    """The backend normalizes and exposes the Safe address it reads."""
    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address="0xB69ab6C7E73F711D5f2d10feD8f0d09B1D028C28",
        sleep=lambda seconds: None,
    )
    assert backend.safe_address == SAFE_ADDRESS


def test_backend_retries_transport_errors() -> None:
    """A dropped connection is retried before the nonce read succeeds."""
    attempts = {"count": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.TransportError("connection reset")
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": "0x" + format(FIXTURE_NONCE, "064x")},
        )

    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=httpx.MockTransport(flaky),
        sleep=lambda seconds: None,
    )
    assert backend.fetch_live_nonce() == FIXTURE_NONCE
    assert attempts["count"] == 2


def test_backend_retries_json_error_rate_limit() -> None:
    """A JSON-RPC rate-limit error with HTTP 200 is retried like a 429."""
    attempts = {"count": 0}

    def rate_limited(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(200, json={"error": {"code": -32016, "message": "rate limited"}})
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "result": "0x" + format(FIXTURE_NONCE, "064x")},
        )

    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=httpx.MockTransport(rate_limited),
        sleep=lambda seconds: None,
    )
    assert backend.fetch_live_nonce() == FIXTURE_NONCE
    assert attempts["count"] == 2


def test_backend_rejects_non_json_response() -> None:
    """A 200 response that is not JSON fails closed."""

    def plain_text(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=httpx.MockTransport(plain_text),
        sleep=lambda seconds: None,
    )
    with pytest.raises(SafeTransactionUnavailableError, match="not valid JSON"):
        backend.fetch_live_nonce()


def test_backend_rejects_body_without_result_or_error() -> None:
    """A body carrying neither result nor error is refused."""

    def empty_object(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"foo": 1})

    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=httpx.MockTransport(empty_object),
        sleep=lambda seconds: None,
    )
    with pytest.raises(SafeTransactionUnavailableError, match="neither result nor error"):
        backend.fetch_live_nonce()


def test_backend_retries_rate_limited_nonce_read() -> None:
    """Transient rate limiting is absorbed before the nonce read succeeds."""
    transport = FixtureSafeTransport(failures_before_success=2)
    sleeps: list[float] = []
    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=transport,
        sleep=sleeps.append,
    )
    assert backend.fetch_live_nonce() == FIXTURE_NONCE
    assert len(transport.calls) == 3
    # Exponential backoff doubles across the two retries.
    assert sleeps == [0.5, 1.0]


def test_backend_fails_closed_after_exhausted_retries() -> None:
    """Persistent rate limiting surfaces as an explicit unavailable error."""
    transport = FixtureSafeTransport(failures_before_success=99)
    backend = make_backend(transport)
    with pytest.raises(SafeTransactionUnavailableError, match="failed after 5 attempts"):
        backend.fetch_live_nonce()


def test_backend_rejects_short_nonce_word() -> None:
    """A truncated nonce return word is refused instead of partially decoded."""
    transport = FixtureSafeTransport()

    def short_nonce(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x04"})

    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=httpx.MockTransport(short_nonce),
        sleep=lambda seconds: None,
    )
    assert transport is not None
    with pytest.raises(SafeTransactionUnavailableError, match="instead of 32"):
        backend.fetch_live_nonce()


def test_backend_rejects_short_domain_separator_word() -> None:
    """A truncated domain separator return is refused undecoded."""

    def short_separator(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x2401ab93"})

    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=httpx.MockTransport(short_separator),
        sleep=lambda seconds: None,
    )
    with pytest.raises(SafeTransactionUnavailableError, match="instead of 32"):
        backend.read_domain_separator()


def test_backend_rejects_oversized_response() -> None:
    """A response above the configured bound fails closed immediately."""

    def huge_response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (1024 * 1024 + 1))

    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=httpx.MockTransport(huge_response),
        sleep=lambda seconds: None,
    )
    with pytest.raises(SafeTransactionUnavailableError, match="above the limit"):
        backend.fetch_live_nonce()


def test_backend_rejects_unexpected_http_status() -> None:
    """A non-retriable HTTP failure surfaces as an explicit error."""

    def forbidden(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    backend = SafeTransactionRpcBackend(
        rpc_url="https://example.invalid",
        safe_address=SAFE_ADDRESS,
        transport=httpx.MockTransport(forbidden),
        sleep=lambda seconds: None,
    )
    with pytest.raises(SafeTransactionUnavailableError, match="unexpected HTTP status 403"):
        backend.fetch_live_nonce()


def test_backend_rejects_unrecoverable_rpc_error() -> None:
    """A non-retriable JSON-RPC error fails closed without retries."""
    transport = FixtureSafeTransport(failures_before_success=1, error_code=-32602)
    backend = make_backend(transport)
    with pytest.raises(SafeTransactionUnavailableError, match="RPC error -32602"):
        backend.fetch_live_nonce()


def test_validate_owner_signature_accepts_live_contract_verdict() -> None:
    """The read-only proof matches cast's canonical checkSignatures vector."""
    transport = FixtureSafeTransport()
    backend = make_backend(transport)
    built = BuiltSafeTransaction(
        domain_separator=LIVE_DOMAIN_SEPARATOR,
        transaction=reference_approve_transaction(),
        safe_tx_hash="0x" + "11" * 32,
    )
    signature = SafeOwnerSignature(
        v=27,
        r=1,
        s=2,
        signer_address="0x0000000000000000000000000000000000000001",
        encoded="0x" + "0" * 63 + "1" + "0" * 63 + "2" + "1b",
    )
    validation = backend.validate_owner_signature(built, signature)
    assert validation == SafeSignatureValidation(
        verified=True,
        source="read_only_eth_call",
        diagnostic="The live Safe contract accepted the signature read-only.",
    )
    expected = (
        "0x934f3a11"
        + "11" * 32
        + "0000000000000000000000000000000000000000000000000000000000000060"
        + "0000000000000000000000000000000000000000000000000000000000000080"
        + "0000000000000000000000000000000000000000000000000000000000000000"
        + "0000000000000000000000000000000000000000000000000000000000000041"
        + signature.encoded[2:]
        + "0" * 62
    )
    assert transport.calls[0]["params"][0]["data"] == expected


def test_validate_owner_signature_reports_revert_evidence() -> None:
    """A reverting checkSignatures call reports the contract's rejection."""
    backend = make_backend(FixtureSafeTransport(check_signatures_reverts=True))
    account = Account.create()
    built = build_safe_transaction(reference_approve_transaction(), SAFE_ADDRESS)
    signature = sign_safe_tx_hash(bytes(account.key), built.safe_tx_hash)
    validation = backend.validate_owner_signature(built, signature)
    assert validation.verified is False
    assert "GS021" in validation.diagnostic


def test_backend_constructor_validates_bounds() -> None:
    """Non-positive bounds are rejected before any request is attempted."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        SafeTransactionRpcBackend(
            rpc_url="https://example.invalid", safe_address=SAFE_ADDRESS, timeout_seconds=0
        )
    with pytest.raises(ValueError, match="max_attempts"):
        SafeTransactionRpcBackend(
            rpc_url="https://example.invalid", safe_address=SAFE_ADDRESS, max_attempts=0
        )
    with pytest.raises(ValueError, match="max_response_bytes"):
        SafeTransactionRpcBackend(
            rpc_url="https://example.invalid",
            safe_address=SAFE_ADDRESS,
            max_response_bytes=0,
        )
    with pytest.raises(ValueError, match="address"):
        SafeTransactionRpcBackend(rpc_url="https://example.invalid", safe_address="0x1234")


def test_rpc_revert_is_an_unavailable_failure() -> None:
    """A contract revert is a subclass of the unavailable error family."""
    assert issubclass(SafeTransactionRpcRevertError, SafeTransactionUnavailableError)


def test_built_transaction_models_are_immutable() -> None:
    """Built transaction content cannot be mutated after construction."""
    built = build_safe_transaction(reference_approve_transaction(), SAFE_ADDRESS)
    with pytest.raises(ValueError):
        built.transaction.nonce = 99
    signature = sign_safe_tx_hash(bytes(Account.create().key), built.safe_tx_hash)
    with pytest.raises(ValueError):
        signature.v = 26  # type: ignore[assignment]


def test_owner_signature_model_rejects_malformed_encoding() -> None:
    """Only a complete 65-byte encoding may form an owner signature."""
    with pytest.raises(ValueError):
        SafeOwnerSignature(
            v=27,
            r=1,
            s=1,
            signer_address=SAFE_ADDRESS,
            encoded="0x1234",
        )


def test_built_transaction_rejects_malformed_hash() -> None:
    """The built model only carries complete lowercase 32-byte hashes."""
    transaction = reference_approve_transaction()
    with pytest.raises(ValueError):
        BuiltSafeTransaction(
            domain_separator=LIVE_DOMAIN_SEPARATOR,
            transaction=transaction,
            safe_tx_hash="0x1234",
        )
