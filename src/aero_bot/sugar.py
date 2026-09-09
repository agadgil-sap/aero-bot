"""Read-only Aerodrome LP Sugar enumeration backend for Base pool discovery.

Aerodrome's LP Sugar contract is the venue's own complete read layer: one paginated
``all(limit, offset)`` call returns every classic and Slipstream pool with its gauge,
factory, emissions, and fee configuration in a single coherent observation.
Factory-event scanning and secondary yield feeds both silently miss pools, so this
backend is the only authoritative pool inventory for the application.
"""

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, cast

import httpx
from pydantic import BaseModel, Field

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.venues import (
    PoolCandidate,
    PoolDiscoveryBatch,
    PoolDiscoveryUnavailableError,
    PoolKind,
)

# The LP Sugar deployment pinned by sugar-sdk 0.3.1's Base chain configuration.
LP_SUGAR_ADDRESS = "0x27fc745390d1f4BaF8D184FBd97748340f786634"
# The sugar-sdk project publishes the contract sources and the vendored ABI.
LP_SUGAR_SOURCE_URL = "https://github.com/velodrome-finance/sugar"
# Base's public JSON-RPC endpoint requires no credentials for read-only eth_call use.
DEFAULT_BASE_RPC_URL = "https://mainnet.base.org"
# The vendored ABI fragment for the all() function is bundled beside this module.
LP_SUGAR_ABI_RESOURCE = "lp_sugar_abi.json"
# keccak256("all(uint256,uint256)")[0:4], verified against the live Base deployment.
ALL_FUNCTION_SELECTOR = "b10daf7b"
# The sugar contract rejects any page larger than MAX_LPS pools.
MAX_POOLS_PER_PAGE = 500
# The contract cap itself: one page enumerates five hundred pools, halving
# the request count (and so the public endpoint's rate-limit pressure) a
# full sweep pays versus smaller pages, while each bounded response stays
# far under the accepted byte ceiling.
DEFAULT_PAGE_SIZE = 500
# Twenty seconds bounds a failed request without blocking local startup indefinitely.
REQUEST_TIMEOUT_SECONDS = 20.0
# Five attempts with exponential backoff absorb public-RPC rate limiting.
MAX_REQUEST_ATTEMPTS = 5
# Backoff starts at half a second and doubles for each retry.
BASE_BACKOFF_SECONDS = 0.5
# A short politeness delay separates pagination requests on the shared public RPC.
PAGE_DELAY_SECONDS = 0.25
# Sixty-four MiB bounds a page well above the largest observed page response.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# A runaway enumeration guard fails closed instead of paging forever.
MAX_TOTAL_POOLS = 100_000
# Base's public endpoint reports rate limiting with this JSON-RPC error code.
RATE_LIMIT_ERROR_CODE = -32016
# Every eth_call word is exactly 32 bytes.
WORD_BYTES = 32
# A 20-byte EVM address occupies the low 160 bits of one ABI word.
ADDRESS_WORD_MASK = (1 << 160) - 1
# The all-zero address represents an absent contract in Sugar output.
ZERO_ADDRESS = "0x" + "0" * 40
# ABI int24 values are sign-extended to a full 256-bit two's-complement word.
SIGN_BIT = 1 << 255
WORD_MODULUS = 1 << 256
# The fixed JSON-RPC request identifier keeps read-only calls reproducible.
JSON_RPC_ID = 1

# The decoded field layout mirrors the vendored Lp struct ABI fragment exactly.
LP_FIELD_LAYOUT: tuple[tuple[str, str], ...] = (
    ("lp", "address"),
    ("symbol", "string"),
    ("decimals", "uint8"),
    ("liquidity", "uint256"),
    ("type", "int24"),
    ("tick", "int24"),
    ("sqrt_ratio", "uint160"),
    ("token0", "address"),
    ("reserve0", "uint256"),
    ("staked0", "uint256"),
    ("token1", "address"),
    ("reserve1", "uint256"),
    ("staked1", "uint256"),
    ("gauge", "address"),
    ("gauge_liquidity", "uint256"),
    ("gauge_alive", "bool"),
    ("fee", "address"),
    ("bribe", "address"),
    ("factory", "address"),
    ("emissions", "uint256"),
    ("emissions_token", "address"),
    ("pool_fee", "uint256"),
    ("unstaked_fee", "uint256"),
    ("token0_fees", "uint256"),
    ("token1_fees", "uint256"),
    ("nfpm", "address"),
    ("alm", "address"),
    ("root", "address"),
)
# The static head of one Lp tuple holds exactly one word per declared field.
LP_HEAD_WORDS = len(LP_FIELD_LAYOUT)


class LpSugarRecord(BaseModel):
    """Represent one decoded Lp tuple from the LP Sugar all() enumeration."""

    # Frozen strict fields preserve the exact onchain observation after decoding.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The pool contract address identifies the candidate on Base.
    lp_address: EvmAddress
    # The source symbol is retained exactly as reported and may be empty.
    symbol: str
    # Tick spacing is positive for Slipstream pools and 0 or -1 for classic pools.
    tick_spacing: int
    # The current pool tick is zero for classic pools.
    current_tick: int
    # The current square-root price is zero for classic pools.
    sqrt_ratio: Annotated[int, Field(ge=0)]
    # Active pool liquidity measures current concentrated onchain liquidity.
    pool_active_liquidity: Annotated[int, Field(ge=0)]
    # Token zero is read directly from the pool observation.
    token0_address: EvmAddress
    # Reserve zero is the raw token-unit pool balance.
    reserve0: Annotated[int, Field(ge=0)]
    # Staked zero is the raw token-unit balance held in gauge positions.
    staked0: Annotated[int, Field(ge=0)]
    # Token one is read directly from the pool observation.
    token1_address: EvmAddress
    # Reserve one is the raw token-unit pool balance.
    reserve1: Annotated[int, Field(ge=0)]
    # Staked one is the raw token-unit balance held in gauge positions.
    staked1: Annotated[int, Field(ge=0)]
    # A gauge is absent when Sugar reports the zero address.
    gauge_address: EvmAddress | None
    # Gauge liquidity measures the staked liquidity sharing gauge emissions.
    gauge_liquidity: Annotated[int, Field(ge=0)]
    # Gauge liveness is Aerodrome's own kill-switch state for emissions.
    gauge_alive: bool
    # The factory address identifies the deployment lineage of the pool.
    factory_address: EvmAddress
    # Emissions are the raw per-second reward rate paid by the gauge.
    emissions_per_second: Annotated[int, Field(ge=0)]
    # The emissions token is absent when the gauge is not emitting.
    emissions_token_address: EvmAddress | None
    # Pool fee is the Slipstream fee tier in parts per million.
    pool_fee_ppm: Annotated[int, Field(ge=0)]
    # Unstaked fee is the higher tier charged when liquidity is not staked.
    unstaked_fee_ppm: Annotated[int, Field(ge=0)]
    # Accumulated token-zero fees evidence the pool's fee revenue.
    token0_fees: Annotated[int, Field(ge=0)]
    # Accumulated token-one fees evidence the pool's fee revenue.
    token1_fees: Annotated[int, Field(ge=0)]
    # The NonfungiblePositionManager that mints this pool's positions; absent
    # for classic pools and always present for Slipstream pools.
    nfpm_address: EvmAddress | None


def _read_word(data: bytes, byte_offset: int) -> int:
    """Read one big-endian 32-byte ABI word.

    Args:
        data: Complete ABI-encoded response bytes.
        byte_offset: Byte position of the word start.

    Returns:
        The unsigned 256-bit word value.

    Raises:
        ValueError: If the word would read beyond the available bytes.
    """
    if byte_offset < 0 or byte_offset + WORD_BYTES > len(data):
        raise ValueError(f"ABI word at byte {byte_offset} is out of bounds")
    return int.from_bytes(data[byte_offset : byte_offset + WORD_BYTES], "big")


def _decode_signed_word(word: int) -> int:
    """Convert a sign-extended ABI word to its signed Python value.

    Args:
        word: Unsigned 256-bit word value.

    Returns:
        The two's-complement signed value.
    """
    return word if word < SIGN_BIT else word - WORD_MODULUS


def _decode_address_word(word: int) -> str:
    """Extract a lowercase EVM address from an ABI word.

    Args:
        word: Unsigned 256-bit word holding a right-padded address.

    Returns:
        The address as a 0x-prefixed lowercase 40-character string.
    """
    return "0x" + format(word & ADDRESS_WORD_MASK, "040x")


def decode_lp_page(data: bytes) -> tuple[LpSugarRecord, ...]:
    """Decode one ABI-encoded all() response into immutable Lp records.

    Args:
        data: Raw eth_call return bytes for one all(limit, offset) page.

    Returns:
        Every decoded Lp tuple in source order.

    Raises:
        ValueError: If the bytes are not a well-formed Lp array encoding.
    """
    if len(data) % WORD_BYTES != 0:
        raise ValueError("LP Sugar response is not a sequence of whole ABI words")
    # A single dynamic return value is prefixed by one offset word fixed at 0x20.
    array_offset = _read_word(data, 0)
    if array_offset != WORD_BYTES:
        raise ValueError("LP Sugar response has an unexpected outer offset")
    # The array length immediately follows the outer offset word.
    record_count = _read_word(data, array_offset)
    if record_count > MAX_POOLS_PER_PAGE:
        raise ValueError(f"LP Sugar page reported {record_count} pools above the contract cap")
    # Dynamic-array elements are located by an offset table relative to its own start.
    offsets_start = array_offset + WORD_BYTES
    records: list[LpSugarRecord] = []
    for index in range(record_count):
        element_offset = _read_word(data, offsets_start + index * WORD_BYTES)
        if element_offset % WORD_BYTES != 0:
            raise ValueError("LP Sugar element is not word aligned")
        element_start = offsets_start + element_offset
        records.append(_decode_lp_record(data, element_start))
    return tuple(records)


def _pool_kind_for_tick_spacing(tick_spacing: int) -> PoolKind:
    """Map the Sugar type field to the pool-kind enumeration.

    Args:
        tick_spacing: The signed int24 type value from one Lp tuple.

    Returns:
        The matching classic or Slipstream pool kind.

    Raises:
        ValueError: If the type value is outside the documented Sugar encoding.
    """
    if tick_spacing > 0:
        return PoolKind.SLIPSTREAM
    if tick_spacing == 0:
        return PoolKind.CLASSIC_STABLE
    if tick_spacing == -1:
        return PoolKind.CLASSIC_VOLATILE
    raise ValueError(f"LP Sugar reported an invalid pool type {tick_spacing}")


def _decode_lp_record(data: bytes, element_start: int) -> LpSugarRecord:
    """Decode one Lp tuple from its static head and string tail.

    Args:
        data: Complete ABI-encoded response bytes.
        element_start: Byte position of the tuple's static head.

    Returns:
        One immutable decoded record.

    Raises:
        ValueError: If any field or the dynamic string is malformed.
    """
    # Raw slots are collected by ABI name before typed model construction.
    slots: dict[str, int] = {}
    symbol_offset = 0
    for index, (field_name, _) in enumerate(LP_FIELD_LAYOUT):
        slot = _read_word(data, element_start + index * WORD_BYTES)
        slots[field_name] = slot
        if field_name == "symbol":
            symbol_offset = slot
    # The string offset is relative to the start of this tuple's encoding.
    string_length = _read_word(data, element_start + symbol_offset)
    string_start = element_start + symbol_offset + WORD_BYTES
    if string_start + string_length > len(data):
        raise ValueError("LP Sugar symbol string is out of bounds")
    try:
        symbol = data[string_start : string_start + string_length].decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("LP Sugar symbol was not valid UTF-8") from error
    # The int24 type field maps Slipstream tick spacing or classic pool flavor.
    tick_spacing = _decode_signed_word(slots["type"])
    _pool_kind_for_tick_spacing(tick_spacing)
    return LpSugarRecord.model_validate(
        {
            "lp_address": _decode_address_word(slots["lp"]),
            "symbol": symbol,
            "tick_spacing": tick_spacing,
            "current_tick": _decode_signed_word(slots["tick"]),
            "sqrt_ratio": slots["sqrt_ratio"],
            "pool_active_liquidity": slots["liquidity"],
            "token0_address": _decode_address_word(slots["token0"]),
            "reserve0": slots["reserve0"],
            "staked0": slots["staked0"],
            "token1_address": _decode_address_word(slots["token1"]),
            "reserve1": slots["reserve1"],
            "staked1": slots["staked1"],
            "gauge_address": _optional_address_word(slots["gauge"]),
            "gauge_liquidity": slots["gauge_liquidity"],
            "gauge_alive": bool(slots["gauge_alive"]),
            "factory_address": _decode_address_word(slots["factory"]),
            "emissions_per_second": slots["emissions"],
            "emissions_token_address": _optional_address_word(slots["emissions_token"]),
            "pool_fee_ppm": slots["pool_fee"],
            "unstaked_fee_ppm": slots["unstaked_fee"],
            "token0_fees": slots["token0_fees"],
            "token1_fees": slots["token1_fees"],
            "nfpm_address": _optional_address_word(slots["nfpm"]),
        }
    )


def _optional_address_word(word: int) -> str | None:
    """Map the zero address to an absent optional contract.

    Args:
        word: Unsigned 256-bit word holding a right-padded address.

    Returns:
        The lowercase address, or None when Sugar reported the zero address.
    """
    address = _decode_address_word(word)
    return None if address == ZERO_ADDRESS else address


class LpSugarRpcBackend:
    """Enumerate Aerodrome pools through read-only LP Sugar eth_call pagination."""

    def __init__(
        self,
        rpc_url: str = DEFAULT_BASE_RPC_URL,
        sugar_address: str = LP_SUGAR_ADDRESS,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_attempts: int = MAX_REQUEST_ATTEMPTS,
        page_delay_seconds: float = PAGE_DELAY_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        """Configure bounded read-only enumeration behavior.

        Args:
            rpc_url: Base JSON-RPC endpoint used exclusively for eth_call reads.
            sugar_address: LP Sugar contract address supplying the enumeration.
            timeout_seconds: Complete per-request timeout in seconds.
            page_size: Pools requested per all() page, at most the contract cap.
            max_attempts: Attempts per request before failing closed.
            page_delay_seconds: Politeness delay between pagination requests.
            max_response_bytes: Maximum accepted size of one RPC response body.
            transport: Optional injected HTTP transport for deterministic tests.
            sleep: Injected delay function used for backoff and politeness waits.
            progress: Optional callback receiving one human-readable line per
                enumerated page and per retried request, so a slow sweep never
                looks like a stall to the operator watching stderr.

        Raises:
            ValueError: If any bound is non-positive or above its documented cap.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if page_size <= 0 or page_size > MAX_POOLS_PER_PAGE:
            raise ValueError("page_size must be between one and the contract cap")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if page_delay_seconds < 0:
            raise ValueError("page_delay_seconds must be non-negative")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        # Address normalization rejects malformed configuration before any request.
        self._sugar_address = normalize_evm_address(sugar_address)
        self._rpc_url = rpc_url
        self._timeout_seconds = timeout_seconds
        self._page_size = page_size
        self._max_attempts = max_attempts
        self._page_delay_seconds = page_delay_seconds
        self._max_response_bytes = max_response_bytes
        # An injected transport keeps unit tests completely off the network.
        self._transport = transport
        self._sleep = sleep
        # Progress lines are pure operator feedback; None keeps the backend silent.
        self._progress = progress

    def discover(
        self,
        b20_addresses: frozenset[str],
        quote_token_address: str,
    ) -> PoolDiscoveryBatch:
        """Enumerate every Sugar pool and return official B20/quote pair candidates.

        Args:
            b20_addresses: Issuer-verified B20 contracts permitted for pairing.
            quote_token_address: Official native Base USDC contract.

        Returns:
            A source-stamped batch of pair-scoped candidates with enumeration counts.

        Raises:
            PoolDiscoveryUnavailableError: If the read-only enumeration cannot
                complete with bounded retries.
        """
        normalized_b20 = frozenset(address.lower() for address in b20_addresses)
        normalized_quote = quote_token_address.lower()
        with httpx.Client(
            timeout=self._timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
            headers={"User-Agent": "aero-bot/0.1 read-only-lp-sugar-enumeration"},
        ) as client:
            # Pinning one block keeps every page a coherent single-state snapshot.
            block_number_hex = cast(
                str,
                self._rpc_call(client, "eth_blockNumber", []),
            )
            try:
                block_number = int(block_number_hex, 16)
            except (TypeError, ValueError) as error:
                raise PoolDiscoveryUnavailableError(
                    "LP Sugar enumeration received an invalid block number"
                ) from error
            records = self._enumerate_pages(client, block_number_hex)
        # Pair scoping in the backend keeps the batch small; the adapter still
        # independently revalidates every boundary before any pool is accepted.
        candidates = tuple(
            _candidate_from_record(record)
            for record in records
            if _record_matches_pair_scope(record, normalized_b20, normalized_quote)
        )
        return PoolDiscoveryBatch(
            source=f"lp-sugar:{self._sugar_address}@block:{block_number}",
            observed_at=datetime.now(UTC),
            snapshot_block=block_number,
            candidates=candidates,
            enumerated_pool_count=len(records),
        )

    def _enumerate_pages(
        self,
        client: httpx.Client,
        block_number_hex: str,
    ) -> tuple[LpSugarRecord, ...]:
        """Page through all() until a short page signals the end of the inventory.

        Args:
            client: The bounded read-only HTTP client.
            block_number_hex: The pinned block tag shared by every page request.

        Returns:
            Every decoded Lp record across all pages in enumeration order.

        Raises:
            PoolDiscoveryUnavailableError: If pagination cannot complete or is
                unbounded.
        """
        records: list[LpSugarRecord] = []
        offset = 0
        while True:
            calldata = (
                "0x"
                + ALL_FUNCTION_SELECTOR
                + format(self._page_size, "064x")
                + format(offset, "064x")
            )
            result = cast(
                str,
                self._rpc_call(
                    client,
                    "eth_call",
                    [{"to": self._sugar_address, "data": calldata}, block_number_hex],
                ),
            )
            try:
                page = decode_lp_page(_hex_to_bytes(result))
            except ValueError as error:
                raise PoolDiscoveryUnavailableError(
                    f"LP Sugar page at offset {offset} failed decoding: {error}"
                ) from error
            records.extend(page)
            if self._progress is not None:
                self._progress(
                    f"lp sugar enumeration: {len(records)} pools enumerated "
                    f"at block {int(block_number_hex, 16)}"
                )
            if len(page) < self._page_size:
                return tuple(records)
            offset += self._page_size
            if len(records) > MAX_TOTAL_POOLS:
                raise PoolDiscoveryUnavailableError(
                    f"LP Sugar enumeration exceeded {MAX_TOTAL_POOLS} pools"
                )
            self._sleep(self._page_delay_seconds)

    def _rpc_call(self, client: httpx.Client, method: str, params: list[object]) -> object:
        """Perform one JSON-RPC request with retry and backoff on transient failures.

        Args:
            client: The bounded read-only HTTP client.
            method: The JSON-RPC method name, restricted to read-only calls.
            params: The JSON-RPC parameters for the method.

        Returns:
            The successful JSON-RPC result value.

        Raises:
            PoolDiscoveryUnavailableError: If the request keeps failing after
                bounded retries or reports a non-transient error.
        """
        payload = {"jsonrpc": "2.0", "id": JSON_RPC_ID, "method": method, "params": params}
        failure = "no attempt was made"
        for attempt in range(self._max_attempts):
            if attempt > 0:
                # Exponential backoff absorbs public-endpoint rate limiting.
                backoff = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                if self._progress is not None:
                    self._progress(
                        f"rpc {method} attempt {attempt + 1} of {self._max_attempts} "
                        f"failed ({failure}); backing off {backoff:.1f}s"
                    )
                self._sleep(backoff)
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
                raise PoolDiscoveryUnavailableError(
                    f"RPC response contained {response_size} bytes, above the configured limit"
                )
            if response.status_code != 200:
                raise PoolDiscoveryUnavailableError(
                    f"RPC request failed with unexpected HTTP status {response.status_code}"
                )
            try:
                body = cast(object, response.json())
            except ValueError as error:
                raise PoolDiscoveryUnavailableError("RPC response was not valid JSON") from error
            if not isinstance(body, dict) or "result" not in body:
                error_body = body.get("error") if isinstance(body, dict) else None
                if not isinstance(error_body, dict):
                    raise PoolDiscoveryUnavailableError("RPC response had neither result nor error")
                error_code = error_body.get("code")
                error_message = str(error_body.get("message", ""))
                # Base's public endpoint reports rate limiting as a retriable error.
                if error_code == RATE_LIMIT_ERROR_CODE or "rate limit" in error_message.lower():
                    failure = f"RPC error {error_code}: {error_message}"
                    continue
                raise PoolDiscoveryUnavailableError(f"RPC error {error_code}: {error_message}")
            return cast(object, body["result"])
        raise PoolDiscoveryUnavailableError(
            f"RPC {method} failed after {self._max_attempts} attempts: {failure}"
        )


def _hex_to_bytes(value: str) -> bytes:
    """Decode a 0x-prefixed hexadecimal RPC result into bytes.

    Args:
        value: The JSON-RPC result string.

    Returns:
        The decoded byte string.

    Raises:
        ValueError: If the value is not a 0x-prefixed even-length hex string.
    """
    if not value.startswith("0x"):
        raise ValueError("RPC result was not a 0x-prefixed hex string")
    return bytes.fromhex(value[2:])


def _record_matches_pair_scope(
    record: LpSugarRecord,
    b20_addresses: frozenset[str],
    quote_token_address: str,
) -> bool:
    """Check whether one record pairs the quote token with an official B20 token.

    Args:
        record: One decoded Lp observation.
        b20_addresses: Normalized issuer-verified B20 contracts.
        quote_token_address: Normalized native USDC contract.

    Returns:
        True when the pool pair includes the quote token and at least one B20 token.
    """
    tokens = frozenset({record.token0_address, record.token1_address})
    return quote_token_address in tokens and bool(tokens.intersection(b20_addresses))


def _candidate_from_record(record: LpSugarRecord) -> PoolCandidate:
    """Project one decoded Lp record into an untrusted pool candidate.

    Args:
        record: One decoded Lp observation.

    Returns:
        A candidate carrying every Slipstream field required by later validation.
    """
    return PoolCandidate(
        pool_kind=_pool_kind_for_tick_spacing(record.tick_spacing),
        pool_address=record.lp_address,
        factory_address=record.factory_address,
        token0_address=record.token0_address,
        token1_address=record.token1_address,
        tick_spacing=record.tick_spacing,
        current_tick=record.current_tick,
        sqrt_ratio=record.sqrt_ratio,
        pool_fee_ppm=record.pool_fee_ppm,
        unstaked_fee_ppm=record.unstaked_fee_ppm,
        reserve0=record.reserve0,
        reserve1=record.reserve1,
        staked0=record.staked0,
        staked1=record.staked1,
        gauge_address=record.gauge_address,
        gauge_liquidity=record.gauge_liquidity,
        gauge_alive=record.gauge_alive,
        emissions_per_second=record.emissions_per_second,
        emissions_token_address=record.emissions_token_address,
        pool_active_liquidity=record.pool_active_liquidity,
        nfpm_address=record.nfpm_address,
    )
