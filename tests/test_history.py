"""Behavior tests for the read-only onchain event-history reconstruction."""

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.resources import files
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from aero_bot.history import (
    ERC20_DECIMALS_SELECTOR,
    GAUGE_DEPOSIT_TOPIC0,
    GAUGE_STAKE_TOPIC_COUNT,
    GAUGE_STAKE_TOPICS_LAYOUT,
    GAUGE_WITHDRAW_TOPIC0,
    MAX_HEADER_BATCH_SIZE,
    MAX_TOTAL_GAUGE_STAKE_EVENTS,
    MAX_TOTAL_SWAP_EVENTS,
    REHEARSAL_LOOKBACK,
    SWAP_DATA_LAYOUT,
    SWAP_EVENT_TOPIC0,
    BlockHeader,
    EmissionsAprHistory,
    EmissionsAprPoint,
    EventHistoryRpcBackend,
    GaugeStakeEventRecord,
    HistoryUnavailableError,
    PoolPricePath,
    PoolPricePoint,
    SwapEventRecord,
    build_emissions_apr_history,
    build_price_path,
    decode_gauge_stake_log,
    decode_swap_log,
    price_usdc_per_stock,
    staked_tvl_usd,
)

# Fixture identities mirror official contracts without claiming live observations.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
POOL_ADDRESS = "0x1111111111111111111111111111111111111111"
OTHER_POOL_ADDRESS = "0x1313131313131313131313131313131313131313"
SENDER_ADDRESS = "0x2111111111111111111111111111111111111111"
RECIPIENT_ADDRESS = "0x2121212121212121212121212121212121212121"
GAUGE_ADDRESS = "0x3111111111111111111111111111111111111111"
OTHER_GAUGE_ADDRESS = "0x3313131313131313131313131313131313131313"
QUOTE_ADDRESS = "0x" + "82" * 20
# The fixture chain head and its base timestamp define a 2-second block cadence.
FIXTURE_LATEST_BLOCK = 1_000
FIXTURE_GENESIS_TIMESTAMP = 1_756_000_000
FIXTURE_BLOCK_SECONDS = 2
# A ten-second lookback from the head timestamp lands the window at block 995.
DEFAULT_LOOKBACK = timedelta(seconds=10)


def fixture_block_timestamp(block_number: int) -> datetime:
    """Compute the fixture chain's timestamp for one block.

    Args:
        block_number: Fixture block number.

    Returns:
        The block's UTC timestamp under the linear fixture cadence.
    """
    return datetime.fromtimestamp(
        FIXTURE_GENESIS_TIMESTAMP + FIXTURE_BLOCK_SECONDS * block_number, tz=UTC
    )


def encode_word(value: int) -> bytes:
    """Encode one integer as an unsigned 32-byte ABI word.

    Args:
        value: The unsigned or negative Python integer.

    Returns:
        The two's-complement big-endian word.
    """
    return (value % (1 << 256)).to_bytes(32, "big")


def swap_log(
    block_number: int,
    log_index: int,
    sqrt_ratio: int,
    liquidity: int = 1_000,
    tick: int = -5,
    pool_address: str = POOL_ADDRESS,
    amount0: int = 10,
    amount1: int = -20,
) -> dict[str, Any]:
    """Build one well-formed raw Slipstream Swap log entry.

    Args:
        block_number: Fixture block the swap was mined in.
        log_index: Log position inside the block.
        sqrt_ratio: Raw post-swap square-root price.
        liquidity: Raw active liquidity after the swap.
        tick: Signed post-swap tick.
        pool_address: Pool the log was emitted from.
        amount0: Signed token-zero delta carried by the log.
        amount1: Signed token-one delta carried by the log.

    Returns:
        A JSON-RPC log object shaped exactly like an eth_getLogs entry.
    """
    data = b"".join(
        (
            encode_word(amount0),
            encode_word(amount1),
            encode_word(sqrt_ratio),
            encode_word(liquidity),
            encode_word(tick),
        )
    )
    return {
        "address": pool_address,
        "topics": [SWAP_EVENT_TOPIC0, SENDER_ADDRESS, RECIPIENT_ADDRESS],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block_number),
        "logIndex": hex(log_index),
        "removed": False,
    }


def gauge_log(
    block_number: int,
    log_index: int,
    liquidity: int,
    is_deposit: bool = True,
    gauge_address: str = GAUGE_ADDRESS,
) -> dict[str, Any]:
    """Build one well-formed raw CLGauge stake log entry.

    Args:
        block_number: Fixture block the stake event was mined in.
        log_index: Log position inside the block.
        liquidity: Raw indexed liquidity delta of the stake event.
        is_deposit: True for Deposit and False for Withdraw.
        gauge_address: Gauge contract the log was emitted from.

    Returns:
        A JSON-RPC log object shaped exactly like an eth_getLogs entry.
    """
    topic0 = GAUGE_DEPOSIT_TOPIC0 if is_deposit else GAUGE_WITHDRAW_TOPIC0
    return {
        "address": gauge_address,
        "topics": [
            topic0,
            # Indexed addresses occupy the low 160 bits of their topic word.
            "0x" + "00" * 12 + SENDER_ADDRESS[2:],
            "0x" + format(7, "064x"),
            "0x" + format(liquidity, "064x"),
        ],
        # Every stake-event parameter is indexed, so the data section is empty.
        "data": "0x",
        "blockNumber": hex(block_number),
        "logIndex": hex(log_index),
        "removed": False,
    }


class FixtureRpcTransport(httpx.MockTransport):
    """Serve deterministic JSON-RPC responses without touching the network."""

    def __init__(
        self,
        logs: list[dict[str, Any]] | None = None,
        failures_before_success: int = 0,
        latest_block: int = FIXTURE_LATEST_BLOCK,
        result_overrides: dict[str, object] | None = None,
        failure_mode: str | None = None,
        erc20_decimals: dict[str, str] | None = None,
        batch_result_override: object | None = None,
        batch_entry_failures_before_success: int = 0,
        batch_failure_mode: str | None = None,
    ) -> None:
        """Configure the fixture endpoint with optional transient failures.

        Args:
            logs: Raw log entries served to any matching eth_getLogs window.
            failures_before_success: Rate-limit failures served before success.
            latest_block: Chain head block number served by eth_blockNumber.
            result_overrides: Raw result objects served by method name, used to
                exercise garbage-response handling.
            failure_mode: Optional transport-level failure served on every call,
                one of transport_error, http_429, http_500, http_404, not_json,
                empty_body, or fatal_rpc_error.
            erc20_decimals: Raw decimals() result words by token address;
                unmapped tokens are served malformed evidence.
            batch_result_override: Raw body served verbatim for every JSON-RPC
                batch request, used to exercise batch response handling.
            batch_entry_failures_before_success: Rate-limited first entries
                served on batch responses before clean success.
            batch_failure_mode: Optional transport-level failure served only on
                batch requests, one of transport_error, http_429, http_500,
                http_404, not_json, or oversized.
        """
        # Request counting lets tests assert dedup and retry behavior exactly;
        # batch payloads are lists of the same single-request shapes.
        self.calls: list[dict[str, Any] | list[dict[str, Any]]] = []
        self.batch_calls: list[list[dict[str, Any]]] = []
        self.header_calls_by_block: dict[int, int] = {}
        self._logs = logs or []
        self._remaining_failures = failures_before_success
        self._latest_block = latest_block
        self._result_overrides = result_overrides or {}
        self._failure_mode = failure_mode
        self._erc20_decimals = erc20_decimals or {}
        self._batch_result_override = batch_result_override
        self._batch_entry_failures = batch_entry_failures_before_success
        self._batch_failure_mode = batch_failure_mode
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one JSON-RPC request with configured fixture evidence."""
        payload = json.loads(request.content.decode("utf-8"))
        self.calls.append(payload)
        if self._failure_mode == "transport_error":
            raise httpx.ConnectError("fixture transport refused the connection")
        if self._failure_mode == "http_429":
            return httpx.Response(429, json={})
        if self._failure_mode == "http_500":
            return httpx.Response(500, json={})
        if self._failure_mode == "http_404":
            return httpx.Response(404, json={})
        if self._failure_mode == "not_json":
            return httpx.Response(200, content=b"<html>not json</html>")
        if self._failure_mode == "empty_body":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1})
        if self._failure_mode == "fatal_rpc_error":
            # A non-rate-limit JSON-RPC error fails immediately without retries.
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32601, "message": "method not found"},
                },
            )
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            # Rate-limit failures reuse the observed public-endpoint wording.
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32016, "message": "over rate limit"},
                },
            )
        if isinstance(payload, list):
            return self._handle_batch(payload)
        return self._result(self._serve_result(payload["method"], payload["params"]))

    def _handle_batch(self, entries: list[dict[str, Any]]) -> httpx.Response:
        """Serve one JSON-RPC batch by dispatching each entry in place."""
        self.batch_calls.append(entries)
        if self._batch_failure_mode == "transport_error":
            raise httpx.ConnectError("fixture transport refused the connection")
        if self._batch_failure_mode == "http_429":
            return httpx.Response(429, json={})
        if self._batch_failure_mode == "http_500":
            return httpx.Response(500, json={})
        if self._batch_failure_mode == "http_404":
            return httpx.Response(404, json={})
        if self._batch_failure_mode == "not_json":
            return httpx.Response(200, content=b"<html>not json</html>")
        if self._batch_failure_mode == "oversized":
            return httpx.Response(200, content=b"x" * 4096)
        if self._batch_result_override is not None:
            return httpx.Response(200, json=self._batch_result_override)
        responses: list[dict[str, Any]] = [
            {
                "jsonrpc": "2.0",
                "id": entry["id"],
                "result": self._serve_result(entry["method"], entry["params"]),
            }
            for entry in entries
        ]
        if self._batch_entry_failures > 0:
            self._batch_entry_failures -= 1
            # One rate-limited entry forces the whole batch to be retried.
            responses[0] = {
                "jsonrpc": "2.0",
                "id": entries[0]["id"],
                "error": {"code": -32016, "message": "over rate limit"},
            }
        return httpx.Response(200, json=responses)

    def _serve_result(self, method: str, params: object) -> object:
        """Dispatch one read-only method against the fixture evidence."""
        if method in self._result_overrides:
            return self._result_overrides[method]
        if method == "eth_blockNumber":
            return hex(self._latest_block)
        if method == "eth_getBlockByNumber":
            assert isinstance(params, list)
            return self._block_header_result(params[0])
        if method == "eth_getLogs":
            assert isinstance(params, list)
            return self._logs_result(params[0])
        if method == "eth_call":
            assert isinstance(params, list)
            return self._call_result(params[0])
        raise AssertionError(f"unexpected RPC method {method}")

    def _block_header_result(self, block_hex: str) -> dict[str, str]:
        """Serve one block header under the fixture cadence."""
        block_number = int(block_hex, 16)
        self.header_calls_by_block[block_number] = (
            self.header_calls_by_block.get(block_number, 0) + 1
        )
        # Enormous probe positions clamp to the cadence range so synthetic
        # search spaces stay representable as timestamps.
        cadence_block = min(block_number, FIXTURE_LATEST_BLOCK)
        return {
            "number": hex(block_number),
            "timestamp": hex(int(fixture_block_timestamp(cadence_block).timestamp())),
        }

    def _logs_result(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        """Serve the fixture logs inside one queried block window."""
        from_block = int(query["fromBlock"], 16)
        to_block = int(query["toBlock"], 16)
        return [log for log in self._logs if from_block <= int(log["blockNumber"], 16) <= to_block]

    def _call_result(self, call: object) -> str:
        """Serve one eth_call as the configured ERC20 decimals read."""
        assert isinstance(call, dict)
        decimals_word = self._erc20_decimals.get(str(call.get("to", "")).lower())
        if decimals_word is None:
            # An unmapped token is served malformed evidence for fail-closed tests.
            return "0x"
        return decimals_word

    def _result(self, result: object) -> httpx.Response:
        """Wrap one value as a successful JSON-RPC response."""
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def single_calls(self, method: str) -> list[dict[str, Any]]:
        """Return every unbatched request carrying one method name.

        Args:
            method: JSON-RPC method name the requests must carry.

        Returns:
            The recorded single-request payloads for that method.
        """
        return [call for call in self.calls if isinstance(call, dict) and call["method"] == method]


def no_sleep(_seconds: float) -> None:
    """Discard retry delays so failure-path tests run instantly."""
    return None


def fixture_backend(transport: FixtureRpcTransport) -> EventHistoryRpcBackend:
    """Build one offline backend around a fixture transport.

    Args:
        transport: The deterministic fixture endpoint.

    Returns:
        A backend wired to the fixture with instant politeness delays.
    """
    return EventHistoryRpcBackend(transport=transport, sleep=no_sleep, page_delay_seconds=0.0)


def fetch_path(
    fixture_backend_instance: EventHistoryRpcBackend,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> PoolPricePath:
    """Fetch one fixture pool path with the standard pair configuration.

    Args:
        fixture_backend_instance: Offline backend serving fixture evidence.
        lookback: Lookback window override for one behavior test.

    Returns:
        The reconstructed fixture path.
    """
    return fixture_backend_instance.fetch_price_path(
        pool_address=POOL_ADDRESS,
        token_address=B20_ADDRESS,
        token0_address=B20_ADDRESS,
        token1_address=QUOTE_ADDRESS,
        token_decimals=18,
        quote_decimals=6,
        lookback=lookback,
    )


def fetch_emissions(
    fixture_backend_instance: EventHistoryRpcBackend,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> EmissionsAprHistory:
    """Fetch one fixture pool's emissions-APR history anchored at the head.

    Args:
        fixture_backend_instance: Offline backend serving fixture evidence.
        lookback: Lookback window override for one behavior test.

    Returns:
        The reconstructed fixture emissions-APR series.
    """
    return fixture_backend_instance.fetch_emissions_apr_history(
        pool_address=POOL_ADDRESS,
        gauge_address=GAUGE_ADDRESS,
        anchor_block=FIXTURE_LATEST_BLOCK,
        anchor_gauge_liquidity=2_000,
        anchor_staked_tvl_usd=Decimal("4000"),
        emissions_per_second=10**18,
        aero_price_assumption_usd=Decimal("2"),
        lookback=lookback,
    )


def test_decode_swap_log_round_trips_one_event() -> None:
    """A well-formed log decodes with the negative amounts and tick sign-extended."""
    record = decode_swap_log(swap_log(123, 4, 1 << 96, 987, -77, amount0=-500, amount1=1_000))

    assert record == SwapEventRecord(
        block_number=123,
        log_index=4,
        amount0=-500,
        amount1=1_000,
        sqrt_ratio=1 << 96,
        liquidity=987,
        tick=-77,
    )


def test_decode_swap_log_rejects_malformed_entries() -> None:
    """Structurally invalid logs fail closed instead of yielding records."""
    valid = swap_log(1, 0, 1 << 96)
    # Each malformed candidate violates one structural invariant of the event.
    malformed = [
        "not-a-dict",
        {**valid, "removed": True},
        {**valid, "topics": valid["topics"][:2]},
        {**valid, "topics": ["0x" + "ab" * 32, *valid["topics"][1:]]},
        {**valid, "data": "not-hex-prefixed"},
        {**valid, "data": "0x" + "zz" * 160},
        {**valid, "data": "0x" + "00" * 31},
        {**valid, "data": "0x" + "00" * 160},
        {**valid, "blockNumber": "0x!"},
    ]
    for candidate in malformed:
        with pytest.raises(ValueError):
            decode_swap_log(candidate)


def test_price_usdc_per_stock_handles_both_token_orderings() -> None:
    """Raw sqrt prices scale to USDC per stock for either token ordering."""
    # A sqrt ratio of 2^96 squares to exactly one raw price unit.
    assert price_usdc_per_stock(1 << 96, True, 18, 6) == 10**12
    # The reciprocal ordering divides the scaled raw price exactly.
    assert price_usdc_per_stock(1 << 96, False, 18, 6) == 10**12
    # Doubling the root quadruples the raw price.
    assert price_usdc_per_stock(1 << 97, True, 18, 6) == 4 * 10**12
    assert price_usdc_per_stock(1 << 97, False, 18, 6) == 10**12 / 4
    with pytest.raises(ValueError, match="sqrt_ratio"):
        price_usdc_per_stock(0, True, 18, 6)
    with pytest.raises(ValueError, match="stock_decimals"):
        price_usdc_per_stock(1 << 96, True, 99, 6)
    with pytest.raises(ValueError, match="quote_decimals"):
        price_usdc_per_stock(1 << 96, True, 18, 99)


def test_build_price_path_orders_points_and_attaches_prices() -> None:
    """Records sort by block and log index and receive exact header timestamps."""
    records = (
        SwapEventRecord(
            block_number=10,
            log_index=1,
            amount0=10,
            amount1=-20,
            sqrt_ratio=1 << 96,
            liquidity=5,
            tick=1,
        ),
        SwapEventRecord(
            block_number=9,
            log_index=7,
            amount0=10,
            amount1=-20,
            sqrt_ratio=1 << 97,
            liquidity=6,
            tick=2,
        ),
        SwapEventRecord(
            block_number=10,
            log_index=0,
            amount0=10,
            amount1=-20,
            sqrt_ratio=1 << 95,
            liquidity=7,
            tick=3,
        ),
    )
    timestamps = {
        9: fixture_block_timestamp(9),
        10: fixture_block_timestamp(10),
    }
    path = build_price_path(
        pool_address=POOL_ADDRESS,
        token_address=B20_ADDRESS,
        token0_address=B20_ADDRESS,
        token1_address=QUOTE_ADDRESS,
        token_decimals=18,
        quote_decimals=6,
        from_block=9,
        to_block=10,
        observed_at=datetime.now(UTC),
        records=records,
        timestamps_by_block=timestamps,
    )

    assert [point.block_number for point in path.points] == [9, 10, 10]
    assert [point.log_index for point in path.points] == [7, 0, 1]
    assert [point.price_usdc for point in path.points] == [4 * 10**12, 10**12 / 4, 10**12]
    assert path.points[0].timestamp == timestamps[9]
    assert path.token_is_token0 is True


def test_build_price_path_rejects_foreign_tokens() -> None:
    """A token outside the pool pair fails closed."""
    records = (
        SwapEventRecord(
            block_number=9,
            log_index=0,
            amount0=10,
            amount1=-20,
            sqrt_ratio=1 << 96,
            liquidity=1,
            tick=0,
        ),
    )
    with pytest.raises(ValueError, match="pool's two tokens"):
        build_price_path(
            pool_address=POOL_ADDRESS,
            token_address=OTHER_POOL_ADDRESS,
            token0_address=B20_ADDRESS,
            token1_address=QUOTE_ADDRESS,
            token_decimals=18,
            quote_decimals=6,
            from_block=9,
            to_block=9,
            observed_at=datetime.now(UTC),
            records=records,
            timestamps_by_block={9: fixture_block_timestamp(9)},
        )


def test_build_price_path_rejects_missing_timestamps() -> None:
    """A record block without a header timestamp fails closed."""
    records = (
        SwapEventRecord(
            block_number=9,
            log_index=0,
            amount0=10,
            amount1=-20,
            sqrt_ratio=1 << 96,
            liquidity=1,
            tick=0,
        ),
    )
    with pytest.raises(ValueError, match="lacks a header timestamp"):
        build_price_path(
            pool_address=POOL_ADDRESS,
            token_address=B20_ADDRESS,
            token0_address=B20_ADDRESS,
            token1_address=QUOTE_ADDRESS,
            token_decimals=18,
            quote_decimals=6,
            from_block=9,
            to_block=9,
            observed_at=datetime.now(UTC),
            records=records,
            timestamps_by_block={},
        )


def test_fetch_price_path_locates_window_and_dedups_headers() -> None:
    """The full flow locates the boundary, orders swaps, and reads each header once."""
    transport = FixtureRpcTransport(
        logs=[
            swap_log(996, 1, 1 << 96, 100, -5),
            swap_log(996, 0, 1 << 95, 101, -6),
            swap_log(999, 0, 1 << 97, 102, -7),
            swap_log(990, 0, 1 << 96, 103, -8),
        ]
    )
    path = fetch_path(fixture_backend(transport))

    assert path.from_block == 995
    assert path.to_block == FIXTURE_LATEST_BLOCK
    assert [point.block_number for point in path.points] == [996, 996, 999]
    assert [point.log_index for point in path.points] == [0, 1, 0]
    assert [point.price_usdc for point in path.points] == [10**12 / 4, 10**12, 4 * 10**12]
    assert path.points[0].timestamp == fixture_block_timestamp(996)
    assert path.points[2].timestamp == fixture_block_timestamp(999)
    # Every distinct block header was read exactly once across search and events.
    assert set(transport.header_calls_by_block.values()) == {1}


def test_fetch_price_path_binary_search_boundary_is_exact() -> None:
    """Lookbacks falling between block timestamps pick the exact next block."""
    # The fixture head timestamp is T0+2000; ten seconds back hits block 995.
    assert fetch_path(fixture_backend(FixtureRpcTransport())).from_block == 995
    # Nine seconds back skips block 995 because its timestamp predates the target.
    nine_seconds = fetch_path(fixture_backend(FixtureRpcTransport()), timedelta(seconds=9))
    assert nine_seconds.from_block == 996
    # The locked rehearsal lookback is roughly the last three weeks.
    assert REHEARSAL_LOOKBACK.days == 21


def test_total_swap_event_bound_covers_the_locked_lookback_at_the_busiest_rate() -> None:
    """The decoded-event bound fits a 21-day window on the busiest B20 pool."""
    # The busiest B20 pool (AAPLc) reconstructs roughly 3,600 swaps a day, so
    # the locked 21-day lookback needs about 76,000 events; the bound sits near
    # twice that need, and the half-margin check below is what the bound must
    # always satisfy for the locked lookback to reconstruct.
    busiest_21_day_events = 3_600 * REHEARSAL_LOOKBACK.days
    assert MAX_TOTAL_SWAP_EVENTS == 150_000
    assert busiest_21_day_events * 3 // 2 <= MAX_TOTAL_SWAP_EVENTS
    # Stake events run orders of magnitude sparser than swaps, so their guard
    # stays tighter than the swap bound.
    assert MAX_TOTAL_GAUGE_STAKE_EVENTS < MAX_TOTAL_SWAP_EVENTS


def test_fetch_price_path_without_swaps_returns_empty_path() -> None:
    """A quiet pool reconstructs to an empty but well-formed path."""
    path = fetch_path(fixture_backend(FixtureRpcTransport()))

    assert path.points == ()
    assert path.from_block == 995
    assert path.token_is_token0 is True


def test_fetch_price_path_retries_rate_limited_reads() -> None:
    """Transient rate-limit errors are retried with backoff until success."""
    transport = FixtureRpcTransport(failures_before_success=2)
    path = fetch_path(fixture_backend(transport))

    assert path.points == ()
    assert len(transport.single_calls("eth_blockNumber")) == 3


def test_fetch_price_path_fails_closed_at_window_log_bound() -> None:
    """A single block at the log bound fails closed because no split can rule truncation out."""
    transport = FixtureRpcTransport(logs=[swap_log(996, 0, 1 << 96), swap_log(996, 1, 1 << 96)])
    fixture = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, max_logs_per_window=2
    )

    with pytest.raises(HistoryUnavailableError, match="single-block window 996"):
        fetch_path(fixture)


def test_fetch_price_path_refines_busy_windows_instead_of_dropping_them() -> None:
    """Windows at the log bound split in half until every half answers below it."""
    transport = FixtureRpcTransport(
        logs=[
            swap_log(995, 0, 1 << 95),
            swap_log(998, 0, 1 << 96),
            swap_log(999, 0, 1 << 97),
        ]
    )
    fixture = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, max_logs_per_window=2
    )

    path = fetch_path(fixture)

    # Every swap survives refinement, ordered exactly as mined.
    assert [point.block_number for point in path.points] == [995, 998, 999]
    # The initial window reached the bound, so the run split it into halves.
    queries = [
        (int(call["params"][0]["fromBlock"], 16), int(call["params"][0]["toBlock"], 16))
        for call in transport.single_calls("eth_getLogs")
    ]
    assert (995, 997) in queries
    assert (998, 1000) in queries


def test_fetch_price_path_fails_closed_at_total_event_bound() -> None:
    """A pool decoding past the total event bound fails the reconstruction closed."""
    transport = FixtureRpcTransport(
        logs=[
            swap_log(996, 0, 1 << 95),
            swap_log(997, 0, 1 << 96),
            swap_log(998, 0, 1 << 97),
        ]
    )
    # Three swaps spread across blocks stay below the per-window bound, so only
    # the configurable total-event runaway guard trips.
    fixture = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, max_total_swap_events=2
    )

    with pytest.raises(HistoryUnavailableError, match="exceeded 2 decoded Swap events"):
        fetch_path(fixture)


def test_fetch_price_path_fails_closed_on_malformed_logs() -> None:
    """One malformed log entry in a window fails the whole reconstruction."""
    broken = swap_log(996, 0, 1 << 96)
    broken["data"] = "0x" + "00" * 31

    with pytest.raises(HistoryUnavailableError, match="malformed Swap log"):
        fetch_path(fixture_backend(FixtureRpcTransport(logs=[broken])))


def test_fetch_price_path_fails_closed_on_foreign_pool_logs() -> None:
    """A log served from another pool address fails the reconstruction."""
    logs = [swap_log(996, 0, 1 << 96, pool_address=OTHER_POOL_ADDRESS)]

    with pytest.raises(HistoryUnavailableError, match="another contract"):
        fetch_path(fixture_backend(FixtureRpcTransport(logs=logs)))


def test_fetch_price_path_fails_closed_on_header_lookup_bound() -> None:
    """The cumulative header-lookup bound fails the reconstruction closed."""
    transport = FixtureRpcTransport(
        logs=[swap_log(block, 0, 1 << 96) for block in (996, 997, 998, 999)]
    )
    # The search itself reads about eleven headers, so this bound survives the
    # search and trips while resolving the swap blocks.
    fixture = EventHistoryRpcBackend(
        transport=transport,
        sleep=no_sleep,
        page_delay_seconds=0.0,
        max_block_header_lookups=12,
    )

    with pytest.raises(HistoryUnavailableError, match="lookups exceeded"):
        fetch_path(fixture)


def test_backend_constructor_rejects_invalid_bounds() -> None:
    """Every documented bound must be positive before any request is made."""
    invalid_constructors: tuple[tuple[str, Callable[[], EventHistoryRpcBackend]], ...] = (
        ("timeout_seconds", lambda: EventHistoryRpcBackend(timeout_seconds=0)),
        ("max_attempts", lambda: EventHistoryRpcBackend(max_attempts=0)),
        ("page_delay_seconds", lambda: EventHistoryRpcBackend(page_delay_seconds=-1)),
        ("max_response_bytes", lambda: EventHistoryRpcBackend(max_response_bytes=0)),
        ("log_window_blocks", lambda: EventHistoryRpcBackend(log_window_blocks=0)),
        ("max_logs_per_window", lambda: EventHistoryRpcBackend(max_logs_per_window=0)),
        ("max_total_swap_events", lambda: EventHistoryRpcBackend(max_total_swap_events=0)),
        (
            "max_total_gauge_stake_events",
            lambda: EventHistoryRpcBackend(max_total_gauge_stake_events=0),
        ),
        (
            "max_block_header_lookups",
            lambda: EventHistoryRpcBackend(max_block_header_lookups=0),
        ),
    )
    for name, construct in invalid_constructors:
        with pytest.raises(ValueError, match=name):
            construct()


def test_fetch_price_path_rejects_non_positive_lookback() -> None:
    """A non-positive lookback is a programmer error and fails fast."""
    with pytest.raises(ValueError, match="lookback"):
        fetch_path(fixture_backend(FixtureRpcTransport()), timedelta(0))


def test_build_price_path_supports_stock_as_token1() -> None:
    """A pool where USDC sorts first still prices the stock in USDC."""
    records = (
        SwapEventRecord(
            block_number=9,
            log_index=0,
            amount0=10,
            amount1=-20,
            sqrt_ratio=1 << 96,
            liquidity=1,
            tick=0,
        ),
    )
    path = build_price_path(
        pool_address=POOL_ADDRESS,
        token_address=B20_ADDRESS,
        token0_address=QUOTE_ADDRESS,
        token1_address=B20_ADDRESS,
        token_decimals=18,
        quote_decimals=6,
        from_block=9,
        to_block=9,
        observed_at=datetime.now(UTC),
        records=records,
        timestamps_by_block={9: fixture_block_timestamp(9)},
    )

    assert path.token_is_token0 is False
    # One raw price unit with USDC token0 and an 18-decimal stock still yields
    # the same 10^12 USDC-per-stock price as the mirrored ordering.
    assert path.points[0].price_usdc == 10**12


def test_point_and_path_validators_reject_naive_or_inverted_inputs() -> None:
    """Naive timestamps and inverted windows fail model validation."""
    with pytest.raises(ValidationError, match="timezone-aware"):
        PoolPricePoint(
            timestamp=datetime.now(),
            block_number=1,
            log_index=0,
            amount0=1,
            amount1=-1,
            sqrt_ratio=1 << 96,
            liquidity=1,
            tick=0,
            price_usdc=Decimal(1),
        )
    aware = datetime.now(UTC)
    with pytest.raises(ValidationError, match="from_block"):
        PoolPricePath(
            pool_address=POOL_ADDRESS,
            token_address=B20_ADDRESS,
            token_is_token0=True,
            token_decimals=18,
            quote_decimals=6,
            from_block=2,
            to_block=1,
            observed_at=aware,
            points=(),
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        PoolPricePath(
            pool_address=POOL_ADDRESS,
            token_address=B20_ADDRESS,
            token_is_token0=True,
            token_decimals=18,
            quote_decimals=6,
            from_block=1,
            to_block=1,
            observed_at=datetime.now(),
            points=(),
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        BlockHeader(number=1, timestamp=datetime.now())


def header_override(value: object) -> dict[str, object]:
    """Build one eth_getBlockByNumber result override.

    Args:
        value: The raw result object served for every header read.

    Returns:
        A one-entry override map for the fixture transport.
    """
    return {"eth_getBlockByNumber": value}


def test_fetch_price_path_fails_closed_on_garbage_head_number() -> None:
    """A non-string or invalid-hex head block number fails the reconstruction."""
    with pytest.raises(HistoryUnavailableError, match="did not return a string"):
        fetch_path(fixture_backend(FixtureRpcTransport(result_overrides={"eth_blockNumber": 123})))
    with pytest.raises(HistoryUnavailableError, match="invalid hex"):
        fetch_path(
            fixture_backend(FixtureRpcTransport(result_overrides={"eth_blockNumber": "0xzz"}))
        )


def test_fetch_price_path_fails_closed_on_garbage_log_responses() -> None:
    """A non-list eth_getLogs result fails the reconstruction."""
    fixture = fixture_backend(FixtureRpcTransport(result_overrides={"eth_getLogs": "not-a-list"}))

    with pytest.raises(HistoryUnavailableError, match="not a list"):
        fetch_path(fixture)


def test_fetch_price_path_fails_closed_on_garbage_block_headers() -> None:
    """Malformed header results fail the reconstruction with precise diagnostics."""
    garbage_headers: tuple[object, ...] = (
        "not-a-dict",
        {"number": "0xzz", "timestamp": "0x1"},
        {"number": "0x1", "timestamp": "0xzz"},
        {"number": "-0x1", "timestamp": "0x1"},
    )
    for value in garbage_headers:
        with pytest.raises(HistoryUnavailableError, match="header was malformed"):
            fetch_path(
                fixture_backend(FixtureRpcTransport(result_overrides=header_override(value)))
            )


def test_fetch_price_path_fails_closed_on_mismatched_header_number() -> None:
    """A header served for a different block fails the reconstruction."""
    overrides = header_override({"number": "0x2", "timestamp": "0x1"})

    with pytest.raises(HistoryUnavailableError, match="returned block"):
        fetch_path(fixture_backend(FixtureRpcTransport(result_overrides=overrides)))


def test_fetch_price_path_fails_closed_on_binary_search_probe_bound() -> None:
    """A search space needing more than the probe bound fails closed."""
    fixture = fixture_backend(FixtureRpcTransport(latest_block=2**200))

    with pytest.raises(HistoryUnavailableError, match="probe bound"):
        fetch_path(fixture)


def test_fetch_price_path_pages_across_multiple_windows() -> None:
    """Logs from consecutive windows collect into one ordered path."""
    transport = FixtureRpcTransport(
        logs=[
            swap_log(996, 0, 1 << 96),
            swap_log(998, 0, 1 << 97),
        ]
    )
    fixture = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, log_window_blocks=2
    )
    path = fetch_path(fixture)

    assert [point.block_number for point in path.points] == [996, 998]
    # The tiny two-block windows cover 995..1000 across three reads.
    assert len(transport.single_calls("eth_getLogs")) == 3


def test_fetch_price_path_fails_closed_on_transport_failures() -> None:
    """HTTP-level and transport failures fail the reconstruction closed."""
    for mode, pattern in (
        ("transport_error", "transport error"),
        ("http_429", "HTTP status 429"),
        ("http_500", "HTTP status 500"),
        ("http_404", "unexpected HTTP status 404"),
        ("not_json", "not valid JSON"),
        ("empty_body", "neither result nor error"),
        ("fatal_rpc_error", "RPC error -32601"),
    ):
        with pytest.raises(HistoryUnavailableError, match=pattern):
            fetch_path(fixture_backend(FixtureRpcTransport(failure_mode=mode)))


def test_fetch_price_path_fails_closed_on_oversized_responses() -> None:
    """A response above the configured byte bound fails closed."""
    fixture = EventHistoryRpcBackend(
        transport=FixtureRpcTransport(),
        sleep=no_sleep,
        page_delay_seconds=0.0,
        max_response_bytes=1,
    )

    with pytest.raises(HistoryUnavailableError, match="above the configured limit"):
        fetch_path(fixture)


def test_fetch_price_path_fails_closed_after_exhausted_retries() -> None:
    """Persistent rate limiting exhausts the bounded retry attempts."""
    fixture = fixture_backend(FixtureRpcTransport(failures_before_success=99))

    with pytest.raises(HistoryUnavailableError, match="failed after 5 attempts"):
        fetch_path(fixture)


def test_bundled_slipstream_abi_matches_decoder_layout() -> None:
    """The vendored Swap fragment and the decoder layout cannot drift apart."""
    document = json.loads(
        files("aero_bot").joinpath("slipstream_pool_abi.json").read_text(encoding="utf-8")
    )
    events = [entry for entry in document if entry.get("type") == "event"]
    assert len(events) == 1
    event = events[0]
    assert event["name"] == "Swap"
    assert event["anonymous"] is False
    inputs = event["inputs"]
    # The two indexed address parameters precede the non-indexed data words.
    assert [(item["name"], item["type"], item["indexed"]) for item in inputs[:2]] == [
        ("sender", "address", True),
        ("recipient", "address", True),
    ]
    non_indexed = [(item["name"], item["type"], item["indexed"]) for item in inputs[2:]]
    assert non_indexed == [(name, abi_type, False) for name, abi_type in SWAP_DATA_LAYOUT]


def test_decode_gauge_stake_log_round_trips_both_directions() -> None:
    """Deposit and Withdraw entries decode with their exact liquidity delta."""
    deposit = decode_gauge_stake_log(gauge_log(123, 4, 987, is_deposit=True))
    withdraw = decode_gauge_stake_log(gauge_log(124, 0, 456, is_deposit=False))

    assert deposit == GaugeStakeEventRecord(
        block_number=123, log_index=4, is_deposit=True, liquidity=987
    )
    assert withdraw == GaugeStakeEventRecord(
        block_number=124, log_index=0, is_deposit=False, liquidity=456
    )
    # A zero delta is a real onchain withdraw after decreaseStakedLiquidity.
    zero = decode_gauge_stake_log(gauge_log(125, 1, 0, is_deposit=False))
    assert zero.liquidity == 0


def test_decode_gauge_stake_log_rejects_malformed_entries() -> None:
    """Structurally invalid stake logs fail closed instead of yielding records."""
    valid = gauge_log(1, 0, 100)
    # Each malformed candidate violates one structural invariant of the event.
    malformed = [
        "not-a-dict",
        {**valid, "removed": True},
        {**valid, "topics": valid["topics"][:3]},
        {**valid, "topics": ["0x" + "ab" * 32, *valid["topics"][1:]]},
        {**valid, "topics": [valid["topics"][0], "0xzz" + "0" * 62, *valid["topics"][2:]]},
        {**valid, "data": "0x" + "00" * 32},
        {**valid, "data": "not-hex"},
        {**valid, "blockNumber": "0x!"},
    ]
    for candidate in malformed:
        with pytest.raises(ValueError):
            decode_gauge_stake_log(candidate)


def test_staked_tvl_usd_values_both_balances_exactly() -> None:
    """Staked token balances convert to an exact USDC value at the pool price."""
    # 1500 USDC plus twenty shares at 25 USDC per share equals 2000 USDC.
    assert staked_tvl_usd(1_500_000_000, 6, 2_000_000_000, 8, Decimal("25")) == Decimal("2000")
    assert staked_tvl_usd(0, 6, 0, 8, Decimal("25")) == Decimal("0")
    with pytest.raises(ValueError, match="usdc_decimals"):
        staked_tvl_usd(0, 99, 0, 8, Decimal("25"))
    with pytest.raises(ValueError, match="stock_decimals"):
        staked_tvl_usd(0, 6, 0, 99, Decimal("25"))
    with pytest.raises(ValueError, match="stock_price_usdc"):
        staked_tvl_usd(0, 6, 0, 8, Decimal("0"))


def test_build_emissions_apr_history_folds_anchor_backwards() -> None:
    """Net stake events invert into the window-start liquidity exactly.

    The fixture anchors 2000 liquidity at block 100 with a withdraw of 300
    at block 98 and a deposit of 500 at block 99, so the window opens at
    1800 and each step's APR is the annual reward over the scaled staked
    value: 63072000 USDC per year over 2 USDC per liquidity unit.
    """
    records = (
        GaugeStakeEventRecord(block_number=99, log_index=5, is_deposit=True, liquidity=500),
        GaugeStakeEventRecord(block_number=98, log_index=0, is_deposit=False, liquidity=300),
    )
    history = build_emissions_apr_history(
        pool_address=POOL_ADDRESS,
        gauge_address=GAUGE_ADDRESS,
        from_block=98,
        to_block=100,
        anchor_block=100,
        observed_at=datetime.now(UTC),
        window_start_timestamp=fixture_block_timestamp(98),
        anchor_gauge_liquidity=2_000,
        anchor_staked_tvl_usd=Decimal("4000"),
        emissions_per_second=10**18,
        aero_price_assumption_usd=Decimal("2"),
        records=records,
        timestamps_by_block={98: fixture_block_timestamp(98), 99: fixture_block_timestamp(99)},
    )

    assert history.starting_gauge_liquidity == 1_800
    assert [step.gauge_liquidity for step in history.steps] == [1_800, 1_500, 2_000]
    assert [step.emissions_apr for step in history.steps] == [
        Decimal("17520"),
        Decimal("21024"),
        Decimal("15768"),
    ]
    # The opening step carries no onchain ordering evidence by design.
    assert history.steps[0].block_number is None
    assert history.steps[0].log_index is None
    assert history.steps[1].block_number == 98
    assert history.steps[2].log_index == 5
    assert history.steps[2].timestamp == fixture_block_timestamp(99)


def test_build_emissions_apr_history_orders_events_by_log_index() -> None:
    """Same-block events apply in log-index order, not record order."""
    records = (
        GaugeStakeEventRecord(block_number=98, log_index=1, is_deposit=True, liquidity=500),
        GaugeStakeEventRecord(block_number=98, log_index=0, is_deposit=False, liquidity=500),
    )
    history = build_emissions_apr_history(
        pool_address=POOL_ADDRESS,
        gauge_address=GAUGE_ADDRESS,
        from_block=98,
        to_block=98,
        anchor_block=98,
        observed_at=datetime.now(UTC),
        window_start_timestamp=fixture_block_timestamp(98),
        anchor_gauge_liquidity=2_000,
        anchor_staked_tvl_usd=Decimal("4000"),
        emissions_per_second=10**18,
        aero_price_assumption_usd=Decimal("2"),
        records=records,
        timestamps_by_block={98: fixture_block_timestamp(98)},
    )

    # The withdraw applies first, so liquidity dips before the deposit restores.
    assert [step.gauge_liquidity for step in history.steps] == [2_000, 1_500, 2_000]


def test_build_emissions_apr_history_rejects_contradictory_anchors() -> None:
    """Events implying negative staked liquidity contradict the anchor."""
    common: dict[str, Any] = {
        "pool_address": POOL_ADDRESS,
        "gauge_address": GAUGE_ADDRESS,
        "from_block": 98,
        "to_block": 100,
        "anchor_block": 100,
        "observed_at": datetime.now(UTC),
        "window_start_timestamp": fixture_block_timestamp(98),
        "anchor_gauge_liquidity": 2_000,
        "anchor_staked_tvl_usd": Decimal("4000"),
        "emissions_per_second": 10**18,
        "aero_price_assumption_usd": Decimal("2"),
    }
    # Net deposits above the anchor drive the window-start level negative.
    with pytest.raises(ValueError, match="negative before the window"):
        build_emissions_apr_history(
            **common,
            records=(
                GaugeStakeEventRecord(
                    block_number=99, log_index=0, is_deposit=True, liquidity=5_000
                ),
            ),
            timestamps_by_block={99: fixture_block_timestamp(99)},
        )
    # A huge early withdraw drives an intermediate level negative.
    with pytest.raises(ValueError, match="went negative at block 98"):
        build_emissions_apr_history(
            **common,
            records=(
                GaugeStakeEventRecord(
                    block_number=98, log_index=0, is_deposit=False, liquidity=9_000
                ),
                GaugeStakeEventRecord(
                    block_number=99, log_index=0, is_deposit=True, liquidity=9_000
                ),
            ),
            timestamps_by_block={
                98: fixture_block_timestamp(98),
                99: fixture_block_timestamp(99),
            },
        )


def test_build_emissions_apr_history_rejects_zero_liquidity_steps() -> None:
    """A window draining every staked share leaves the APR undefined."""
    records = (
        # The 4000 withdraw drains the 4000 window-start level exactly, and
        # the later deposit restores the 2000 anchor.
        GaugeStakeEventRecord(block_number=98, log_index=0, is_deposit=False, liquidity=4_000),
        GaugeStakeEventRecord(block_number=99, log_index=0, is_deposit=True, liquidity=2_000),
    )
    with pytest.raises(ValueError, match="zero staked liquidity"):
        build_emissions_apr_history(
            pool_address=POOL_ADDRESS,
            gauge_address=GAUGE_ADDRESS,
            from_block=98,
            to_block=100,
            anchor_block=100,
            observed_at=datetime.now(UTC),
            window_start_timestamp=fixture_block_timestamp(98),
            anchor_gauge_liquidity=2_000,
            anchor_staked_tvl_usd=Decimal("4000"),
            emissions_per_second=10**18,
            aero_price_assumption_usd=Decimal("2"),
            records=records,
            timestamps_by_block={
                98: fixture_block_timestamp(98),
                99: fixture_block_timestamp(99),
            },
        )


def test_build_emissions_apr_history_rejects_missing_timestamps() -> None:
    """An event block without a header timestamp fails closed."""
    records = (GaugeStakeEventRecord(block_number=99, log_index=0, is_deposit=True, liquidity=500),)
    with pytest.raises(ValueError, match="lacks a header timestamp"):
        build_emissions_apr_history(
            pool_address=POOL_ADDRESS,
            gauge_address=GAUGE_ADDRESS,
            from_block=98,
            to_block=100,
            anchor_block=100,
            observed_at=datetime.now(UTC),
            window_start_timestamp=fixture_block_timestamp(98),
            anchor_gauge_liquidity=2_000,
            anchor_staked_tvl_usd=Decimal("4000"),
            emissions_per_second=10**18,
            aero_price_assumption_usd=Decimal("2"),
            records=records,
            timestamps_by_block={},
        )


def test_build_emissions_apr_history_rejects_non_positive_anchor_inputs() -> None:
    """Non-positive anchor values fail before any folding begins."""
    base: dict[str, Any] = {
        "pool_address": POOL_ADDRESS,
        "gauge_address": GAUGE_ADDRESS,
        "from_block": 98,
        "to_block": 100,
        "anchor_block": 100,
        "observed_at": datetime.now(UTC),
        "window_start_timestamp": fixture_block_timestamp(98),
        "records": (),
        "timestamps_by_block": {},
    }
    with pytest.raises(ValueError, match="anchor_gauge_liquidity"):
        build_emissions_apr_history(
            **base,
            anchor_gauge_liquidity=0,
            anchor_staked_tvl_usd=Decimal("4000"),
            emissions_per_second=10**18,
            aero_price_assumption_usd=Decimal("2"),
        )
    with pytest.raises(ValueError, match="anchor_staked_tvl_usd"):
        build_emissions_apr_history(
            **base,
            anchor_gauge_liquidity=2_000,
            anchor_staked_tvl_usd=Decimal("0"),
            emissions_per_second=10**18,
            aero_price_assumption_usd=Decimal("2"),
        )
    with pytest.raises(ValueError, match="aero_price_assumption_usd"):
        build_emissions_apr_history(
            **base,
            anchor_gauge_liquidity=2_000,
            anchor_staked_tvl_usd=Decimal("4000"),
            emissions_per_second=10**18,
            aero_price_assumption_usd=Decimal("0"),
        )


def test_emissions_models_reject_invalid_steps_and_windows() -> None:
    """Step and history validators fail closed on inconsistent evidence."""
    aware = datetime.now(UTC)
    with pytest.raises(ValidationError, match="timezone-aware"):
        EmissionsAprPoint(
            timestamp=datetime.now(),
            gauge_liquidity=100,
            emissions_apr=Decimal("1"),
        )
    with pytest.raises(ValidationError, match="together"):
        EmissionsAprPoint(
            timestamp=aware,
            block_number=1,
            gauge_liquidity=100,
            emissions_apr=Decimal("1"),
        )
    valid_step = EmissionsAprPoint(timestamp=aware, gauge_liquidity=100, emissions_apr=Decimal("1"))
    later_step = EmissionsAprPoint(
        timestamp=aware + timedelta(seconds=1),
        gauge_liquidity=90,
        emissions_apr=Decimal("2"),
    )
    history_fields: dict[str, Any] = {
        "pool_address": POOL_ADDRESS,
        "gauge_address": GAUGE_ADDRESS,
        "from_block": 98,
        "to_block": 100,
        "anchor_block": 100,
        "observed_at": aware,
        "anchor_gauge_liquidity": 2_000,
        "anchor_staked_tvl_usd": Decimal("4000"),
        "emissions_per_second": 10**18,
        "aero_price_assumption_usd": Decimal("2"),
        "starting_gauge_liquidity": 1_800,
    }
    with pytest.raises(ValidationError, match="anchor_block"):
        EmissionsAprHistory(
            **{**history_fields, "anchor_block": 101},
            reconstruction_mode="event_fold",
            steps=(valid_step,),
        )
    with pytest.raises(ValidationError, match="non-decreasing"):
        EmissionsAprHistory(
            **history_fields,
            reconstruction_mode="event_fold",
            steps=(later_step, valid_step),
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        EmissionsAprHistory(
            **{**history_fields, "observed_at": datetime.now()},
            reconstruction_mode="event_fold",
            steps=(valid_step,),
        )
    with pytest.raises(ValidationError, match="exactly one step"):
        EmissionsAprHistory(
            **history_fields,
            reconstruction_mode="constant_anchor_apr",
            steps=(valid_step, later_step),
        )


def test_fetch_emissions_apr_history_reconstructs_exact_steps() -> None:
    """The anchored fetch folds events, stamps headers, and skips head reads."""
    transport = FixtureRpcTransport(
        logs=[
            gauge_log(998, 0, 800, is_deposit=True),
            gauge_log(996, 3, 300, is_deposit=False),
        ]
    )
    history = fetch_emissions(fixture_backend(transport))

    assert history.from_block == 995
    assert history.to_block == FIXTURE_LATEST_BLOCK
    assert history.anchor_block == FIXTURE_LATEST_BLOCK
    assert history.reconstruction_mode == "event_fold"
    assert history.starting_gauge_liquidity == 1_500
    assert [step.gauge_liquidity for step in history.steps] == [1_500, 1_200, 2_000]
    # Annual reward of 63072000 USDC over 2 USDC per liquidity unit.
    assert [step.emissions_apr for step in history.steps] == [
        Decimal("21024"),
        Decimal("26280"),
        Decimal("15768"),
    ]
    assert history.steps[1].timestamp == fixture_block_timestamp(996)
    assert history.steps[1].log_index == 3
    # The anchored fetch never reads the chain head.
    assert not transport.single_calls("eth_blockNumber")
    # Every distinct block header was read exactly once across search and events.
    assert set(transport.header_calls_by_block.values()) == {1}


def test_fetch_emissions_apr_history_without_events_returns_constant_series() -> None:
    """A quiet gauge reconstructs to one window-opening step at anchor level."""
    history = fetch_emissions(fixture_backend(FixtureRpcTransport()))

    assert history.starting_gauge_liquidity == 2_000
    assert len(history.steps) == 1
    assert history.steps[0].emissions_apr == Decimal("15768")
    assert history.steps[0].block_number is None


def test_fetch_emissions_apr_history_fails_closed_on_malformed_gauge_logs() -> None:
    """One malformed stake log in a window fails the whole reconstruction."""
    broken = gauge_log(996, 0, 100)
    broken["data"] = "0x" + "00" * 32

    with pytest.raises(HistoryUnavailableError, match="malformed gauge stake log"):
        fetch_emissions(fixture_backend(FixtureRpcTransport(logs=[broken])))


def test_fetch_emissions_apr_history_fails_closed_on_foreign_gauge_logs() -> None:
    """A log served from another gauge address fails the reconstruction."""
    logs = [gauge_log(996, 0, 100, gauge_address=OTHER_GAUGE_ADDRESS)]

    with pytest.raises(HistoryUnavailableError, match="another contract"):
        fetch_emissions(fixture_backend(FixtureRpcTransport(logs=logs)))


def test_fetch_emissions_apr_history_fails_closed_at_window_log_bound() -> None:
    """A single block at the log bound fails closed because no split can rule truncation out."""
    transport = FixtureRpcTransport(logs=[gauge_log(996, 0, 100), gauge_log(996, 1, 100)])
    fixture = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, max_logs_per_window=2
    )

    with pytest.raises(HistoryUnavailableError, match="single-block window 996"):
        fetch_emissions(fixture)


def test_fetch_emissions_apr_history_refines_busy_windows() -> None:
    """Stake events spanning blocks survive a bounded window's refinement."""
    transport = FixtureRpcTransport(
        logs=[gauge_log(996, 0, 300, is_deposit=False), gauge_log(998, 0, 800, is_deposit=True)]
    )
    fixture = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, max_logs_per_window=2
    )

    history = fetch_emissions(fixture)

    # Both stake events fold into the series exactly as mined.
    assert [step.gauge_liquidity for step in history.steps] == [1_500, 1_200, 2_000]


def test_fetch_emissions_apr_history_fails_closed_at_total_event_bound() -> None:
    """A gauge decoding past the total event bound fails the reconstruction closed."""
    transport = FixtureRpcTransport(logs=[gauge_log(996, 0, 100), gauge_log(998, 0, 100)])
    fixture = EventHistoryRpcBackend(
        transport=transport,
        sleep=no_sleep,
        page_delay_seconds=0.0,
        max_total_gauge_stake_events=1,
    )

    with pytest.raises(HistoryUnavailableError, match="exceeded 1 decoded gauge stake events"):
        fetch_emissions(fixture)


def test_fetch_emissions_apr_history_falls_back_on_contradictory_events() -> None:
    """Events contradicting the anchor yield the labeled constant-APR fallback.

    Live B20 gauges hit this routinely because staked positions also change
    liquidity through the position manager, which stake events do not carry.
    """
    transport = FixtureRpcTransport(logs=[gauge_log(996, 0, 500_000, is_deposit=True)])
    history = fetch_emissions(fixture_backend(transport))

    assert history.reconstruction_mode == "constant_anchor_apr"
    assert len(history.steps) == 1
    # The fallback holds the anchor's exact level and APR across the window.
    assert history.starting_gauge_liquidity == 2_000
    assert history.steps[0].gauge_liquidity == 2_000
    assert history.steps[0].emissions_apr == Decimal("15768")
    assert history.steps[0].block_number is None


def test_fetch_emissions_apr_history_pages_across_multiple_windows() -> None:
    """Stake events from consecutive windows collect into one ordered series."""
    transport = FixtureRpcTransport(
        logs=[
            gauge_log(996, 0, 300, is_deposit=False),
            gauge_log(998, 0, 800, is_deposit=True),
        ]
    )
    fixture = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, log_window_blocks=2
    )
    history = fetch_emissions(fixture)

    assert [step.gauge_liquidity for step in history.steps] == [1_500, 1_200, 2_000]
    # The tiny two-block windows cover 995..1000 across three reads.
    assert len(transport.single_calls("eth_getLogs")) == 3


def test_fetch_emissions_apr_history_rejects_invalid_inputs() -> None:
    """Invalid fetch inputs fail fast before any request is made."""
    fixture = fixture_backend(FixtureRpcTransport())
    with pytest.raises(ValueError, match="lookback"):
        fetch_emissions(fixture, timedelta(0))
    with pytest.raises(ValueError, match="anchor_block"):
        fixture.fetch_emissions_apr_history(
            pool_address=POOL_ADDRESS,
            gauge_address=GAUGE_ADDRESS,
            anchor_block=-1,
            anchor_gauge_liquidity=2_000,
            anchor_staked_tvl_usd=Decimal("4000"),
            emissions_per_second=10**18,
            aero_price_assumption_usd=Decimal("2"),
        )
    with pytest.raises(ValueError, match="anchor_gauge_liquidity"):
        fixture.fetch_emissions_apr_history(
            pool_address=POOL_ADDRESS,
            gauge_address=GAUGE_ADDRESS,
            anchor_block=FIXTURE_LATEST_BLOCK,
            anchor_gauge_liquidity=0,
            anchor_staked_tvl_usd=Decimal("4000"),
            emissions_per_second=10**18,
            aero_price_assumption_usd=Decimal("2"),
        )
    with pytest.raises(ValueError, match="anchor_staked_tvl_usd"):
        fixture.fetch_emissions_apr_history(
            pool_address=POOL_ADDRESS,
            gauge_address=GAUGE_ADDRESS,
            anchor_block=FIXTURE_LATEST_BLOCK,
            anchor_gauge_liquidity=2_000,
            anchor_staked_tvl_usd=Decimal("0"),
            emissions_per_second=10**18,
            aero_price_assumption_usd=Decimal("2"),
        )
    with pytest.raises(ValueError, match="emissions_per_second"):
        fixture.fetch_emissions_apr_history(
            pool_address=POOL_ADDRESS,
            gauge_address=GAUGE_ADDRESS,
            anchor_block=FIXTURE_LATEST_BLOCK,
            anchor_gauge_liquidity=2_000,
            anchor_staked_tvl_usd=Decimal("4000"),
            emissions_per_second=-1,
            aero_price_assumption_usd=Decimal("2"),
        )
    with pytest.raises(ValueError, match="aero_price_assumption_usd"):
        fixture.fetch_emissions_apr_history(
            pool_address=POOL_ADDRESS,
            gauge_address=GAUGE_ADDRESS,
            anchor_block=FIXTURE_LATEST_BLOCK,
            anchor_gauge_liquidity=2_000,
            anchor_staked_tvl_usd=Decimal("4000"),
            emissions_per_second=10**18,
            aero_price_assumption_usd=Decimal("0"),
        )


def test_bundled_slipstream_gauge_abi_matches_decoder_layout() -> None:
    """The vendored gauge fragments and the decoder layout cannot drift apart."""
    document = json.loads(
        files("aero_bot").joinpath("slipstream_gauge_abi.json").read_text(encoding="utf-8")
    )
    events = [entry for entry in document if entry.get("type") == "event"]
    assert len(events) == 2
    assert {event["name"] for event in events} == {"Deposit", "Withdraw"}
    for event in events:
        assert event["anonymous"] is False
        indexed = [(item["name"], item["type"], item["indexed"]) for item in event["inputs"]]
        # Every parameter is indexed in the order the decoder expects.
        assert indexed == [(name, abi_type, True) for name, abi_type in GAUGE_STAKE_TOPICS_LAYOUT]
        assert len(indexed) + 1 == GAUGE_STAKE_TOPIC_COUNT


def test_backend_rejects_out_of_range_header_batch_size() -> None:
    """The header batch size must stay inside the public endpoint's cap."""
    with pytest.raises(ValueError, match="header_batch_size"):
        EventHistoryRpcBackend(header_batch_size=0)
    with pytest.raises(ValueError, match="header_batch_size"):
        EventHistoryRpcBackend(header_batch_size=MAX_HEADER_BATCH_SIZE + 1)


def test_read_erc20_decimals_serves_one_exact_word() -> None:
    """The decimals read targets the token with the standard selector."""
    # B20 tokens carry eight decimals and native USDC carries six.
    transport = FixtureRpcTransport(
        erc20_decimals={
            B20_ADDRESS: "0x" + encode_word(8).hex(),
            QUOTE_ADDRESS: "0x" + encode_word(6).hex(),
        }
    )
    backend = fixture_backend(transport)

    assert backend.read_erc20_decimals(B20_ADDRESS) == 8
    # A mixed-case spelling normalizes to the same token before the call.
    assert backend.read_erc20_decimals("0xB20000000000000000000078EE7CE2FE4908108C") == 8
    assert backend.read_erc20_decimals(QUOTE_ADDRESS) == 6
    # Both calls send one read-only eth_call to the token contract.
    for call in transport.calls:
        assert isinstance(call, dict)
        assert call["method"] == "eth_call"
        assert call["params"][0]["data"] == ERC20_DECIMALS_SELECTOR
        assert call["params"][1] == "latest"


def test_read_erc20_decimals_fails_closed_on_malformed_evidence() -> None:
    """Malformed, absent, and out-of-bound decimals answers never reach the math."""
    # An unmapped token is served a non-word answer and a mapped one overflows.
    transport = FixtureRpcTransport(erc20_decimals={B20_ADDRESS: "0x" + encode_word(2**40).hex()})
    backend = fixture_backend(transport)

    with pytest.raises(HistoryUnavailableError, match="exactly one ABI word"):
        backend.read_erc20_decimals(QUOTE_ADDRESS)
    with pytest.raises(HistoryUnavailableError, match="above the documented bound"):
        backend.read_erc20_decimals(B20_ADDRESS)


def test_batched_header_prefetch_groups_header_reads() -> None:
    """Event-window headers are prefetched through bounded batch requests."""
    logs = [swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96), swap_log(998, 0, 1 << 96)]
    transport = FixtureRpcTransport(logs=logs)
    backend = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, header_batch_size=2
    )

    path = fetch_path(backend)

    assert [point.block_number for point in path.points] == [996, 997, 998]
    # Every event block was served exactly once despite the grouped requests.
    assert transport.header_calls_by_block[996] == 1
    assert transport.header_calls_by_block[997] == 1
    assert transport.header_calls_by_block[998] == 1
    # Each batch carries at most the configured header reads.
    assert transport.batch_calls
    assert all(len(batch) <= 2 for batch in transport.batch_calls)
    assert all(
        entry["method"] == "eth_getBlockByNumber"
        for batch in transport.batch_calls
        for entry in batch
    )


def test_batched_prefetch_matches_the_unbatched_path() -> None:
    """Batching changes only the wire shape, never the reconstructed evidence."""
    logs = [
        swap_log(995, 0, 1 << 96),
        swap_log(996, 1, 1 << 96),
        swap_log(998, 0, 1 << 96),
    ]
    unbatched = fetch_path(
        EventHistoryRpcBackend(
            transport=FixtureRpcTransport(logs=logs), sleep=no_sleep, page_delay_seconds=0.0
        )
    )
    batched = fetch_path(
        EventHistoryRpcBackend(
            transport=FixtureRpcTransport(logs=logs),
            sleep=no_sleep,
            page_delay_seconds=0.0,
            header_batch_size=MAX_HEADER_BATCH_SIZE,
        )
    )

    assert batched.points == unbatched.points
    assert batched.from_block == unbatched.from_block
    assert batched.to_block == unbatched.to_block


def test_batched_prefetch_retries_rate_limited_entries() -> None:
    """One rate-limited batch entry retries the whole batch before failing."""
    # Blocks 996, 998, and 999 are never binary-search probes, so all three
    # arrive through the batched prefetch path.
    logs = [swap_log(996, 0, 1 << 96), swap_log(998, 0, 1 << 96), swap_log(999, 0, 1 << 96)]
    transport = FixtureRpcTransport(logs=logs, batch_entry_failures_before_success=1)
    backend = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, header_batch_size=2
    )

    path = fetch_path(backend)

    assert len(path.points) == 3
    # The first batch was served one rate-limited entry and retried in full,
    # then the remaining header completed the window.
    assert len(transport.batch_calls) == 3
    assert transport.batch_calls[0] == transport.batch_calls[1]


def test_batched_prefetch_fails_closed_on_count_mismatch() -> None:
    """A truncated batch response never reaches the header cache."""
    logs = [swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96), swap_log(998, 0, 1 << 96)]
    transport = FixtureRpcTransport(
        logs=logs,
        batch_result_override=[
            {"jsonrpc": "2.0", "id": 1, "result": {"number": "0x3e4", "timestamp": "0x2"}}
        ],
    )
    backend = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, header_batch_size=2
    )

    with pytest.raises(HistoryUnavailableError, match="held 1 entries for 2 requests"):
        fetch_path(backend)


def test_batched_prefetch_fails_closed_on_endpoint_batch_rejection() -> None:
    """A non-list batch body is the endpoint's own rejection and fails closed."""
    logs = [swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96), swap_log(998, 0, 1 << 96)]
    transport = FixtureRpcTransport(
        logs=logs,
        batch_result_override={
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32014, "message": "maximum 10 calls in 1 batch"},
        },
    )
    backend = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, header_batch_size=2
    )

    with pytest.raises(HistoryUnavailableError, match="a non-list body"):
        fetch_path(backend)


def test_batched_prefetch_fails_closed_on_id_substitution() -> None:
    """Batch entries answering ids the run never sent are rejected."""
    logs = [swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96), swap_log(998, 0, 1 << 96)]
    transport = FixtureRpcTransport(
        logs=logs,
        batch_result_override=[
            {"jsonrpc": "2.0", "id": 1, "result": {"number": "0x3e4", "timestamp": "0x2"}},
            {"jsonrpc": "2.0", "id": 7, "result": {"number": "0x3e5", "timestamp": "0x2"}},
        ],
    )
    backend = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, header_batch_size=2
    )

    with pytest.raises(HistoryUnavailableError, match="did not cover every request"):
        fetch_path(backend)


def test_batched_prefetch_fails_closed_on_mismatched_header_numbers() -> None:
    """A batch entry answering a different block never reaches the cache."""
    logs = [swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96), swap_log(998, 0, 1 << 96)]
    transport = FixtureRpcTransport(
        logs=logs,
        batch_result_override=[
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"number": hex(996), "timestamp": hex(1_756_000_000)},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"number": hex(990), "timestamp": hex(1_756_000_000)},
            },
        ],
    )
    backend = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, header_batch_size=2
    )

    with pytest.raises(HistoryUnavailableError, match="returned block 990"):
        fetch_path(backend)


def test_batched_prefetch_fails_closed_after_exhausted_retries() -> None:
    """Persistent entry rate limiting exhausts the bounded batch retries."""
    logs = [swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96), swap_log(998, 0, 1 << 96)]
    transport = FixtureRpcTransport(logs=logs, batch_entry_failures_before_success=99)
    backend = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, header_batch_size=2
    )

    with pytest.raises(HistoryUnavailableError, match="failed after 5 attempts"):
        fetch_path(backend)


@pytest.mark.parametrize(
    ("mode", "pattern"),
    [
        ("transport_error", "transport error"),
        ("http_429", "failed after 5 attempts: HTTP status 429"),
        ("http_500", "HTTP status 500"),
        ("http_404", "unexpected HTTP status 404"),
        ("not_json", "not valid JSON"),
    ],
)
def test_batched_prefetch_fails_closed_on_transport_failures(mode: str, pattern: str) -> None:
    """Transport-level batch failures fail the reconstruction closed."""
    logs = [swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96), swap_log(998, 0, 1 << 96)]
    transport = FixtureRpcTransport(logs=logs, batch_failure_mode=mode)
    backend = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, header_batch_size=2
    )

    with pytest.raises(HistoryUnavailableError, match=pattern):
        fetch_path(backend)


def test_batched_prefetch_fails_closed_on_oversized_responses() -> None:
    """A batch response above the configured byte bound fails closed."""
    logs = [swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96), swap_log(998, 0, 1 << 96)]
    transport = FixtureRpcTransport(logs=logs, batch_failure_mode="oversized")
    backend = EventHistoryRpcBackend(
        transport=transport,
        sleep=no_sleep,
        page_delay_seconds=0.0,
        max_response_bytes=16,
        header_batch_size=2,
    )

    with pytest.raises(HistoryUnavailableError, match="above the configured limit"):
        fetch_path(backend)


@pytest.mark.parametrize(
    ("entries", "pattern"),
    [
        # A non-dict entry carries no integer id to match any request.
        (["not-an-entry", {"jsonrpc": "2.0", "id": 2, "result": {}}], "integer id"),
        # A string id echoes nothing the run sent as an integer.
        (
            [
                {"jsonrpc": "2.0", "id": "1", "result": {}},
                {"jsonrpc": "2.0", "id": 2, "result": {}},
            ],
            "integer id",
        ),
        # An entry with neither a result nor an error body is unusable.
        (
            [
                {"jsonrpc": "2.0", "id": 1},
                {"jsonrpc": "2.0", "id": 2, "result": {"number": hex(997), "timestamp": "0x1"}},
            ],
            "neither result nor error",
        ),
        # A non-rate-limit entry error fails immediately without retries.
        (
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32601, "message": "method not found"},
                },
                {"jsonrpc": "2.0", "id": 2, "result": {"number": hex(997), "timestamp": "0x1"}},
            ],
            "RPC error -32601 on batched eth_getBlockByNumber",
        ),
    ],
)
def test_batched_prefetch_fails_closed_on_malformed_entries(
    entries: list[object], pattern: str
) -> None:
    """Malformed batch entries never reach the header cache."""
    logs = [swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96), swap_log(998, 0, 1 << 96)]
    transport = FixtureRpcTransport(logs=logs, batch_result_override=entries)
    backend = EventHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, header_batch_size=2
    )

    with pytest.raises(HistoryUnavailableError, match=pattern):
        fetch_path(backend)
