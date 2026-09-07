"""Safe execTransaction construction, EIP-712 SafeTx hashing, and owner signing.

This module is the transaction layer for the canary execution phase: it builds
the calldata for Safe ``execTransaction``, computes the EIP-712 SafeTx hash the
contract will verify, signs it with an owner key through the audited
``eth-account`` library, and validates the resulting signature both locally
(recovery round trip) and read-only against the live contract
(``checkNSignatures``). It never sources key material itself: raw key bytes
arrive as an argument, are used only inside one signing call, and are never
stored, logged, or persisted.

Two contract facts were verified live against the canary Safe
(0xB69ab6C7E73F711D5f2d10feD8f0d09B1D028C28, Base, v1.4.1+L2) and shape this
module rather than being re-derived:

- The Safe signs with the minimal EIP-712 domain
  ``EIP712Domain(uint256 chainId,address verifyingContract)`` - no name, no
  version - exactly as its on-chain ``domainSeparator()`` computes it. The
  locally computed separator matches the live contract's
  0x2401ab93a5043f9b56993751c9d5ffd546082acf131c93442d16df07c3ba7340, and
  SafeTx hashes built over it match every Safe Transaction Service record for
  this Safe. The commonly documented ``{name: "Safe", version: "1.4.1"}``
  domain would produce signatures the live contract rejects.
- The nonce getter reachable on this deployment is ``nonce()``;
  ``getNonce()`` reverts. The live nonce was 4 after the four executed
  reference transactions.
"""

import time
from collections.abc import Callable
from typing import Annotated, Literal, cast

import httpx
from eth_account import Account
from eth_keys.datatypes import Signature
from eth_utils.crypto import keccak
from pydantic import BaseModel, Field

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.transactions import HexData

# Base mainnet is the only chain the Safe transaction layer supports.
SAFE_CHAIN_ID: Literal[8453] = 8453
# The EIP-712 domain schema the Safe contract itself hashes, verified against
# the live contract's domainSeparator().
SAFE_DOMAIN_TYPE_STRING = "EIP712Domain(uint256 chainId,address verifyingContract)"
# The SafeTx typed-data schema, byte-identical to the contract's SAFE_TX_TYPEHASH.
SAFE_TX_TYPE_STRING = (
    "SafeTx(address to,uint256 value,bytes data,uint8 operation,uint256 safeTxGas,"
    "uint256 baseGas,uint256 gasPrice,address gasToken,address refundReceiver,uint256 nonce)"
)
# Type hashes derive from the schemas above at import time so they cannot drift.
SAFE_DOMAIN_TYPEHASH = keccak(text=SAFE_DOMAIN_TYPE_STRING)
SAFE_TX_TYPEHASH = keccak(text=SAFE_TX_TYPE_STRING)
# The EIP-712 signing prefix precedes the domain separator and struct hash.
EIP712_PREFIX = b"\x19\x01"
# The all-zero address represents absent gas token and refund receiver arguments.
ZERO_ADDRESS = "0x" + "0" * 40
# keccak256 of the full execTransaction signature, first four bytes.
EXEC_TRANSACTION_SIGNATURE = (
    "execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)"
)
EXEC_TRANSACTION_SELECTOR = "6a761202"
# keccak256("nonce()")[0:4], the live-verified nonce getter for this deployment.
SAFE_NONCE_SELECTOR = "affed0e0"
# keccak256("domainSeparator()")[0:4], used to cross-check local domain hashing.
SAFE_DOMAIN_SEPARATOR_SELECTOR = "f698da25"
# keccak256("checkNSignatures(bytes32,bytes,bytes)")[0:4], read-only signature proof.
CHECK_N_SIGNATURES_SELECTOR = "9c546ffd"
# Every ABI argument or return word below occupies exactly 32 bytes.
WORD_BYTES = 32
# Twenty seconds bounds one failed request without blocking the caller.
REQUEST_TIMEOUT_SECONDS = 20.0
# Five attempts with exponential backoff absorb public-RPC rate limiting.
MAX_REQUEST_ATTEMPTS = 5
# Backoff starts at half a second and doubles per retry.
BASE_BACKOFF_SECONDS = 0.5
# One MiB bounds every response far above the fixed-word calls made here.
MAX_RESPONSE_BYTES = 1024 * 1024
# Base's public endpoint reports rate limiting with this JSON-RPC error code.
RATE_LIMIT_ERROR_CODE = -32016
# The standard JSON-RPC code reporting an in-contract execution revert.
EXECUTION_REVERT_ERROR_CODE = 3
# The fixed JSON-RPC request identifier keeps read-only calls reproducible.
JSON_RPC_ID = 1
# Read-only contract calls are attributed to the zero address.
READ_ONLY_CALLER = ZERO_ADDRESS


class SafeTransactionUnavailableError(RuntimeError):
    """Signal that a read-only Safe RPC read could not complete bounded retries."""


class SafeTransactionRpcRevertError(SafeTransactionUnavailableError):
    """Signal that a read-only Safe eth_call reverted inside the contract."""


class StaleNonceError(ValueError):
    """Refuse a Safe transaction nonce older than the contract's live nonce."""


class SignatureMismatchError(RuntimeError):
    """Signal that a produced signature failed local recovery validation."""


class SafeTransaction(BaseModel):
    """Represent one execTransaction payload before hashing or signing."""

    # Frozen strict fields prevent the hashed content from changing afterwards.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Target is the contract the Safe calls.
    to_address: EvmAddress
    # Native value transferred with the call; swaps send zero because the
    # router pulls USDC through the pre-set exact allowance.
    value_wei: Annotated[int, Field(ge=0)] = 0
    # Complete ABI-encoded call payload executed by the Safe.
    data: HexData
    # Operation is fixed to CALL; DELEGATECALL is never built by this layer.
    operation: Literal[0] = 0
    # Gas limits, price, token, and refund receiver stay at zero exactly as
    # every reference transaction did, so the executor EOA pays its own gas.
    safe_tx_gas: Literal[0] = 0
    base_gas: Literal[0] = 0
    gas_price_wei: Literal[0] = 0
    gas_token: EvmAddress = ZERO_ADDRESS
    refund_receiver: EvmAddress = ZERO_ADDRESS
    # Nonce is the Safe's replay protection counter supplied by the caller.
    nonce: Annotated[int, Field(ge=0)]


class BuiltSafeTransaction(BaseModel):
    """Carry one Safe transaction together with its domain-separated hash."""

    # Frozen strict fields keep the hash bound to the exact hashed content.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Chain ID fixes the hash to Base mainnet.
    chain_id: Literal[8453] = SAFE_CHAIN_ID
    # The domain separator actually hashed, for operator verification.
    domain_separator: Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]
    # The transaction whose content produced the hash below.
    transaction: SafeTransaction
    # EIP-712 SafeTx hash the contract will verify at execution time.
    safe_tx_hash: Annotated[str, Field(pattern=r"^0x[0-9a-f]{64}$")]


class SafeOwnerSignature(BaseModel):
    """Represent one plain ECDSA owner signature over a SafeTx hash."""

    # Frozen strict fields keep the signature bound to its signer evidence.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Recovery identifier uses the raw 27/28 Ethereum convention.
    v: Literal[27, 28]
    # R and S are the ECDSA signature integers.
    r: Annotated[int, Field(gt=0)]
    s: Annotated[int, Field(gt=0)]
    # The public address derived from the signing key; exposing only this
    # keeps key material out of every model.
    signer_address: EvmAddress
    # The 65-byte concatenated r||s||v encoding checkNSignatures consumes.
    encoded: Annotated[str, Field(pattern=r"^0x[0-9a-f]{130}$")]


class SafeSignatureValidation(BaseModel):
    """Report the live contract's read-only verdict on one signature."""

    # Frozen strict fields preserve one coherent validation outcome.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Verified is true only when the live contract accepted the signature.
    verified: bool
    # Source records the read-only RPC backend that produced the verdict.
    source: str
    # Diagnostic explains acceptance or carries the contract's revert evidence.
    diagnostic: str


def _address_word(address: str) -> bytes:
    """Encode one normalized address as its low-160-bit ABI word.

    Args:
        address: Normalized 0x-prefixed 20-byte address.

    Returns:
        The 32-byte big-endian word holding the address.
    """
    return bytes.fromhex(address[2:].rjust(64, "0"))


def _padded_length(length: int) -> int:
    """Round one byte length up to the next whole ABI word boundary.

    Args:
        length: Non-negative byte length of one dynamic value.

    Returns:
        The length rounded up to a multiple of 32, with zero staying zero.
    """
    return (length + WORD_BYTES - 1) // WORD_BYTES * WORD_BYTES


def compute_safe_domain_separator(safe_address: str, chain_id: int = SAFE_CHAIN_ID) -> str:
    """Compute the Safe's minimal EIP-712 domain separator.

    Args:
        safe_address: The Safe proxy contract whose transactions are signed.
        chain_id: The EIP-155 chain ID; only Base mainnet is supported.

    Returns:
        The 0x-prefixed 32-byte domain separator the live contract verifies.

    Raises:
        ValueError: If the address is malformed or the chain is not Base.
    """
    if chain_id != SAFE_CHAIN_ID:
        raise ValueError("safe transactions are fixed to Base mainnet (chain id 8453)")
    normalized = normalize_evm_address(safe_address)
    # abi.encode(domainTypehash, chainId, verifyingContract).
    encoded = (
        SAFE_DOMAIN_TYPEHASH + chain_id.to_bytes(WORD_BYTES, "big") + _address_word(normalized)
    )
    return "0x" + keccak(encoded).hex()


def compute_safe_tx_hash(transaction: SafeTransaction, safe_address: str) -> str:
    """Compute the EIP-712 SafeTx hash the Safe contract will verify.

    Args:
        transaction: The complete execTransaction payload including nonce.
        safe_address: The Safe proxy contract that anchors the domain.

    Returns:
        The 0x-prefixed 32-byte safeTxHash for signing and on-chain checks.
    """
    # keccak(data) is the abi.encode representation of the dynamic bytes field.
    data_hash = keccak(bytes.fromhex(transaction.data[2:]))
    # abi.encode(typehash, to, value, dataHash, operation, gas words, nonce).
    struct_hash = keccak(
        SAFE_TX_TYPEHASH
        + _address_word(transaction.to_address)
        + transaction.value_wei.to_bytes(WORD_BYTES, "big")
        + data_hash
        + transaction.operation.to_bytes(WORD_BYTES, "big")
        + transaction.safe_tx_gas.to_bytes(WORD_BYTES, "big")
        + transaction.base_gas.to_bytes(WORD_BYTES, "big")
        + transaction.gas_price_wei.to_bytes(WORD_BYTES, "big")
        + _address_word(transaction.gas_token)
        + _address_word(transaction.refund_receiver)
        + transaction.nonce.to_bytes(WORD_BYTES, "big")
    )
    domain_separator = bytes.fromhex(compute_safe_domain_separator(safe_address)[2:])
    return "0x" + keccak(EIP712_PREFIX + domain_separator + struct_hash).hex()


def build_safe_transaction(transaction: SafeTransaction, safe_address: str) -> BuiltSafeTransaction:
    """Bind one transaction to its domain-separated SafeTx hash.

    Args:
        transaction: The complete execTransaction payload including nonce.
        safe_address: The Safe proxy contract anchoring the EIP-712 domain.

    Returns:
        The immutable built transaction carrying its safeTxHash.
    """
    return BuiltSafeTransaction(
        domain_separator=compute_safe_domain_separator(safe_address),
        transaction=transaction,
        safe_tx_hash=compute_safe_tx_hash(transaction, safe_address),
    )


def ensure_usable_nonce(proposed_nonce: int, live_nonce: int) -> None:
    """Reject a nonce the live contract would consider replayed.

    Args:
        proposed_nonce: The nonce a caller wants to build with.
        live_nonce: The Safe's current on-chain nonce.

    Raises:
        StaleNonceError: If the proposed nonce is older than the live one.
    """
    if proposed_nonce < live_nonce:
        raise StaleNonceError(
            f"proposed Safe nonce {proposed_nonce} is older than the live nonce {live_nonce}; "
            "refusing to build a transaction that cannot execute"
        )


def sign_safe_tx_hash(key_bytes: bytes, safe_tx_hash: str) -> SafeOwnerSignature:
    """Sign one SafeTx hash with injected key bytes and validate recovery.

    The key is used only inside this call and never stored or logged. The
    hash is signed directly - without an EIP-191 personal-sign prefix - because
    the SafeTx hash is already EIP-712 domain separated.

    Args:
        key_bytes: Exactly 32 raw private-key bytes supplied by the caller.
        safe_tx_hash: The 0x-prefixed 32-byte SafeTx hash to sign.

    Returns:
        The owner signature plus its recovered public address.

    Raises:
        ValueError: If the key is not 32 bytes or the hash is malformed.
        SignatureMismatchError: If local recovery does not reproduce the signer.
    """
    if len(key_bytes) != WORD_BYTES:
        raise ValueError("signing key must be exactly 32 raw bytes")
    if not safe_tx_hash.startswith("0x") or len(safe_tx_hash) != 2 + 64:
        raise ValueError("safe tx hash must be 0x followed by 32 bytes")
    try:
        hash_bytes = bytes.fromhex(safe_tx_hash[2:])
    except ValueError as error:
        raise ValueError("safe tx hash must contain only hexadecimal characters") from error
    # The audited eth-account library performs the ECDSA signing operation and
    # derives the public address once; neither value is retained afterwards.
    signed = Account.unsafe_sign_hash(hash_bytes, key_bytes)
    signer_address = Account.from_key(key_bytes).address
    # Local round-trip validation proves the produced (v, r, s) recovers the
    # signer before anything is persisted or shown to an operator.
    recovered = Signature(vrs=(signed.v - 27, signed.r, signed.s))
    recovered_address = recovered.recover_public_key_from_msg_hash(hash_bytes).to_address()
    if recovered_address.lower() != signer_address.lower():
        raise SignatureMismatchError(
            f"signature recovery produced {recovered_address} instead of {signer_address}"
        )
    return SafeOwnerSignature(
        v=signed.v,
        r=signed.r,
        s=signed.s,
        signer_address=normalize_evm_address(signer_address),
        encoded="0x" + bytes(signed.signature).hex(),
    )


def build_exec_transaction_calldata(
    transaction: SafeTransaction,
    signature: SafeOwnerSignature,
) -> str:
    """ABI-encode execTransaction for the built payload and owner signature.

    Args:
        transaction: The complete execTransaction payload.
        signature: The plain ECDSA owner signature over its SafeTx hash.

    Returns:
        Complete 0x-prefixed calldata targeting the Safe contract.
    """
    # Eight static argument words precede the two dynamic offset words.
    static_words = (
        _address_word(transaction.to_address)
        + transaction.value_wei.to_bytes(WORD_BYTES, "big")
        + transaction.operation.to_bytes(WORD_BYTES, "big")
        + transaction.safe_tx_gas.to_bytes(WORD_BYTES, "big")
        + transaction.base_gas.to_bytes(WORD_BYTES, "big")
        + transaction.gas_price_wei.to_bytes(WORD_BYTES, "big")
        + _address_word(transaction.gas_token)
        + _address_word(transaction.refund_receiver)
    )
    data_bytes = bytes.fromhex(transaction.data[2:])
    signature_bytes = bytes.fromhex(signature.encoded[2:])
    # The head holds the eight static words plus the two dynamic offsets.
    data_offset = len(static_words) + 2 * WORD_BYTES
    # The signature segment follows the data length word and padded data bytes.
    signature_offset = data_offset + WORD_BYTES + _padded_length(len(data_bytes))
    head = (
        static_words
        + data_offset.to_bytes(WORD_BYTES, "big")
        + signature_offset.to_bytes(WORD_BYTES, "big")
    )
    encoded = (
        head
        + len(data_bytes).to_bytes(WORD_BYTES, "big")
        + data_bytes.ljust(_padded_length(len(data_bytes)), b"\x00")
        + len(signature_bytes).to_bytes(WORD_BYTES, "big")
        + signature_bytes
    )
    return f"0x{EXEC_TRANSACTION_SELECTOR}{encoded.hex()}"


class SafeTransactionRpcBackend:
    """Perform the bounded read-only Safe contract reads this layer requires."""

    def __init__(
        self,
        rpc_url: str,
        safe_address: str,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_attempts: int = MAX_REQUEST_ATTEMPTS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure bounded read-only Safe RPC behavior.

        Args:
            rpc_url: Base JSON-RPC endpoint used exclusively for read-only calls.
            safe_address: The Safe proxy contract whose state is read.
            timeout_seconds: Complete per-request timeout in seconds.
            max_attempts: Attempts per request before failing closed.
            max_response_bytes: Maximum accepted size of one RPC response body.
            transport: Optional injected HTTP transport for deterministic tests.
            sleep: Injected delay function used for retry backoff.

        Raises:
            ValueError: If any bound is non-positive or the address is malformed.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        self._safe_address = normalize_evm_address(safe_address)
        self._rpc_url = rpc_url
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._max_response_bytes = max_response_bytes
        self._transport = transport
        self._sleep = sleep

    @property
    def safe_address(self) -> str:
        """Return the normalized Safe proxy address this backend reads."""
        return self._safe_address

    def fetch_live_nonce(self) -> int:
        """Read the Safe's current nonce with bounded retries.

        Returns:
            The live on-chain Safe nonce.

        Raises:
            SafeTransactionUnavailableError: If the read cannot complete or
                returns a malformed value.
        """
        result = self._eth_call(f"0x{SAFE_NONCE_SELECTOR}")
        # A full 32-byte word is required; short words indicate tampered output.
        if len(result) != 2 + 64:
            raise SafeTransactionUnavailableError(
                f"Safe nonce() returned {len(result) - 2} bytes instead of 32"
            )
        return int(result[2:], 16)

    def read_domain_separator(self) -> str:
        """Read the Safe contract's live EIP-712 domain separator.

        Returns:
            The 0x-prefixed 32-byte domain separator.

        Raises:
            SafeTransactionUnavailableError: If the read cannot complete or
                returns a malformed value.
        """
        result = self._eth_call(f"0x{SAFE_DOMAIN_SEPARATOR_SELECTOR}")
        if len(result) != 2 + 64:
            raise SafeTransactionUnavailableError(
                f"Safe domainSeparator() returned {len(result) - 2} bytes instead of 32"
            )
        return result.lower()

    def validate_owner_signature(
        self,
        built: BuiltSafeTransaction,
        signature: SafeOwnerSignature,
    ) -> SafeSignatureValidation:
        """Prove one signature against the live contract without spending gas.

        The call is a read-only eth_call to checkNSignatures(safeTxHash, "0x",
        signature); a plain ECDSA signature is validated directly against the
        hash, so empty data is correct.

        Args:
            built: The built transaction whose hash the signature covers.
            signature: The owner signature being proven.

        Returns:
            The verified verdict or the contract's revert evidence.

        Raises:
            SafeTransactionUnavailableError: If the call cannot complete with
                bounded retries for a non-revert reason.
        """
        # The head holds the static hash plus two dynamic offsets (0x60 total).
        data_offset = 3 * WORD_BYTES
        # The signature section follows the empty data section's length word.
        signature_offset = data_offset + WORD_BYTES
        calldata = (
            f"0x{CHECK_N_SIGNATURES_SELECTOR}"
            + built.safe_tx_hash[2:]
            + data_offset.to_bytes(WORD_BYTES, "big").hex()
            + signature_offset.to_bytes(WORD_BYTES, "big").hex()
            + (0).to_bytes(WORD_BYTES, "big").hex()
            + (len(signature.encoded[2:]) // 2).to_bytes(WORD_BYTES, "big").hex()
            + signature.encoded[2:]
        )
        try:
            self._eth_call(calldata)
        except SafeTransactionRpcRevertError as revert:
            return SafeSignatureValidation(
                verified=False,
                source="read_only_eth_call",
                diagnostic=f"checkNSignatures rejected the signature: {revert}",
            )
        return SafeSignatureValidation(
            verified=True,
            source="read_only_eth_call",
            diagnostic="The live Safe contract accepted the signature read-only.",
        )

    def _eth_call(self, calldata: str) -> str:
        """Perform one read-only eth_call against the Safe with retries.

        Args:
            calldata: Complete 0x-prefixed call payload.

        Returns:
            The 0x-prefixed return bytes.

        Raises:
            SafeTransactionUnavailableError: If retries are exhausted or the
                response is unusable.
            SafeTransactionRpcRevertError: If the contract itself reverted.
        """
        payload = {
            "jsonrpc": "2.0",
            "id": JSON_RPC_ID,
            "method": "eth_call",
            "params": [
                {"from": READ_ONLY_CALLER, "to": self._safe_address, "data": calldata},
                "latest",
            ],
        }
        failure = "no attempt was made"
        with httpx.Client(
            timeout=self._timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
            headers={"User-Agent": "aero-bot/0.1 read-only-safe-transaction-reads"},
        ) as client:
            for attempt in range(self._max_attempts):
                if attempt > 0:
                    self._sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
                try:
                    response = client.post(self._rpc_url, json=payload)
                except httpx.TransportError as error:
                    failure = f"transport error: {error}"
                    continue
                if response.status_code == 429 or response.status_code >= 500:
                    failure = f"HTTP status {response.status_code}"
                    continue
                response_size = len(response.content)
                if response_size > self._max_response_bytes:
                    raise SafeTransactionUnavailableError(
                        f"RPC response contained {response_size} bytes, above the limit"
                    )
                if response.status_code != 200:
                    raise SafeTransactionUnavailableError(
                        f"RPC request failed with unexpected HTTP status {response.status_code}"
                    )
                try:
                    body = cast(object, response.json())
                except ValueError as error:
                    raise SafeTransactionUnavailableError(
                        "RPC response was not valid JSON"
                    ) from error
                if not isinstance(body, dict) or "result" not in body:
                    error_body = body.get("error") if isinstance(body, dict) else None
                    if not isinstance(error_body, dict):
                        raise SafeTransactionUnavailableError(
                            "RPC response had neither result nor error"
                        )
                    error_code = error_body.get("code")
                    error_message = str(error_body.get("message", ""))
                    # A contract revert is a legitimate answer, not a retry case.
                    if (
                        error_code == EXECUTION_REVERT_ERROR_CODE
                        or "revert" in error_message.lower()
                    ):
                        raise SafeTransactionRpcRevertError(f"eth_call reverted: {error_message}")
                    # Base's public endpoint reports rate limiting as retriable.
                    if error_code == RATE_LIMIT_ERROR_CODE or "rate limit" in error_message.lower():
                        failure = f"RPC error {error_code}: {error_message}"
                        continue
                    raise SafeTransactionUnavailableError(
                        f"RPC error {error_code}: {error_message}"
                    )
                return str(body["result"])
        raise SafeTransactionUnavailableError(
            f"RPC eth_call failed after {self._max_attempts} attempts: {failure}"
        )
