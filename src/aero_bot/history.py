"""Read-only onchain event-history reconstruction for the rehearsal harness.

Two histories are reconstructed from raw logs without any keyed service:
Slipstream pools emit one ``Swap`` event per swap carrying the post-swap
``sqrtPriceX96`` and ``tick``, so filtering a pool's logs by the event topic
reconstructs its exact historical price path, and Slipstream gauges emit
``Deposit`` and ``Withdraw`` events whose indexed liquidity deltas fold into
the emissions-APR series the dilution gate replays against. Block ranges are
located from timestamps through a bounded binary search over block headers,
and every event's timestamp comes from its own block header, keeping the
reconstructed series exact rather than interpolated. Because staked positions
can also change liquidity through the position manager while the gauge holds
them, a stake-event fold that contradicts its anchor falls back to a clearly
labeled constant-anchor APR instead of fabricating a series.
"""

import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Annotated, Literal, Protocol, Self, TypeVar, cast

import httpx
from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import (
    IMMUTABLE_MODEL_CONFIG,
    EvmAddress,
    NonNegativeDecimal,
    normalize_evm_address,
)
from aero_bot.sugar import DEFAULT_BASE_RPC_URL

# Aerodrome's official Slipstream repository publishes the concentrated-pool contracts.
SLIPSTREAM_POOL_SOURCE_URL = "https://github.com/aerodrome-finance/slipstream"
# The vendored Swap-event ABI fragment is bundled beside this module.
SLIPSTREAM_POOL_ABI_RESOURCE = "slipstream_pool_abi.json"
# The vendored CLGauge event ABI fragment is bundled beside this module.
SLIPSTREAM_GAUGE_ABI_RESOURCE = "slipstream_gauge_abi.json"
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
# The canonical CLGauge staking events; every parameter is indexed, so each
# log's data section is empty and the liquidity delta lives in the last topic.
GAUGE_DEPOSIT_EVENT_SIGNATURE = "Deposit(address,uint256,uint128)"
# keccak256(GAUGE_DEPOSIT_EVENT_SIGNATURE), computed independently and matched
# against the live AAPLc/USDC gauge where it dominates every other topic.
GAUGE_DEPOSIT_TOPIC0 = "0x1c8ab8c7f45390d58f58f1d655213a82cca5d12179761a87c16f098813b8f211"
GAUGE_WITHDRAW_EVENT_SIGNATURE = "Withdraw(address,uint256,uint128)"
# keccak256(GAUGE_WITHDRAW_EVENT_SIGNATURE), verified against the same gauge.
GAUGE_WITHDRAW_TOPIC0 = "0x8903a5b5d08a841e7f68438387f1da20c84dea756379ed37e633ff3854b99b84"
# topic0 plus the indexed user, token id, and liquidity fill all four slots.
GAUGE_STAKE_TOPIC_COUNT = 4
# The indexed topic layout mirrors the vendored gauge event fragments exactly.
GAUGE_STAKE_TOPICS_LAYOUT: tuple[tuple[str, str], ...] = (
    ("user", "address"),
    ("tokenId", "uint256"),
    ("liquidityToStake", "uint128"),
)
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
# A runaway guard bounds one pool's total decoded events like discovery
# pagination. The busiest B20 pool (AAPLc) reconstructs roughly 3,600 swaps a
# day, so a 21-day window needs about 76,000 events; the bound sits near twice
# that need so even a busier pool reconstructs at the locked lookback.
MAX_TOTAL_SWAP_EVENTS = 150_000
# A runaway guard bounds one gauge's decoded stake events the same way; stake
# events run orders of magnitude sparser than swaps, so it stays tighter.
MAX_TOTAL_GAUGE_STAKE_EVENTS = 50_000
# Base's public endpoint serves at most ten JSON-RPC calls per batch request,
# verified live; larger batches fail with error -32014 "maximum 10 calls in
# 1 batch", so the batched header reader never exceeds this cap.
MAX_HEADER_BATCH_SIZE = 10
# keccak256("decimals()")[0:4], the ubiquitous standard ERC20 metadata
# selector shared by every token in this protocol.
ERC20_DECIMALS_SELECTOR = "0x313ce567"
# AERO distributes 18-decimal rewards exactly like the rest of the protocol.
AERO_DECIMALS = 18
# A fixed 365-day year matches the policy engine's annualization convention.
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
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
    # The signed token-zero delta; positive means the pool received token zero.
    amount0: int
    # The signed token-one delta; positive means the pool received token one.
    amount1: int
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
    # The signed token-zero delta of this swap, exactly as emitted.
    amount0: int
    # The signed token-one delta of this swap, exactly as emitted.
    amount1: int
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


class GaugeStakeEventRecord(BaseModel):
    """Represent one decoded CLGauge Deposit or Withdraw log before resolution."""

    # Frozen strict fields preserve the exact onchain event after decoding.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The block the stake event was mined in locates the event on the chain.
    block_number: Annotated[int, Field(ge=0)]
    # The log index orders multiple stake events inside one block deterministically.
    log_index: Annotated[int, Field(ge=0)]
    # True for Deposit, which adds staked liquidity, and False for Withdraw.
    is_deposit: bool
    # The indexed liquidity delta; zero occurs when a withdraw follows an
    # earlier decreaseStakedLiquidity that already removed everything.
    liquidity: Annotated[int, Field(ge=0)]


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


class EmissionsAprPoint(BaseModel):
    """Represent one step of a pool's reconstructed emissions-APR series."""

    # Frozen strict fields keep each reconstructed step immutable.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The instant this step's liquidity and APR took effect.
    timestamp: datetime
    # Onchain ordering evidence is absent only on the window-opening step.
    block_number: Annotated[int, Field(ge=0)] | None = None
    log_index: Annotated[int, Field(ge=0)] | None = None
    # Gauge staked liquidity in effect from this instant onward.
    gauge_liquidity: Annotated[int, Field(ge=0)]
    # Raw AERO emissions APR per staked liquidity in Aerodrome's display
    # convention, derived under this module's documented assumptions.
    emissions_apr: NonNegativeDecimal

    @model_validator(mode="after")
    def require_complete_ordering_evidence(self) -> Self:
        """Reject naive timestamps and half-present onchain ordering evidence."""
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        if (self.block_number is None) != (self.log_index is None):
            raise ValueError("block_number and log_index must be present together")
        return self


class EmissionsAprHistory(BaseModel):
    """Collect one pool's reconstructed emissions-APR series with provenance."""

    # Frozen strict fields keep one reconstruction stable for a whole replay.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The Slipstream pool whose gauge emissions the series describes.
    pool_address: EvmAddress
    # The CLGauge whose Deposit and Withdraw events were reconstructed.
    gauge_address: EvmAddress
    # The first block included in the reconstruction window.
    from_block: Annotated[int, Field(ge=0)]
    # The last block included in the reconstruction window.
    to_block: Annotated[int, Field(ge=0)]
    # The Sugar snapshot block that reported the anchor state; it pins the
    # window's end so the backward liquidity fold closes exactly.
    anchor_block: Annotated[int, Field(ge=0)]
    # The instant the reconstruction was performed, for ledger provenance.
    observed_at: datetime
    # Staked gauge liquidity at the anchor block, exactly as Sugar reported.
    anchor_gauge_liquidity: Annotated[int, Field(gt=0)]
    # Staked value in USDC at the anchor, valued under this module's convention.
    anchor_staked_tvl_usd: Annotated[Decimal, Field(gt=0)]
    # The gauge's raw AERO reward rate per second, held constant across the
    # window because Aerodrome resets it only at weekly epochs.
    emissions_per_second: Annotated[int, Field(ge=0)]
    # The documented AERO price assumption behind every APR in this series.
    aero_price_assumption_usd: Annotated[Decimal, Field(gt=0)]
    # The reconstruction method behind this series: event_fold stake events
    # closed on the anchor exactly, while constant_anchor_apr fell back to the
    # documented anchor-level APR because the events contradicted the anchor
    # (staked positions can also change liquidity through the position
    # manager while the gauge holds them, which stake events do not carry).
    reconstruction_mode: Literal["event_fold", "constant_anchor_apr"]
    # Event-fold liquidity before the window's first stake event; under the
    # constant fallback it equals the anchor because no level was derivable.
    starting_gauge_liquidity: Annotated[int, Field(ge=0)]
    # Steps ordered by time, opening with the window-start liquidity step.
    steps: Annotated[tuple[EmissionsAprPoint, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def require_ordered_window(self) -> Self:
        """Reject inverted windows, a displaced anchor, or unordered steps."""
        if self.from_block > self.to_block:
            raise ValueError("from_block must not exceed to_block")
        if not self.from_block <= self.anchor_block <= self.to_block:
            raise ValueError("anchor_block must lie inside the window")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.reconstruction_mode == "constant_anchor_apr" and len(self.steps) != 1:
            raise ValueError("the constant-anchor fallback must hold exactly one step")
        timestamps = [step.timestamp for step in self.steps]
        # The pairwise zip is intentionally one element shorter on the right.
        if any(later < earlier for earlier, later in zip(timestamps, timestamps[1:], strict=False)):
            raise ValueError("steps must be ordered by non-decreasing timestamp")
        return self


class HistoryUnavailableError(RuntimeError):
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
    amount0_word = int.from_bytes(data_bytes[0:WORD_BYTES], "big")
    amount1_word = int.from_bytes(data_bytes[WORD_BYTES : 2 * WORD_BYTES], "big")
    sqrt_ratio = int.from_bytes(data_bytes[2 * WORD_BYTES : 3 * WORD_BYTES], "big")
    liquidity = int.from_bytes(data_bytes[3 * WORD_BYTES : 4 * WORD_BYTES], "big")
    tick_word = int.from_bytes(data_bytes[4 * WORD_BYTES : 5 * WORD_BYTES], "big")
    # Amounts and tick are sign-extended two's-complement words.
    amount0 = amount0_word if amount0_word < SIGN_BIT else amount0_word - WORD_MODULUS
    amount1 = amount1_word if amount1_word < SIGN_BIT else amount1_word - WORD_MODULUS
    tick = tick_word if tick_word < SIGN_BIT else tick_word - WORD_MODULUS
    if sqrt_ratio <= 0:
        raise ValueError("Swap log reported a non-positive sqrtPriceX96")
    return SwapEventRecord(
        block_number=block_number,
        log_index=log_index,
        amount0=amount0,
        amount1=amount1,
        sqrt_ratio=sqrt_ratio,
        liquidity=liquidity,
        tick=tick,
    )


def decode_gauge_stake_log(log: object) -> GaugeStakeEventRecord:
    """Decode and validate one raw eth_getLogs entry as a CLGauge stake event.

    Args:
        log: One JSON-RPC log object with address, topics, data, and block fields.

    Returns:
        The immutable decoded event carrying its liquidity direction and delta.

    Raises:
        ValueError: If the log is not a well-formed non-removed Deposit or
            Withdraw event.
    """
    if not isinstance(log, dict):
        raise ValueError("Gauge stake log entry was not a JSON object")
    if log.get("removed") is True:
        # A reorged log is corrupt evidence for a historical reconstruction.
        raise ValueError("Gauge stake log was removed by a reorg")
    topics = log.get("topics")
    if not isinstance(topics, list) or len(topics) != GAUGE_STAKE_TOPIC_COUNT:
        raise ValueError("Gauge stake log must carry exactly four topics")
    topic0 = topics[0]
    if not isinstance(topic0, str):
        raise ValueError("Gauge stake log topic0 was not a string")
    normalized_topic0 = topic0.lower()
    if normalized_topic0 == GAUGE_DEPOSIT_TOPIC0:
        is_deposit = True
    elif normalized_topic0 == GAUGE_WITHDRAW_TOPIC0:
        is_deposit = False
    else:
        raise ValueError("Gauge stake log topic0 does not match a CLGauge stake event")
    if log.get("data") != "0x":
        # Every parameter is indexed, so any data word means a different event.
        raise ValueError("Gauge stake log data must be empty because every parameter is indexed")
    # The user and token-id topics are validated for well-formedness even though
    # the liquidity fold needs only the final indexed liquidity delta.
    _parse_hex_field(topics[1], "user topic")
    _parse_hex_field(topics[2], "token id topic")
    liquidity = _parse_hex_field(topics[3], "liquidity topic")
    block_number = _parse_hex_field(log.get("blockNumber"), "blockNumber")
    log_index = _parse_hex_field(log.get("logIndex"), "logIndex")
    return GaugeStakeEventRecord(
        block_number=block_number,
        log_index=log_index,
        is_deposit=is_deposit,
        liquidity=liquidity,
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
                amount0=record.amount0,
                amount1=record.amount1,
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


def staked_tvl_usd(
    staked_usdc_raw: int,
    usdc_decimals: int,
    staked_stock_raw: int,
    stock_decimals: int,
    stock_price_usdc: Decimal,
) -> Decimal:
    """Value one pool's gauge-staked balances in USDC at the observed price.

    The venue's own read layer reports both staked balances, so valuing them
    at the pool's observed price anchors the emissions-APR convention.

    Args:
        staked_usdc_raw: Raw staked USDC token units from the Sugar record.
        usdc_decimals: USDC token decimal count.
        staked_stock_raw: Raw staked stock token units from the Sugar record.
        stock_decimals: Stock token decimal count.
        stock_price_usdc: Observed pool price in USDC per one stock token.

    Returns:
        The exact staked value in USDC.

    Raises:
        ValueError: If any decimal count is out of range or the price is not
            positive.
    """
    if not 0 <= usdc_decimals <= MAX_TOKEN_DECIMALS:
        raise ValueError("usdc_decimals is out of the documented range")
    if not 0 <= stock_decimals <= MAX_TOKEN_DECIMALS:
        raise ValueError("stock_decimals is out of the documented range")
    if stock_price_usdc <= 0:
        raise ValueError("stock_price_usdc must be positive")
    with localcontext() as decimal_context:
        # Local precision isolates deterministic valuation math from settings.
        decimal_context.prec = MATH_PRECISION
        staked_usdc = Decimal(staked_usdc_raw) / Decimal(10) ** usdc_decimals
        staked_stock = Decimal(staked_stock_raw) / Decimal(10) ** stock_decimals
        return +(staked_usdc + staked_stock * stock_price_usdc)


def _emissions_apr(
    gauge_liquidity: int,
    anchor_gauge_liquidity: int,
    anchor_staked_tvl_usd: Decimal,
    emissions_per_second: int,
    aero_price_assumption_usd: Decimal,
) -> Decimal:
    """Annualize one gauge's reward stream over its staked value.

    Two documented approximations hold: the gauge's anchor reward rate is
    constant across the window because Aerodrome resets it only at weekly
    epochs, and staked value scales linearly with staked liquidity at the
    frozen per-unit anchor value because other LPs' range shapes are private.

    Args:
        gauge_liquidity: Reconstructed staked liquidity for this step.
        anchor_gauge_liquidity: Staked liquidity at the anchor block.
        anchor_staked_tvl_usd: Staked value in USDC at the anchor block.
        emissions_per_second: Raw AERO reward rate per second.
        aero_price_assumption_usd: Documented AERO price assumption in USDC.

    Returns:
        The raw emissions APR as a decimal fraction in Aerodrome's display
        convention (1.5 means 150 percent APR).

    Raises:
        ValueError: If the step's staked liquidity leaves the APR undefined.
    """
    if gauge_liquidity <= 0:
        # With no staked liquidity sharing them, emissions are unbounded per
        # unit and the APR has no meaningful value for the replay.
        raise ValueError("emissions APR is undefined for a gauge with zero staked liquidity")
    with localcontext() as decimal_context:
        # Local precision isolates deterministic APR math from settings.
        decimal_context.prec = MATH_PRECISION
        # Annual reward value under the constant-rate assumption.
        annual_reward_usd = (
            Decimal(emissions_per_second)
            * aero_price_assumption_usd
            * Decimal(SECONDS_PER_YEAR)
            / Decimal(10) ** AERO_DECIMALS
        )
        # Staked value scales linearly with liquidity at the frozen anchor value.
        staked_tvl = (
            anchor_staked_tvl_usd * Decimal(gauge_liquidity) / Decimal(anchor_gauge_liquidity)
        )
        return +(annual_reward_usd / staked_tvl)


def build_emissions_apr_history(
    pool_address: str,
    gauge_address: str,
    from_block: int,
    to_block: int,
    anchor_block: int,
    observed_at: datetime,
    window_start_timestamp: datetime,
    anchor_gauge_liquidity: int,
    anchor_staked_tvl_usd: Decimal,
    emissions_per_second: int,
    aero_price_assumption_usd: Decimal,
    records: tuple[GaugeStakeEventRecord, ...],
    timestamps_by_block: Mapping[int, datetime],
) -> EmissionsAprHistory:
    """Fold stake events backward from the anchor into an emissions-APR series.

    The gauge's staked liquidity is anchored exactly at the Sugar snapshot
    block and walked backward by the window's net stake events, so every
    historical dilution and concentration appears exactly as mined. Each step
    converts liquidity into the raw emissions APR under this module's two
    documented approximations: the constant reward rate and the frozen
    per-liquidity anchor value.

    Args:
        pool_address: Slipstream pool whose gauge the series describes.
        gauge_address: CLGauge whose stake events were reconstructed.
        from_block: First block of the reconstruction window.
        to_block: Last block of the reconstruction window.
        anchor_block: Block whose Sugar snapshot reported the anchor state.
        observed_at: Aware instant the reconstruction was performed.
        window_start_timestamp: Aware header timestamp of the first block.
        anchor_gauge_liquidity: Positive staked liquidity at the anchor block.
        anchor_staked_tvl_usd: Positive staked value in USDC at the anchor block.
        emissions_per_second: Raw AERO reward rate per second at the anchor.
        aero_price_assumption_usd: Documented AERO price assumption in USDC.
        records: Decoded stake events from the window.
        timestamps_by_block: Aware block-header timestamps for every record block.

    Returns:
        The immutable stepwise emissions-APR series ordered by time.

    Raises:
        ValueError: If any anchor input is non-positive, any event block lacks
            a timestamp, the events contradict the anchor by driving staked
            liquidity negative or to an APR-undefined zero, or any step is
            invalid.
    """
    if anchor_gauge_liquidity <= 0:
        raise ValueError("anchor_gauge_liquidity must be positive")
    if anchor_staked_tvl_usd <= 0:
        raise ValueError("anchor_staked_tvl_usd must be positive")
    if aero_price_assumption_usd <= 0:
        raise ValueError("aero_price_assumption_usd must be positive")
    ordered = sorted(records, key=lambda item: (item.block_number, item.log_index))
    # Net stake events move liquidity from its window-start level to the anchor.
    net_change = sum(
        (record.liquidity if record.is_deposit else -record.liquidity for record in ordered),
        start=0,
    )
    starting_liquidity = anchor_gauge_liquidity - net_change
    if starting_liquidity < 0:
        raise ValueError(
            "Gauge stake events contradict the anchor: staked liquidity is "
            "negative before the window, so the anchor or event evidence is "
            "incomplete."
        )
    steps = [
        EmissionsAprPoint(
            timestamp=window_start_timestamp,
            gauge_liquidity=starting_liquidity,
            emissions_apr=_emissions_apr(
                starting_liquidity,
                anchor_gauge_liquidity,
                anchor_staked_tvl_usd,
                emissions_per_second,
                aero_price_assumption_usd,
            ),
        )
    ]
    liquidity = starting_liquidity
    for record in ordered:
        liquidity += record.liquidity if record.is_deposit else -record.liquidity
        if liquidity < 0:
            raise ValueError(
                "Gauge stake events contradict the anchor: staked liquidity "
                f"went negative at block {record.block_number}."
            )
        timestamp = timestamps_by_block.get(record.block_number)
        if timestamp is None:
            raise ValueError(f"block {record.block_number} lacks a header timestamp")
        steps.append(
            EmissionsAprPoint(
                timestamp=timestamp,
                block_number=record.block_number,
                log_index=record.log_index,
                gauge_liquidity=liquidity,
                emissions_apr=_emissions_apr(
                    liquidity,
                    anchor_gauge_liquidity,
                    anchor_staked_tvl_usd,
                    emissions_per_second,
                    aero_price_assumption_usd,
                ),
            )
        )
    return EmissionsAprHistory(
        pool_address=normalize_evm_address(pool_address),
        gauge_address=normalize_evm_address(gauge_address),
        from_block=from_block,
        to_block=to_block,
        anchor_block=anchor_block,
        observed_at=observed_at,
        anchor_gauge_liquidity=anchor_gauge_liquidity,
        anchor_staked_tvl_usd=anchor_staked_tvl_usd,
        emissions_per_second=emissions_per_second,
        aero_price_assumption_usd=aero_price_assumption_usd,
        reconstruction_mode="event_fold",
        starting_gauge_liquidity=starting_liquidity,
        steps=tuple(steps),
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
        raise ValueError(f"Log field {field_name} was not a 0x-prefixed hex string")
    try:
        return int(value, 16)
    except ValueError as error:
        raise ValueError(f"Log field {field_name} was not valid hexadecimal") from error


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


class OrderedLogRecord(Protocol):
    """Expose the onchain ordering evidence every decoded log record carries."""

    # The block number and log index order decoded events exactly as mined.
    block_number: int
    log_index: int


# The shared windowed-log collector is generic over the decoded record type.
LogRecordT = TypeVar("LogRecordT", bound=OrderedLogRecord)


class EventHistoryRpcBackend:
    """Reconstruct onchain event histories through read-only logs and header reads."""

    def __init__(
        self,
        rpc_url: str = DEFAULT_BASE_RPC_URL,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        max_attempts: int = MAX_REQUEST_ATTEMPTS,
        page_delay_seconds: float = PAGE_DELAY_SECONDS,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        log_window_blocks: int = DEFAULT_LOG_WINDOW_BLOCKS,
        max_logs_per_window: int = DEFAULT_MAX_LOGS_PER_WINDOW,
        max_total_swap_events: int = MAX_TOTAL_SWAP_EVENTS,
        max_total_gauge_stake_events: int = MAX_TOTAL_GAUGE_STAKE_EVENTS,
        max_block_header_lookups: int = DEFAULT_MAX_BLOCK_HEADER_LOOKUPS,
        header_batch_size: int = 1,
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
            max_total_swap_events: Runaway guard on one pool's total decoded
                Swap events.
            max_total_gauge_stake_events: Runaway guard on one gauge's total
                decoded stake events.
            max_block_header_lookups: Unique block headers one reconstruction
                may fetch before failing closed.
            header_batch_size: Block-header reads grouped into one JSON-RPC
                batch request; one preserves the unbatched wire shape and the
                public-endpoint cap bounds the maximum.
            transport: Optional injected HTTP transport for deterministic tests.
            sleep: Injected delay function used for backoff and politeness waits.

        Raises:
            ValueError: If any bound is non-positive or out of its documented range.
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
        if max_total_swap_events <= 0:
            raise ValueError("max_total_swap_events must be positive")
        if max_total_gauge_stake_events <= 0:
            raise ValueError("max_total_gauge_stake_events must be positive")
        if max_block_header_lookups <= 0:
            raise ValueError("max_block_header_lookups must be positive")
        if not 1 <= header_batch_size <= MAX_HEADER_BATCH_SIZE:
            raise ValueError(f"header_batch_size must be between one and {MAX_HEADER_BATCH_SIZE}")
        self._rpc_url = rpc_url
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._page_delay_seconds = page_delay_seconds
        self._max_response_bytes = max_response_bytes
        self._log_window_blocks = log_window_blocks
        self._max_logs_per_window = max_logs_per_window
        self._max_total_swap_events = max_total_swap_events
        self._max_total_gauge_stake_events = max_total_gauge_stake_events
        self._max_block_header_lookups = max_block_header_lookups
        self._header_batch_size = header_batch_size
        # An injected transport keeps unit tests completely off the network.
        self._transport = transport
        self._sleep = sleep

    def read_erc20_decimals(self, token_address: str) -> int:
        """Read one ERC20 token's decimal count through a read-only eth_call.

        Neither the B20 registry nor the Sugar Lp struct records per-token
        decimals, so the price conversion's decimal scales are read directly
        from each token contract exactly once per token per run.

        Args:
            token_address: ERC20 contract whose decimals() is read.

        Returns:
            The token's decimal count.

        Raises:
            HistoryUnavailableError: If the call cannot complete with bounded
                retries or the response is malformed.
        """
        normalized_token = normalize_evm_address(token_address)
        with httpx.Client(
            timeout=self._timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
            headers={"User-Agent": "aero-bot/0.1 read-only-token-metadata"},
        ) as client:
            result = self._rpc_call(
                client,
                "eth_call",
                [{"to": normalized_token, "data": ERC20_DECIMALS_SELECTOR}, "latest"],
            )
        if not isinstance(result, str) or not result.startswith("0x"):
            raise HistoryUnavailableError("decimals() call did not return a 0x-prefixed string")
        try:
            data_bytes = bytes.fromhex(result[2:])
        except ValueError as error:
            raise HistoryUnavailableError("decimals() call returned invalid hexadecimal") from error
        if len(data_bytes) != WORD_BYTES:
            raise HistoryUnavailableError("decimals() call must return exactly one ABI word")
        decimals = int.from_bytes(data_bytes, "big")
        if decimals > MAX_TOKEN_DECIMALS:
            raise HistoryUnavailableError(
                f"decimals() returned {decimals}, above the documented bound"
            )
        return decimals

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
            HistoryUnavailableError: If any read cannot complete with
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
            self._prefetch_block_headers(client, sorted(unique_blocks), headers)
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

    def fetch_emissions_apr_history(
        self,
        pool_address: str,
        gauge_address: str,
        anchor_block: int,
        anchor_gauge_liquidity: int,
        anchor_staked_tvl_usd: Decimal,
        emissions_per_second: int,
        aero_price_assumption_usd: Decimal,
        lookback: timedelta = REHEARSAL_LOOKBACK,
    ) -> EmissionsAprHistory:
        """Reconstruct one pool's emissions-APR series over a lookback window.

        The window is anchored at the Sugar snapshot block rather than the
        chain head, so the backward liquidity fold closes on the snapshot's
        gauge liquidity whenever the stake events explain it. The gauge's
        Deposit and Withdraw logs are paged through bounded eth_getLogs
        windows and each event's timestamp comes from its own block header.
        No chain-head read is ever made, so a replay run against a pinned
        snapshot is fully reproducible.

        Staked positions can also change liquidity through the position
        manager while the gauge holds them, which stake events do not carry,
        so on the live B20 gauges the fold routinely contradicts the anchor.
        A contradicting fold is not fabricated into a series: the fetch falls
        back to the documented constant-anchor APR, one window-long step at
        the anchor's liquidity level, labeled constant_anchor_apr.

        Args:
            pool_address: Slipstream pool the gauge belongs to.
            gauge_address: CLGauge whose staked liquidity is reconstructed.
            anchor_block: Sugar snapshot block that reported the anchor state.
            anchor_gauge_liquidity: Gauge staked liquidity at the anchor block.
            anchor_staked_tvl_usd: Staked value in USDC at the anchor block.
            emissions_per_second: Raw AERO reward rate per second at the anchor.
            aero_price_assumption_usd: Documented AERO price assumption.
            lookback: How far back from the anchor block to reconstruct.

        Returns:
            The immutable stepwise emissions-APR series labeled event_fold
            when the stake events close on the anchor exactly, otherwise the
            documented constant_anchor_apr fallback.

        Raises:
            ValueError: If any anchor input is non-positive or the lookback
                is not positive.
            HistoryUnavailableError: If any read cannot complete with bounded
                retries, any response violates its bounds, or any decoded
                evidence is malformed.
        """
        if lookback <= timedelta(0):
            raise ValueError("lookback must be positive")
        if anchor_block < 0:
            raise ValueError("anchor_block must be non-negative")
        if anchor_gauge_liquidity <= 0:
            raise ValueError("anchor_gauge_liquidity must be positive")
        if anchor_staked_tvl_usd <= 0:
            raise ValueError("anchor_staked_tvl_usd must be positive")
        if emissions_per_second < 0:
            raise ValueError("emissions_per_second must be non-negative")
        if aero_price_assumption_usd <= 0:
            raise ValueError("aero_price_assumption_usd must be positive")
        normalized_pool = normalize_evm_address(pool_address)
        normalized_gauge = normalize_evm_address(gauge_address)
        with httpx.Client(
            timeout=self._timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
            headers={"User-Agent": "aero-bot/0.1 read-only-gauge-history"},
        ) as client:
            # One header cache serves the anchor read, the search, and the events.
            headers: dict[int, BlockHeader] = {}
            anchor_header = self._block_header(client, anchor_block, headers)
            # The window opens at the first block at or after the target instant.
            target_timestamp = anchor_header.timestamp - lookback
            start_block = self._first_block_at_or_after(
                client, target_timestamp, anchor_block, headers
            )
            records = self._collect_windowed_logs(
                client,
                normalized_gauge,
                start_block,
                anchor_block,
                [[GAUGE_DEPOSIT_TOPIC0, GAUGE_WITHDRAW_TOPIC0]],
                decode_gauge_stake_log,
                self._max_total_gauge_stake_events,
                "gauge stake",
            )
            # Every event block needs its header once; the cache deduplicates and
            # the cumulative lookup bound inside the reader fails closed.
            unique_blocks = {record.block_number for record in records}
            self._prefetch_block_headers(client, sorted(unique_blocks), headers)
            # The opening step carries the window-start header timestamp.
            window_start_header = self._block_header(client, start_block, headers)
            try:
                return build_emissions_apr_history(
                    pool_address=normalized_pool,
                    gauge_address=normalized_gauge,
                    from_block=start_block,
                    to_block=anchor_block,
                    anchor_block=anchor_block,
                    observed_at=datetime.now(UTC),
                    window_start_timestamp=window_start_header.timestamp,
                    anchor_gauge_liquidity=anchor_gauge_liquidity,
                    anchor_staked_tvl_usd=anchor_staked_tvl_usd,
                    emissions_per_second=emissions_per_second,
                    aero_price_assumption_usd=aero_price_assumption_usd,
                    records=records,
                    timestamps_by_block={
                        number: header.timestamp for number, header in headers.items()
                    },
                )
            except ValueError:
                # The pure builder's remaining failure paths are exactly the
                # anchor contradictions (negative or zero staked liquidity),
                # which live gauges hit through position-manager rebalances.
                return EmissionsAprHistory(
                    pool_address=normalized_pool,
                    gauge_address=normalized_gauge,
                    from_block=start_block,
                    to_block=anchor_block,
                    anchor_block=anchor_block,
                    observed_at=datetime.now(UTC),
                    anchor_gauge_liquidity=anchor_gauge_liquidity,
                    anchor_staked_tvl_usd=anchor_staked_tvl_usd,
                    emissions_per_second=emissions_per_second,
                    aero_price_assumption_usd=aero_price_assumption_usd,
                    reconstruction_mode="constant_anchor_apr",
                    starting_gauge_liquidity=anchor_gauge_liquidity,
                    steps=(
                        EmissionsAprPoint(
                            timestamp=window_start_header.timestamp,
                            gauge_liquidity=anchor_gauge_liquidity,
                            emissions_apr=_emissions_apr(
                                anchor_gauge_liquidity,
                                anchor_gauge_liquidity,
                                anchor_staked_tvl_usd,
                                emissions_per_second,
                                aero_price_assumption_usd,
                            ),
                        ),
                    ),
                )

    def _latest_block_number(self, client: httpx.Client) -> int:
        """Read the chain head block number.

        Args:
            client: The bounded read-only HTTP client.

        Returns:
            The latest mined block number.

        Raises:
            HistoryUnavailableError: If the read fails or returns garbage.
        """
        result = self._rpc_call(client, "eth_blockNumber", [])
        if not isinstance(result, str):
            raise HistoryUnavailableError("block number read did not return a string")
        try:
            return int(result, 16)
        except ValueError as error:
            raise HistoryUnavailableError("block number read returned invalid hex") from error

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
            HistoryUnavailableError: If the header read fails or is malformed.
        """
        cached = headers.get(block_number)
        if cached is not None:
            return cached
        if len(headers) >= self._max_block_header_lookups:
            raise HistoryUnavailableError(
                f"Block header lookups exceeded the {self._max_block_header_lookups} bound."
            )
        # The politeness delay also applies to header reads on the shared RPC.
        self._sleep(self._page_delay_seconds)
        result = self._rpc_call(client, "eth_getBlockByNumber", [hex(block_number), False])
        try:
            header = _block_header_from_result(result)
        except ValueError as error:
            raise HistoryUnavailableError(
                f"Block {block_number} header was malformed: {error}"
            ) from error
        if header.number != block_number:
            raise HistoryUnavailableError(
                f"Block header read for {block_number} returned block {header.number}"
            )
        headers[block_number] = header
        return header

    def _prefetch_block_headers(
        self,
        client: httpx.Client,
        numbers: Sequence[int],
        headers: dict[int, BlockHeader],
    ) -> None:
        """Fill the shared header cache for every missing block number.

        A batch size above one groups the header reads into bounded JSON-RPC
        batch requests, which keeps multi-week reconstructions feasible
        against the public endpoint while every timestamp stays an exact
        block-header read rather than an interpolation.

        Args:
            client: The bounded read-only HTTP client.
            numbers: Block numbers requiring headers; duplicates are deduplicated.
            headers: Cache shared across one reconstruction.

        Raises:
            HistoryUnavailableError: If the cumulative lookup bound would be
                exceeded or any batch read fails or is malformed.
        """
        if self._header_batch_size == 1:
            # The unbatched path preserves the one-request-per-header wire shape.
            for block_number in numbers:
                self._block_header(client, block_number, headers)
            return
        missing = [number for number in dict.fromkeys(numbers) if number not in headers]
        if len(headers) + len(missing) > self._max_block_header_lookups:
            raise HistoryUnavailableError(
                f"Block header lookups exceeded the {self._max_block_header_lookups} bound."
            )
        for chunk_start in range(0, len(missing), self._header_batch_size):
            chunk = missing[chunk_start : chunk_start + self._header_batch_size]
            # One politeness delay separates batch requests on the shared RPC.
            self._sleep(self._page_delay_seconds)
            results = self._rpc_batch_call(
                client,
                [("eth_getBlockByNumber", [hex(block_number), False]) for block_number in chunk],
            )
            for block_number, result in zip(chunk, results, strict=True):
                try:
                    header = _block_header_from_result(result)
                except ValueError as error:
                    raise HistoryUnavailableError(
                        f"Block {block_number} header was malformed: {error}"
                    ) from error
                if header.number != block_number:
                    raise HistoryUnavailableError(
                        f"Block header read for {block_number} returned block {header.number}"
                    )
                headers[block_number] = header

    def _rpc_batch_call(
        self,
        client: httpx.Client,
        requests: Sequence[tuple[str, list[object]]],
    ) -> list[object]:
        """Perform one JSON-RPC batch of read-only requests with retry and backoff.

        Args:
            client: The bounded read-only HTTP client.
            requests: (method, params) pairs restricted to read-only calls.

        Returns:
            The successful JSON-RPC results aligned with the request order.

        Raises:
            HistoryUnavailableError: If the batch keeps failing after bounded
                retries, is not answered by exactly one response per request
                with matching ids, or reports a non-transient error.
        """
        payload = [
            {"jsonrpc": "2.0", "id": index + 1, "method": method, "params": params}
            for index, (method, params) in enumerate(requests)
        ]
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
                raise HistoryUnavailableError(
                    f"RPC batch response contained {response_size} bytes, above the "
                    "configured limit"
                )
            if response.status_code != 200:
                raise HistoryUnavailableError(
                    f"RPC batch request failed with unexpected HTTP status {response.status_code}"
                )
            try:
                body = cast(object, response.json())
            except ValueError as error:
                raise HistoryUnavailableError("RPC batch response was not valid JSON") from error
            if not isinstance(body, list) or len(body) != len(requests):
                served = len(body) if isinstance(body, list) else "a non-list body"
                # A non-list body is the endpoint's own batch rejection (for
                # example the maximum-calls-per-batch error), which retrying
                # cannot fix, so it fails closed immediately.
                raise HistoryUnavailableError(
                    f"RPC batch response held {served} entries for {len(requests)} requests."
                )
            entries_by_id: dict[int, dict[str, object]] = {}
            for entry in body:
                entry_id = entry.get("id") if isinstance(entry, dict) else None
                if not isinstance(entry, dict) or not isinstance(entry_id, int):
                    raise HistoryUnavailableError(
                        "RPC batch response held an entry without an integer id"
                    )
                entries_by_id[entry_id] = entry
            if set(entries_by_id) != set(range(1, len(requests) + 1)):
                raise HistoryUnavailableError(
                    "RPC batch response ids did not cover every request exactly once"
                )
            results: list[object] = []
            retriable: str | None = None
            for index, (method, _) in enumerate(requests):
                entry = entries_by_id[index + 1]
                if "result" in entry:
                    results.append(entry["result"])
                    continue
                error_body = entry.get("error")
                if not isinstance(error_body, dict):
                    raise HistoryUnavailableError(
                        f"RPC batch entry for {method} had neither result nor error"
                    )
                error_code = error_body.get("code")
                error_message = str(error_body.get("message", ""))
                # Base's public endpoint reports rate limiting as a retriable error.
                if error_code == RATE_LIMIT_ERROR_CODE or "rate limit" in error_message.lower():
                    retriable = f"rate-limited {method} entry: {error_message}"
                    break
                raise HistoryUnavailableError(
                    f"RPC error {error_code} on batched {method}: {error_message}"
                )
            if retriable is not None:
                failure = retriable
                continue
            return results
        raise HistoryUnavailableError(
            f"RPC batch failed after {self._max_attempts} attempts: {failure}"
        )

    def _first_block_at_or_after(
        self,
        client: httpx.Client,
        target_timestamp: datetime,
        head_block: int,
        headers: dict[int, BlockHeader],
    ) -> int:
        """Binary-search the first block whose timestamp reaches a target.

        Block timestamps are non-decreasing in block number, so a bounded
        binary search over header reads locates the exact window boundary.

        Args:
            client: The bounded read-only HTTP client.
            target_timestamp: Earliest timestamp the window may start from.
            head_block: Block number closing the search space, either the chain
                head or an anchor snapshot block.
            headers: Cache shared across one reconstruction.

        Returns:
            The smallest block number whose timestamp is at or after the target.

        Raises:
            HistoryUnavailableError: If the search exceeds its probe bound
                or block timestamps are not monotone.
        """
        low = 0
        high = head_block
        probes = 0
        while low < high:
            probes += 1
            if probes > MAX_BINARY_SEARCH_PROBES:
                raise HistoryUnavailableError(
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
            HistoryUnavailableError: If a window read fails, hits its log
                bound, or returns malformed event data.
        """
        return self._collect_windowed_logs(
            client,
            pool_address,
            start_block,
            end_block,
            [SWAP_EVENT_TOPIC0],
            decode_swap_log,
            self._max_total_swap_events,
            "Swap",
        )

    def _collect_windowed_logs(
        self,
        client: httpx.Client,
        address: str,
        start_block: int,
        end_block: int,
        topics: Sequence[object],
        decode_one: Callable[[object], LogRecordT],
        total_event_bound: int,
        label: str,
    ) -> tuple[LogRecordT, ...]:
        """Page one contract's logs through bounded, refinable eth_getLogs windows.

        The range first pages through consecutive windows of the configured
        block span. A window whose answer reaches the log bound is presumed
        truncated, because a capped answer is indistinguishable from a
        complete one at that count, so the window splits in half until every
        half answers below the bound; only a single-block window still at the
        bound fails closed because no further split can rule truncation out.

        Args:
            client: The bounded read-only HTTP client.
            address: Normalized contract address filter every log must match.
            start_block: First block of the reconstruction window.
            end_block: Last block of the reconstruction window.
            topics: Topic filter matching the events being reconstructed.
            decode_one: Strict decoder turning one raw log into its record.
            total_event_bound: Runaway guard on the total decoded event count.
            label: Event label used in the fail-closed diagnostics.

        Returns:
            Every decoded event ordered by block number then log index.

        Raises:
            HistoryUnavailableError: If a window read fails, a single-block
                window still reaches its log bound, or any decoded event data
                is malformed.
        """
        records: list[LogRecordT] = []
        pending: deque[tuple[int, int]] = deque(
            (window_start, min(window_start + self._log_window_blocks - 1, end_block))
            for window_start in range(start_block, end_block + 1, self._log_window_blocks)
        )
        while pending:
            window_start, window_end = pending.popleft()
            result = self._rpc_call(
                client,
                "eth_getLogs",
                [
                    {
                        "fromBlock": hex(window_start),
                        "toBlock": hex(window_end),
                        "address": address,
                        "topics": list(topics),
                    }
                ],
            )
            if not isinstance(result, list):
                raise HistoryUnavailableError("eth_getLogs result was not a list")
            if len(result) >= self._max_logs_per_window:
                if window_end > window_start:
                    # A busy window splits in half rather than trusting an
                    # answer that may have been capped by the endpoint.
                    middle = (window_start + window_end) // 2
                    pending.appendleft((middle + 1, window_end))
                    pending.appendleft((window_start, middle))
                else:
                    raise HistoryUnavailableError(
                        f"eth_getLogs single-block window {window_start} returned "
                        f"{len(result)} logs at or above the "
                        f"{self._max_logs_per_window} window bound."
                    )
            else:
                for log in result:
                    try:
                        record = decode_one(log)
                    except ValueError as error:
                        raise HistoryUnavailableError(
                            f"eth_getLogs window {window_start}..{window_end} held a "
                            f"malformed {label} log: {error}"
                        ) from error
                    # The response address must match the requested contract filter.
                    if isinstance(log, dict) and str(log.get("address", "")).lower() != address:
                        raise HistoryUnavailableError(
                            f"eth_getLogs window {window_start}..{window_end} returned a "
                            "log from another contract."
                        )
                    records.append(record)
                if len(records) > total_event_bound:
                    raise HistoryUnavailableError(
                        f"Contract {address} exceeded {total_event_bound} decoded {label} events."
                    )
            if pending:
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
            HistoryUnavailableError: If the request keeps failing after
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
                raise HistoryUnavailableError(
                    f"RPC response contained {response_size} bytes, above the configured limit"
                )
            if response.status_code != 200:
                raise HistoryUnavailableError(
                    f"RPC request failed with unexpected HTTP status {response.status_code}"
                )
            try:
                body = cast(object, response.json())
            except ValueError as error:
                raise HistoryUnavailableError("RPC response was not valid JSON") from error
            if not isinstance(body, dict) or "result" not in body:
                error_body = body.get("error") if isinstance(body, dict) else None
                if not isinstance(error_body, dict):
                    raise HistoryUnavailableError("RPC response had neither result nor error")
                error_code = error_body.get("code")
                error_message = str(error_body.get("message", ""))
                # Base's public endpoint reports rate limiting as a retriable error.
                if error_code == RATE_LIMIT_ERROR_CODE or "rate limit" in error_message.lower():
                    failure = f"RPC error {error_code}: {error_message}"
                    continue
                raise HistoryUnavailableError(f"RPC error {error_code}: {error_message}")
            return cast(object, body["result"])
        raise HistoryUnavailableError(
            f"RPC {method} failed after {self._max_attempts} attempts: {failure}"
        )
