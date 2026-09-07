"""Read-only onchain Swap-event price-path reconstruction for the rehearsal harness.

Slipstream pools emit one ``Swap`` event per swap carrying the post-swap
``sqrtPriceX96`` and ``tick``, so filtering a pool's logs by the event topic
reconstructs its exact historical price path without any keyed service. Block
ranges are located from timestamps through a bounded binary search over block
headers, and every swap's timestamp comes from its own block header, keeping
the reconstructed path exact rather than interpolated.
"""

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Annotated, Self, cast

import httpx
from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address
from aero_bot.sugar import DEFAULT_BASE_RPC_URL

# Aerodrome's official Slipstream repository publishes the concentrated-pool contracts.
SLIPSTREAM_POOL_SOURCE_URL = "https://github.com/aerodrome-finance/slipstream"
# The vendored Swap-event ABI fragment is bundled beside this module.
SLIPSTREAM_POOL_ABI_RESOURCE = "slipstream_pool_abi.json"
# The canonical v3-style Swap signature emitted by every Slipstream pool.
SWAP_EVENT_SIGNATURE = "Swap(address,address,int256,int256,uint160,uint128,int24)"
# keccak256(SWAP_EVENT_SIGNATURE), computed independently and verified against
# live Base Swap logs; the identical constant governs every v3 fork.
SWAP_EVENT_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
# The non-indexed data layout mirrors the vendored Swap event fragment exactly.
SWAP_DATA_LAYOUT: tuple[tuple[str, str], ...] = (
    ("amount0", "int256"),
    ("amount1", "int256"),
    ("sqrtPriceX96", "uint160"),
    ("liquidity", "uint128"),
    ("tick", "int24"),
)
# One Swap log carries topic0, the indexed sender, and the indexed recipient.
SWAP_TOPIC_COUNT = 3
# The rehearsed history window is roughly the last three weeks.
REHEARSAL_LOOKBACK = timedelta(days=21)
# One eth_getLogs window spans at most this many blocks; public endpoints
# bound both block ranges and response sizes, so windows stay modest.
DEFAULT_LOG_WINDOW_BLOCKS = 5_000
# A window returning this many logs fails closed because truncation becomes
# indistinguishable from a complete answer at exactly this count.
DEFAULT_MAX_LOGS_PER_WINDOW = 1_000
# Block-header lookups are bounded so a pathological pool cannot page the
# shared public endpoint for hours; the rehearsal CLI raises it explicitly.
DEFAULT_MAX_BLOCK_HEADER_LOOKUPS = 4_096
# A runaway guard bounds one pool's total decoded events like discovery pagination.
MAX_TOTAL_SWAP_EVENTS = 50_000
# The binary search over block timestamps cannot outwalk this many probes.
MAX_BINARY_SEARCH_PROBES = 128
# Twenty seconds bounds a failed request without blocking the rehearsal forever.
REQUEST_TIMEOUT_SECONDS = 20.0
# Five attempts with exponential backoff absorb public-RPC rate limiting.
MAX_REQUEST_ATTEMPTS = 5
# Backoff starts at half a second and doubles for each retry.
BASE_BACKOFF_SECONDS = 0.5
# A short politeness delay separates read requests on the shared public RPC.
PAGE_DELAY_SECONDS = 0.25
# Sixty-four MiB bounds any single response well above the largest window.
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
# Base's public endpoint reports rate limiting with this JSON-RPC error code.
RATE_LIMIT_ERROR_CODE = -32016
# Every ABI word is exactly 32 bytes.
WORD_BYTES = 32
# ABI integer values are sign-extended to a full 256-bit two's-complement word.
SIGN_BIT = 1 << 255
WORD_MODULUS = 1 << 256
# The fixed JSON-RPC request identifier keeps read-only calls reproducible.
JSON_RPC_ID = 1
# High internal precision keeps raw sqrt-price arithmetic deterministic.
MATH_PRECISION = 60
# sqrtPriceX96 squares against exactly 2^192 to yield the raw price; the int
# conversion is exact, unlike an in-context Decimal power.
RAW_PRICE_SCALE = Decimal(1 << 192)
# Token decimal counts are bounded the same way oracle feed decimals are.
MAX_TOKEN_DECIMALS = 36


class SwapEventRecord(BaseModel):
    """Represent one decoded Slipstream Swap log before timestamp resolution."""

    # Frozen strict fields preserve the exact onchain event after decoding.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The block the swap was mined in locates the event on the chain.
    block_number: Annotated[int, Field(ge=0)]
    # The log index orders multiple swaps inside one block deterministically.
    log_index: Annotated[int, Field(ge=0)]
    # The post-swap square-root price is the raw price witness.
    sqrt_ratio: Annotated[int, Field(gt=0)]
    # Active liquidity after the swap feeds later depth approximations.
    liquidity: Annotated[int, Field(ge=0)]
    # The post-swap tick is the grid-rounded price witness.
    tick: int


class PoolPricePoint(BaseModel):
    """Represent one timestamped price observation reconstructed from a swap."""

    # Frozen strict fields keep each reconstructed point immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The block-header timestamp is the event's exact wall-clock instant.
    timestamp: datetime
    # Block number and log index preserve the onchain ordering evidence.
    block_number: Annotated[int, Field(ge=0)]
    log_index: Annotated[int, Field(ge=0)]
    # The raw post-swap square-root price witness.
    sqrt_ratio: Annotated[int, Field(gt=0)]
    # Active liquidity after the swap.
    liquidity: Annotated[int, Field(ge=0)]
    # The post-swap tick.
    tick: int
    # Price is the stock token's price in USDC per one whole stock token.
    price_usdc: Annotated[Decimal, Field(gt=0)]

    @model_validator(mode="after")
    def require_aware_timestamp(self) -> Self:
        """Reject naive timestamps so all replay wait and cooldown math is absolute."""
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return self


class PoolPricePath(BaseModel):
    """Collect one pool's reconstructed swap-driven price path with provenance."""

    # Frozen strict fields keep one reconstruction stable for a whole replay.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The Slipstream pool whose Swap events were reconstructed.
    pool_address: EvmAddress
    # The B20 stock token paired with native USDC in that pool.
    token_address: EvmAddress
    # True when the stock token sorts before USDC as the pool's token0.
    token_is_token0: bool
    # The stock token's decimal count used to scale raw prices.
    token_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]
    # The quote token's decimal count used to scale raw prices.
    quote_decimals: Annotated[int, Field(ge=0, le=MAX_TOKEN_DECIMALS)]
    # The first block included in the reconstruction window.
    from_block: Annotated[int, Field(ge=0)]
    # The last block included in the reconstruction window.
    to_block: Annotated[int, Field(ge=0)]
    # The instant the reconstruction was performed, for ledger provenance.
    observed_at: datetime
    # Points are ordered by block number then log index, exactly as mined.
    points: tuple[PoolPricePoint, ...]

    @model_validator(mode="after")
    def require_ordered_window(self) -> Self:
        """Reject inverted block windows and naive observation times."""
        if self.from_block > self.to_block:
            raise ValueError("from_block must not exceed to_block")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        return self


class SwapHistoryUnavailableError(RuntimeError):
    """Signal that a read-only history reconstruction could not complete."""


def decode_swap_log(log: object) -> SwapEventRecord:
    """Decode and validate one raw eth_getLogs entry as a Slipstream Swap event.

    Args:
        log: One JSON-RPC log object with address, topics, data, and block fields.

    Returns:
        The immutable decoded event with its onchain ordering evidence.

    Raises:
        ValueError: If the log is not a well-formed non-removed Swap event.
    """
    if not isinstance(log, dict):
        raise ValueError("Swap log entry was not a JSON object")
    if log.get("removed") is True:
        # A reorged log is corrupt evidence for a historical reconstruction.
        raise ValueError("Swap log was removed by a reorg")
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != SWAP_TOPIC_COUNT:
        raise ValueError("Swap log must carry exactly three topics")
    if not isinstance(topics[0], str) or topics[0].lower() != SWAP_EVENT_TOPIC0:
        raise ValueError("Swap log topic0 does not match the Slipstream Swap event")
    data = log.get("data")
    if not isinstance(data, str) or not data.startswith("0x"):
        raise ValueError("Swap log data was not a 0x-prefixed hex string")
    try:
        data_bytes = bytes.fromhex(data[2:])
    except ValueError as error:
        raise ValueError("Swap log data was not valid hexadecimal") from error
    if len(data_bytes) != len(SWAP_DATA_LAYOUT) * WORD_BYTES:
        raise ValueError("Swap log data does not hold exactly five ABI words")
    block_number = _parse_hex_field(log.get("blockNumber"), "blockNumber")
    log_index = _parse_hex_field(log.get("logIndex"), "logIndex")
    # The data words are read positionally per the vendored event layout.
    sqrt_ratio = int.from_bytes(data_bytes[2 * WORD_BYTES : 3 * WORD_BYTES], "big")
    liquidity = int.from_bytes(data_bytes[3 * WORD_BYTES : 4 * WORD_BYTES], "big")
    tick_word = int.from_bytes(data_bytes[4 * WORD_BYTES : 5 * WORD_BYTES], "big")
    tick = tick_word if tick_word < SIGN_BIT else tick_word - WORD_MODULUS
    if sqrt_ratio <= 0:
        raise ValueError("Swap log reported a non-positive sqrtPriceX96")
    return SwapEventRecord(
        block_number=block_number,
        log_index=log_index,
        sqrt_ratio=sqrt_ratio,
        liquidity=liquidity,
        tick=tick,
    )


def price_usdc_per_stock(
    sqrt_ratio: int,
    stock_is_token0: bool,
    stock_decimals: int,
    quote_decimals: int,
) -> Decimal:
    """Convert one raw sqrtPriceX96 into a USDC-per-stock human price.

    The squared raw ratio is token1 raw units per token0 raw unit; applying
    the two tokens' decimal scales yields the human token1-per-token0 price,
    which is already USDC per stock when the stock is token0 and needs a
    reciprocal otherwise.

    Args:
        sqrt_ratio: Positive raw square-root price from a pool observation.
        stock_is_token0: True when the stock token sorts before the quote token.
        stock_decimals: Decimal count of the stock token contract.
        quote_decimals: Decimal count of the USDC quote token.

    Returns:
        The exact USDC price of one whole stock token.

    Raises:
        ValueError: If the sqrt ratio is not positive or decimals are out of range.
    """
    if sqrt_ratio <= 0:
        raise ValueError("sqrt_ratio must be positive")
    if not 0 <= stock_decimals <= MAX_TOKEN_DECIMALS:
        raise ValueError("stock_decimals is out of the documented range")
    if not 0 <= quote_decimals <= MAX_TOKEN_DECIMALS:
        raise ValueError("quote_decimals is out of the documented range")
    with localcontext() as decimal_context:
        # Local precision isolates deterministic raw-price math from settings.
        decimal_context.prec = MATH_PRECISION
        # The raw price is the squared ratio over 2^192 in raw token units.
        raw_price = Decimal(sqrt_ratio) ** 2 / RAW_PRICE_SCALE
        if stock_is_token0:
            # Token1 is USDC, so the scaled human price is already USDC per stock.
            return +(raw_price * Decimal(10) ** (stock_decimals - quote_decimals))
        # Token0 is USDC, so the scaled human price is stock per USDC.
        human_stock_per_usdc = raw_price * Decimal(10) ** (quote_decimals - stock_decimals)
        return +(Decimal(1) / human_stock_per_usdc)


def build_price_path(
    pool_address: str,
    token_address: str,
    token0_address: str,
    token1_address: str,
    token_decimals: int,
    quote_decimals: int,
    from_block: int,
    to_block: int,
    observed_at: datetime,
    records: tuple[SwapEventRecord, ...],
    timestamps_by_block: Mapping[int, datetime],
) -> PoolPricePath:
    """Attach block timestamps and prices to decoded swaps as one ordered path.

    Args:
        pool_address: Slipstream pool the swaps belong to.
        token_address: B20 stock token paired with USDC in that pool.
        token0_address: The pool's token0 contract address.
        token1_address: The pool's token1 contract address.
        token_decimals: Decimal count of the stock token.
        quote_decimals: Decimal count of the USDC quote token.
        from_block: First block of the reconstruction window.
        to_block: Last block of the reconstruction window.
        observed_at: Aware instant the reconstruction was performed.
        records: Decoded Swap events from the window.
        timestamps_by_block: Aware block-header timestamps for every record block.

    Returns:
        The immutable price path ordered by block number then log index.

    Raises:
        ValueError: If the token is not one of the pool's two tokens, a record
            block lacks a timestamp, or any point fails validation.
    """
    normalized_token = normalize_evm_address(token_address)
    token0 = normalize_evm_address(token0_address)
    token1 = normalize_evm_address(token1_address)
    if normalized_token == token0:
        stock_is_token0 = True
    elif normalized_token == token1:
        stock_is_token0 = False
    else:
        raise ValueError("token_address must be one of the pool's two tokens")
    points: list[PoolPricePoint] = []
    for record in sorted(records, key=lambda item: (item.block_number, item.log_index)):
        timestamp = timestamps_by_block.get(record.block_number)
        if timestamp is None:
            raise ValueError(f"block {record.block_number} lacks a header timestamp")
        points.append(
            PoolPricePoint(
                timestamp=timestamp,
                block_number=record.block_number,
                log_index=record.log_index,
                sqrt_ratio=record.sqrt_ratio,
                liquidity=record.liquidity,
                tick=record.tick,
                price_usdc=price_usdc_per_stock(
                    record.sqrt_ratio, stock_is_token0, token_decimals, quote_decimals
                ),
            )
        )
    return PoolPricePath(
        pool_address=pool_address,
        token_address=normalized_token,
        token_is_token0=stock_is_token0,
        token_decimals=token_decimals,
        quote_decimals=quote_decimals,
        from_block=from_block,
        to_block=to_block,
        observed_at=observed_at,
        points=tuple(points),
    )


class BlockHeader(BaseModel):
    """Represent one block's number and timestamp from a header read."""

    # Frozen strict fields preserve the header evidence exactly as served.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The block number locates the header on the chain.
    number: Annotated[int, Field(ge=0)]
    # The timestamp is the block's exact UTC mining instant.
    timestamp: datetime

    @model_validator(mode="after")
    def require_aware_timestamp(self) -> Self:
        """Reject naive timestamps so binary-search comparisons stay absolute."""
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return self


def _parse_hex_field(value: object, field_name: str) -> int:
    """Parse one non-negative hexadecimal log field.

    Args:
        value: The raw JSON-RPC field value.
        field_name: Field name used in the fail-closed diagnostic.

    Returns:
        The parsed non-negative integer; JSON-RPC quantities are unsigned by
        specification, and the 0x-prefix requirement makes negatives unparseable.

    Raises:
        ValueError: If the field is missing or malformed.
    """
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError(f"Swap log field {field_name} was not a 0x-prefixed hex string")
    try:
        return int(value, 16)
    except ValueError as error:
        raise ValueError(f"Swap log field {field_name} was not valid hexadecimal") from error


def _block_header_from_result(result: object) -> BlockHeader:
    """Validate one eth_getBlockByNumber result into a header record.

    Args:
        result: The JSON-RPC result object for one block header read.

    Returns:
        The immutable block header.

    Raises:
        ValueError: If the result is absent or lacks parseable number and timestamp.
    """
    if not isinstance(result, dict):
        raise ValueError("block header result was absent or not an object")
    number = _parse_hex_field(result.get("number"), "number")
    timestamp_seconds = _parse_hex_field(result.get("timestamp"), "timestamp")
    return BlockHeader(
        number=number,
        timestamp=datetime.fromtimestamp(timestamp_seconds, tz=UTC),
    )


class SwapHistoryRpcBackend:
    """Reconstruct pool price paths through read-only eth_getLogs and header reads."""

    def __init__(
        self,
        rpc_url: str = DEFAULT_BASE_RPC_URL,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_attempts: int = MAX_REQUEST_ATTEMPTS,
        page_delay_seconds: float = PAGE_DELAY_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        log_window_blocks: int = DEFAULT_LOG_WINDOW_BLOCKS,
        max_logs_per_window: int = DEFAULT_MAX_LOGS_PER_WINDOW,
        max_block_header_lookups: int = DEFAULT_MAX_BLOCK_HEADER_LOOKUPS,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Configure bounded read-only history reconstruction behavior.

        Args:
            rpc_url: Base JSON-RPC endpoint used exclusively for read-only calls.
            timeout_seconds: Complete per-request timeout in seconds.
            max_attempts: Attempts per request before failing closed.
            page_delay_seconds: Politeness delay between read requests.
            max_response_bytes: Maximum accepted size of one RPC response body.
            log_window_blocks: Maximum block span of one eth_getLogs window.
            max_logs_per_window: Log count at which one window fails closed.
            max_block_header_lookups: Unique block headers one reconstruction
                may fetch before failing closed.
            transport: Optional injected HTTP transport for deterministic tests.
            sleep: Injected delay function used for backoff and politeness waits.

        Raises:
            ValueError: If any bound is non-positive.
        """
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if page_delay_seconds < 0:
            raise ValueError("page_delay_seconds must be non-negative")
        if max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be positive")
        if log_window_blocks <= 0:
            raise ValueError("log_window_blocks must be positive")
        if max_logs_per_window <= 0:
            raise ValueError("max_logs_per_window must be positive")
        if max_block_header_lookups <= 0:
            raise ValueError("max_block_header_lookups must be positive")
        self._rpc_url = rpc_url
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._page_delay_seconds = page_delay_seconds
        self._max_response_bytes = max_response_bytes
        self._log_window_blocks = log_window_blocks
        self._max_logs_per_window = max_logs_per_window
        self._max_block_header_lookups = max_block_header_lookups
        # An injected transport keeps unit tests completely off the network.
        self._transport = transport
        self._sleep = sleep

    def fetch_price_path(
        self,
        pool_address: str,
        token_address: str,
        token0_address: str,
        token1_address: str,
        token_decimals: int,
        quote_decimals: int,
        lookback: timedelta = REHEARSAL_LOOKBACK,
    ) -> PoolPricePath:
        """Reconstruct one pool's swap-driven price path over a lookback window.

        The window's start block is located by a bounded binary search over
        block-header timestamps, swap logs are paged through bounded
        eth_getLogs windows filtered to the pool and the Swap topic, and each
        event's timestamp comes from its own block header.

        Args:
            pool_address: Slipstream pool whose Swap events are reconstructed.
            token_address: B20 stock token paired with USDC in that pool.
            token0_address: The pool's token0 contract address.
            token1_address: The pool's token1 contract address.
            token_decimals: Decimal count of the stock token.
            quote_decimals: Decimal count of the USDC quote token.
            lookback: How far back from the chain head to reconstruct.

        Returns:
            The immutable ordered price path, empty when the pool saw no swaps.

        Raises:
            SwapHistoryUnavailableError: If any read cannot complete with
                bounded retries, any response violates its bounds, or any
                decoded evidence is malformed.
        """
        if lookback <= timedelta(0):
            raise ValueError("lookback must be positive")
        normalized_pool = normalize_evm_address(pool_address)
        with httpx.Client(
            timeout=self._timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
            headers={"User-Agent": "aero-bot/0.1 read-only-swap-history"},
        ) as client:
            # One header cache serves the search, the windows, and the events.
            headers: dict[int, BlockHeader] = {}
            latest_number = self._latest_block_number(client)
            latest_header = self._block_header(client, latest_number, headers)
            # The window opens at the first block at or after the target instant.
            target_timestamp = latest_header.timestamp - lookback
            start_block = self._first_block_at_or_after(
                client, target_timestamp, latest_number, headers
            )
            records = self._collect_swap_records(
                client, normalized_pool, start_block, latest_number
            )
            # Every event block needs its header once; the cache deduplicates and
            # the cumulative lookup bound inside the reader fails closed.
            unique_blocks = {record.block_number for record in records}
            for block_number in sorted(unique_blocks):
                self._block_header(client, block_number, headers)
            return build_price_path(
                pool_address=normalized_pool,
                token_address=token_address,
                token0_address=token0_address,
                token1_address=token1_address,
                token_decimals=token_decimals,
                quote_decimals=quote_decimals,
                from_block=start_block,
                to_block=latest_number,
                observed_at=datetime.now(UTC),
                records=records,
                timestamps_by_block={
                    number: header.timestamp for number, header in headers.items()
                },
            )

    def _latest_block_number(self, client: httpx.Client) -> int:
        """Read the chain head block number.

        Args:
            client: The bounded read-only HTTP client.

        Returns:
            The latest mined block number.

        Raises:
            SwapHistoryUnavailableError: If the read fails or returns garbage.
        """
        result = self._rpc_call(client, "eth_blockNumber", [])
        if not isinstance(result, str):
            raise SwapHistoryUnavailableError("block number read did not return a string")
        try:
            return int(result, 16)
        except ValueError as error:
            raise SwapHistoryUnavailableError("block number read returned invalid hex") from error

    def _block_header(
        self,
        client: httpx.Client,
        block_number: int,
        headers: dict[int, BlockHeader],
    ) -> BlockHeader:
        """Read or return one cached block header with a politeness delay.

        Args:
            client: The bounded read-only HTTP client.
            block_number: Block whose header is required.
            headers: Cache shared across one reconstruction.

        Returns:
            The immutable block header.

        Raises:
            SwapHistoryUnavailableError: If the header read fails or is malformed.
        """
        cached = headers.get(block_number)
        if cached is not None:
            return cached
        if len(headers) >= self._max_block_header_lookups:
            raise SwapHistoryUnavailableError(
                f"Block header lookups exceeded the {self._max_block_header_lookups} bound."
            )
        # The politeness delay also applies to header reads on the shared RPC.
        self._sleep(self._page_delay_seconds)
        result = self._rpc_call(client, "eth_getBlockByNumber", [hex(block_number), False])
        try:
            header = _block_header_from_result(result)
        except ValueError as error:
            raise SwapHistoryUnavailableError(
                f"Block {block_number} header was malformed: {error}"
            ) from error
        if header.number != block_number:
            raise SwapHistoryUnavailableError(
                f"Block header read for {block_number} returned block {header.number}"
            )
        headers[block_number] = header
        return header

    def _first_block_at_or_after(
        self,
        client: httpx.Client,
        target_timestamp: datetime,
        latest_number: int,
        headers: dict[int, BlockHeader],
    ) -> int:
        """Binary-search the first block whose timestamp reaches a target.

        Block timestamps are non-decreasing in block number, so a bounded
        binary search over header reads locates the exact window boundary.

        Args:
            client: The bounded read-only HTTP client.
            target_timestamp: Earliest timestamp the window may start from.
            latest_number: Chain head block number closing the search space.
            headers: Cache shared across one reconstruction.

        Returns:
            The smallest block number whose timestamp is at or after the target.

        Raises:
            SwapHistoryUnavailableError: If the search exceeds its probe bound
                or block timestamps are not monotone.
        """
        low = 0
        high = latest_number
        probes = 0
        while low < high:
            probes += 1
            if probes > MAX_BINARY_SEARCH_PROBES:
                raise SwapHistoryUnavailableError(
                    "Block-timestamp binary search exceeded its probe bound."
                )
            middle = (low + high) // 2
            header = self._block_header(client, middle, headers)
            if header.timestamp >= target_timestamp:
                high = middle
            else:
                low = middle + 1
        return low

    def _collect_swap_records(
        self,
        client: httpx.Client,
        pool_address: str,
        start_block: int,
        end_block: int,
    ) -> tuple[SwapEventRecord, ...]:
        """Page the pool's Swap logs through bounded eth_getLogs windows.

        Args:
            client: The bounded read-only HTTP client.
            pool_address: Normalized Slipstream pool address filter.
            start_block: First block of the reconstruction window.
            end_block: Last block of the reconstruction window.

        Returns:
            Every decoded Swap event ordered by block number then log index.

        Raises:
            SwapHistoryUnavailableError: If a window read fails, hits its log
                bound, or returns malformed event data.
        """
        records: list[SwapEventRecord] = []
        window_start = start_block
        while True:
            window_end = min(window_start + self._log_window_blocks - 1, end_block)
            result = self._rpc_call(
                client,
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(window_start),
                        "toBlock": hex(window_end),
                        "address": pool_address,
                        "topics": [SWAP_EVENT_TOPIC0],
                    }
                ],
            )
            if not isinstance(result, list):
                raise SwapHistoryUnavailableError("eth_getLogs result was not a list")
            if len(result) >= self._max_logs_per_window:
                raise SwapHistoryUnavailableError(
                    f"eth_getLogs window {window_start}..{window_end} returned "
                    f"{len(result)} logs at or above the "
                    f"{self._max_logs_per_window} window bound."
                )
            for log in result:
                try:
                    record = decode_swap_log(log)
                except ValueError as error:
                    raise SwapHistoryUnavailableError(
                        f"eth_getLogs window {window_start}..{window_end} held a "
                        f"malformed Swap log: {error}"
                    ) from error
                # The response address must match the requested pool filter.
                if isinstance(log, dict) and str(log.get("address", "")).lower() != pool_address:
                    raise SwapHistoryUnavailableError(
                        f"eth_getLogs window {window_start}..{window_end} returned a "
                        "log from another pool."
                    )
                records.append(record)
            if len(records) > MAX_TOTAL_SWAP_EVENTS:
                raise SwapHistoryUnavailableError(
                    f"Pool {pool_address} exceeded {MAX_TOTAL_SWAP_EVENTS} decoded swap events."
                )
            if window_end >= end_block:
                break
            window_start = window_end + 1
            # A politeness delay separates window reads on the shared RPC.
            self._sleep(self._page_delay_seconds)
        return tuple(sorted(records, key=lambda item: (item.block_number, item.log_index)))

    def _rpc_call(self, client: httpx.Client, method: str, params: list[object]) -> object:
        """Perform one JSON-RPC request with retry and backoff on transient failures.

        Args:
            client: The bounded read-only HTTP client.
            method: The JSON-RPC method name, restricted to read-only calls.
            params: The JSON-RPC parameters for the method.

        Returns:
            The successful JSON-RPC result value.

        Raises:
            SwapHistoryUnavailableError: If the request keeps failing after
                bounded retries or reports a non-transient error.
        """
        payload = {"jsonrpc": "2.0", "id": JSON_RPC_ID, "method": method, "params": params}
        failure = "no attempt was made"
        for attempt in range(self._max_attempts):
            if attempt > 0:
                # Exponential backoff absorbs public-endpoint rate limiting.
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
                raise SwapHistoryUnavailableError(
                    f"RPC response contained {response_size} bytes, above the configured limit"
                )
            if response.status_code != 200:
                raise SwapHistoryUnavailableError(
                    f"RPC request failed with unexpected HTTP status {response.status_code}"
                )
            try:
                body = cast(object, response.json())
            except ValueError as error:
                raise SwapHistoryUnavailableError("RPC response was not valid JSON") from error
            if not isinstance(body, dict) or "result" not in body:
                error_body = body.get("error") if isinstance(body, dict) else None
                if not isinstance(error_body, dict):
                    raise SwapHistoryUnavailableError("RPC response had neither result nor error")
                error_code = error_body.get("code")
                error_message = str(error_body.get("message", ""))
                # Base's public endpoint reports rate limiting as a retriable error.
                if error_code == RATE_LIMIT_ERROR_CODE or "rate limit" in error_message.lower():
                    failure = f"RPC error {error_code}: {error_message}"
                    continue
                raise SwapHistoryUnavailableError(f"RPC error {error_code}: {error_message}")
            return cast(object, body["result"])
        raise SwapHistoryUnavailableError(
            f"RPC {method} failed after {self._max_attempts} attempts: {failure}"
        )
