"""Behavior tests for the read-only Swap-event price-path reconstruction."""

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
    REHEARSAL_LOOKBACK,
    SWAP_DATA_LAYOUT,
    SWAP_EVENT_TOPIC0,
    BlockHeader,
    PoolPricePath,
    PoolPricePoint,
    SwapEventRecord,
    SwapHistoryRpcBackend,
    SwapHistoryUnavailableError,
    build_price_path,
    decode_swap_log,
    price_usdc_per_stock,
)

# Fixture identities mirror official contracts without claiming live observations.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
POOL_ADDRESS = "0x1111111111111111111111111111111111111111"
OTHER_POOL_ADDRESS = "0x1313131313131313131313131313131313131313"
SENDER_ADDRESS = "0x2111111111111111111111111111111111111111"
RECIPIENT_ADDRESS = "0x2121212121212121212121212121212121212121"
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
) -> dict[str, Any]:
    """Build one well-formed raw Slipstream Swap log entry.

    Args:
        block_number: Fixture block the swap was mined in.
        log_index: Log position inside the block.
        sqrt_ratio: Raw post-swap square-root price.
        liquidity: Raw active liquidity after the swap.
        tick: Signed post-swap tick.
        pool_address: Pool the log was emitted from.

    Returns:
        A JSON-RPC log object shaped exactly like an eth_getLogs entry.
    """
    data = b"".join(
        (
            encode_word(10),
            encode_word(-20),
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


class FixtureRpcTransport(httpx.MockTransport):
    """Serve deterministic JSON-RPC responses without touching the network."""

    def __init__(
        self,
        logs: list[dict[str, Any]] | None = None,
        failures_before_success: int = 0,
        latest_block: int = FIXTURE_LATEST_BLOCK,
        result_overrides: dict[str, object] | None = None,
        failure_mode: str | None = None,
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
        """
        # Request counting lets tests assert dedup and retry behavior exactly.
        self.calls: list[dict[str, Any]] = []
        self.header_calls_by_block: dict[int, int] = {}
        self._logs = logs or []
        self._remaining_failures = failures_before_success
        self._latest_block = latest_block
        self._result_overrides = result_overrides or {}
        self._failure_mode = failure_mode
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
        method = payload["method"]
        if method in self._result_overrides:
            return self._result(self._result_overrides[method])
        if method == "eth_blockNumber":
            return self._result(hex(self._latest_block))
        if method == "eth_getBlockByNumber":
            return self._handle_block_header(payload["params"][0])
        if method == "eth_getLogs":
            return self._handle_logs(payload["params"][0])
        raise AssertionError(f"unexpected RPC method {method}")

    def _handle_block_header(self, block_hex: str) -> httpx.Response:
        """Serve one block header under the fixture cadence."""
        block_number = int(block_hex, 16)
        self.header_calls_by_block[block_number] = (
            self.header_calls_by_block.get(block_number, 0) + 1
        )
        # Enormous probe positions clamp to the cadence range so synthetic
        # search spaces stay representable as timestamps.
        cadence_block = min(block_number, FIXTURE_LATEST_BLOCK)
        return self._result(
            {
                "number": hex(block_number),
                "timestamp": hex(int(fixture_block_timestamp(cadence_block).timestamp())),
            }
        )

    def _handle_logs(self, query: dict[str, Any]) -> httpx.Response:
        """Serve the fixture logs inside one queried block window."""
        from_block = int(query["fromBlock"], 16)
        to_block = int(query["toBlock"], 16)
        matching = [
            log for log in self._logs if from_block <= int(log["blockNumber"], 16) <= to_block
        ]
        return self._result(matching)

    def _result(self, result: object) -> httpx.Response:
        """Wrap one value as a successful JSON-RPC response."""
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def no_sleep(_seconds: float) -> None:
    """Discard retry delays so failure-path tests run instantly."""
    return None


def fixture_backend(transport: FixtureRpcTransport) -> SwapHistoryRpcBackend:
    """Build one offline backend around a fixture transport.

    Args:
        transport: The deterministic fixture endpoint.

    Returns:
        A backend wired to the fixture with instant politeness delays.
    """
    return SwapHistoryRpcBackend(transport=transport, sleep=no_sleep, page_delay_seconds=0.0)


def fetch_path(
    fixture_backend_instance: SwapHistoryRpcBackend,
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


def test_decode_swap_log_round_trips_one_event() -> None:
    """A well-formed log decodes with the negative tick sign-extended exactly."""
    record = decode_swap_log(swap_log(123, 4, 1 << 96, 987, -77))

    assert record == SwapEventRecord(
        block_number=123,
        log_index=4,
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
        SwapEventRecord(block_number=10, log_index=1, sqrt_ratio=1 << 96, liquidity=5, tick=1),
        SwapEventRecord(block_number=9, log_index=7, sqrt_ratio=1 << 97, liquidity=6, tick=2),
        SwapEventRecord(block_number=10, log_index=0, sqrt_ratio=1 << 95, liquidity=7, tick=3),
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
        SwapEventRecord(block_number=9, log_index=0, sqrt_ratio=1 << 96, liquidity=1, tick=0),
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
        SwapEventRecord(block_number=9, log_index=0, sqrt_ratio=1 << 96, liquidity=1, tick=0),
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
    block_number_calls = [call for call in transport.calls if call["method"] == "eth_blockNumber"]
    assert len(block_number_calls) == 3


def test_fetch_price_path_fails_closed_at_window_log_bound() -> None:
    """A window returning at least the configured log bound fails closed."""
    transport = FixtureRpcTransport(logs=[swap_log(996, 0, 1 << 96), swap_log(997, 0, 1 << 96)])
    fixture = SwapHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, max_logs_per_window=2
    )

    with pytest.raises(SwapHistoryUnavailableError, match="window bound"):
        fetch_path(fixture)


def test_fetch_price_path_fails_closed_on_malformed_logs() -> None:
    """One malformed log entry in a window fails the whole reconstruction."""
    broken = swap_log(996, 0, 1 << 96)
    broken["data"] = "0x" + "00" * 31

    with pytest.raises(SwapHistoryUnavailableError, match="malformed Swap log"):
        fetch_path(fixture_backend(FixtureRpcTransport(logs=[broken])))


def test_fetch_price_path_fails_closed_on_foreign_pool_logs() -> None:
    """A log served from another pool address fails the reconstruction."""
    logs = [swap_log(996, 0, 1 << 96, pool_address=OTHER_POOL_ADDRESS)]

    with pytest.raises(SwapHistoryUnavailableError, match="another pool"):
        fetch_path(fixture_backend(FixtureRpcTransport(logs=logs)))


def test_fetch_price_path_fails_closed_on_header_lookup_bound() -> None:
    """The cumulative header-lookup bound fails the reconstruction closed."""
    transport = FixtureRpcTransport(
        logs=[swap_log(block, 0, 1 << 96) for block in (996, 997, 998, 999)]
    )
    # The search itself reads about eleven headers, so this bound survives the
    # search and trips while resolving the swap blocks.
    fixture = SwapHistoryRpcBackend(
        transport=transport,
        sleep=no_sleep,
        page_delay_seconds=0.0,
        max_block_header_lookups=12,
    )

    with pytest.raises(SwapHistoryUnavailableError, match="lookups exceeded"):
        fetch_path(fixture)


def test_backend_constructor_rejects_invalid_bounds() -> None:
    """Every documented bound must be positive before any request is made."""
    invalid_constructors: tuple[tuple[str, Callable[[], SwapHistoryRpcBackend]], ...] = (
        ("timeout_seconds", lambda: SwapHistoryRpcBackend(timeout_seconds=0)),
        ("max_attempts", lambda: SwapHistoryRpcBackend(max_attempts=0)),
        ("page_delay_seconds", lambda: SwapHistoryRpcBackend(page_delay_seconds=-1)),
        ("max_response_bytes", lambda: SwapHistoryRpcBackend(max_response_bytes=0)),
        ("log_window_blocks", lambda: SwapHistoryRpcBackend(log_window_blocks=0)),
        ("max_logs_per_window", lambda: SwapHistoryRpcBackend(max_logs_per_window=0)),
        (
            "max_block_header_lookups",
            lambda: SwapHistoryRpcBackend(max_block_header_lookups=0),
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
        SwapEventRecord(block_number=9, log_index=0, sqrt_ratio=1 << 96, liquidity=1, tick=0),
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
    with pytest.raises(SwapHistoryUnavailableError, match="did not return a string"):
        fetch_path(fixture_backend(FixtureRpcTransport(result_overrides={"eth_blockNumber": 123})))
    with pytest.raises(SwapHistoryUnavailableError, match="invalid hex"):
        fetch_path(
            fixture_backend(FixtureRpcTransport(result_overrides={"eth_blockNumber": "0xzz"}))
        )


def test_fetch_price_path_fails_closed_on_garbage_log_responses() -> None:
    """A non-list eth_getLogs result fails the reconstruction."""
    fixture = fixture_backend(FixtureRpcTransport(result_overrides={"eth_getLogs": "not-a-list"}))

    with pytest.raises(SwapHistoryUnavailableError, match="not a list"):
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
        with pytest.raises(SwapHistoryUnavailableError, match="header was malformed"):
            fetch_path(
                fixture_backend(FixtureRpcTransport(result_overrides=header_override(value)))
            )


def test_fetch_price_path_fails_closed_on_mismatched_header_number() -> None:
    """A header served for a different block fails the reconstruction."""
    overrides = header_override({"number": "0x2", "timestamp": "0x1"})

    with pytest.raises(SwapHistoryUnavailableError, match="returned block"):
        fetch_path(fixture_backend(FixtureRpcTransport(result_overrides=overrides)))


def test_fetch_price_path_fails_closed_on_binary_search_probe_bound() -> None:
    """A search space needing more than the probe bound fails closed."""
    fixture = fixture_backend(FixtureRpcTransport(latest_block=2**200))

    with pytest.raises(SwapHistoryUnavailableError, match="probe bound"):
        fetch_path(fixture)


def test_fetch_price_path_pages_across_multiple_windows() -> None:
    """Logs from consecutive windows collect into one ordered path."""
    transport = FixtureRpcTransport(
        logs=[
            swap_log(996, 0, 1 << 96),
            swap_log(998, 0, 1 << 97),
        ]
    )
    fixture = SwapHistoryRpcBackend(
        transport=transport, sleep=no_sleep, page_delay_seconds=0.0, log_window_blocks=2
    )
    path = fetch_path(fixture)

    assert [point.block_number for point in path.points] == [996, 998]
    # The tiny two-block windows cover 995..1000 across three reads.
    log_queries = [call for call in transport.calls if call["method"] == "eth_getLogs"]
    assert len(log_queries) == 3


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
        with pytest.raises(SwapHistoryUnavailableError, match=pattern):
            fetch_path(fixture_backend(FixtureRpcTransport(failure_mode=mode)))


def test_fetch_price_path_fails_closed_on_oversized_responses() -> None:
    """A response above the configured byte bound fails closed."""
    fixture = SwapHistoryRpcBackend(
        transport=FixtureRpcTransport(),
        sleep=no_sleep,
        page_delay_seconds=0.0,
        max_response_bytes=1,
    )

    with pytest.raises(SwapHistoryUnavailableError, match="above the configured limit"):
        fetch_path(fixture)


def test_fetch_price_path_fails_closed_after_exhausted_retries() -> None:
    """Persistent rate limiting exhausts the bounded retry attempts."""
    fixture = fixture_backend(FixtureRpcTransport(failures_before_success=99))

    with pytest.raises(SwapHistoryUnavailableError, match="failed after 5 attempts"):
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
