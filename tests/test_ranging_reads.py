"""Regression tests for the ranging read's public-gateway log-window boundary.

The production unsigned preflight of main 52d7846 proved the deployed public
Tenderly gateway rejects every eth_getLogs span above 1,000 blocks with
RPC error -32602 (invalid params) regardless of filter shape, while spans at
or below 1,000 succeed, so every board entry deferred through
``range_evidence_deferred``. These tests pin that boundary: the transport
reproduces the exact observed rejection shape, the pre-fix 2,000-block
window fails closed with the exact production journal message, and the
corrected production ranging read covers the full 4.4-hour lookback through
exactly-inclusive 1,000-block pages - every page boundary, with no duplicate
or missing records.
"""

import json
import math
from collections import Counter
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

import httpx
import pytest
from test_history import (
    FIXTURE_BLOCK_SECONDS,
    fixture_block_timestamp,
    swap_log,
)

from aero_bot.history import (
    MAX_HEADER_BATCH_SIZE,
    RANGING_READ_LOOKBACK,
    EventHistoryRpcBackend,
    HistoryUnavailableError,
    PoolPricePath,
)
from aero_bot.ranging_reads import (
    NATIVE_USDC_ADDRESS,
    RANGING_READ_LOG_WINDOW_BLOCKS,
    build_ranging_evidence,
)
from aero_bot.venues import PoolCandidate, PoolKind

# The public Tenderly gateway's measured eth_getLogs span cap: spans at or
# below 1,000 blocks succeed and every wider span is rejected with the
# lowercase -32603 invalid-params wording the diagnostic replayed verbatim.
PUBLIC_GATEWAY_LOG_SPAN_CAP_BLOCKS = 1_000
# The pre-fix window every first ranging request sent above the cap.
PRE_FIX_RANGING_LOG_WINDOW_BLOCKS = 2_000
# A production-shaped fixture chain: two-second blocks with a head at block
# 8,000 (about 4.44 hours of history), so the locked 4.4-hour ranging
# lookback spans 7921 blocks and needs eight capped pages.
GATEWAY_LATEST_BLOCK = 8_000
# The lookback target lands at T0+160s, so the reconstruction opens at block
# 80 and pages 80..1079, 1080..2079, ..., 7080..8000 at the capped window.
RANGING_WINDOW_START_BLOCK = 80
GATEWAY_POOL_ADDRESS = "0x1111111111111111111111111111111111111111"
GATEWAY_STOCK_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"


def no_sleep(_seconds: float) -> None:
    """Discard retry and politeness delays so tests run instantly."""
    return None


def gateway_candidate() -> PoolCandidate:
    """Build one verified Slipstream pool shaped like a B20/USDC candidate.

    Returns:
        The fixture pool the ranging read reconstructs Swap logs for.
    """
    return PoolCandidate(
        pool_address=GATEWAY_POOL_ADDRESS,
        factory_address="0xf8f2eb4940cfe7d13603dddd87f123820fc061ef",
        token0_address=GATEWAY_STOCK_ADDRESS,
        token1_address=NATIVE_USDC_ADDRESS,
        pool_kind=PoolKind.SLIPSTREAM,
        tick_spacing=10,
        current_tick=-5,
        sqrt_ratio=1 << 96,
        pool_fee_ppm=500,
        unstaked_fee_ppm=100_000,
        reserve0=10**18,
        reserve1=5_000_000,
        staked0=10**17,
        staked1=1_000_000,
        gauge_address="0x2222222222222222222222222222222222222222",
        gauge_liquidity=9_999,
        gauge_alive=True,
        emissions_per_second=4_494_371_922_759_724,
        emissions_token_address=None,
    )


class PublicGatewaySpanCapTransport(httpx.MockTransport):
    """Serve fixture evidence while enforcing the gateway's span cap.

    Every non-getLogs method answers like a healthy public endpoint. An
    eth_getLogs whose inclusive block span exceeds the cap answers with the
    observed rejection - HTTP 200 carrying the JSON-RPC error object
    ``-32602: invalid params`` - which the backend must treat as a
    non-retriable fail-closed refusal, exactly as the production path did.
    """

    def __init__(
        self,
        logs: list[dict[str, Any]],
        span_cap_blocks: int = PUBLIC_GATEWAY_LOG_SPAN_CAP_BLOCKS,
        latest_block: int = GATEWAY_LATEST_BLOCK,
    ) -> None:
        """Configure the gateway fixture with its logs and span cap.

        Args:
            logs: Raw Swap log entries served inside any accepted window.
            span_cap_blocks: Maximum accepted inclusive eth_getLogs span.
            latest_block: Chain head block number served by eth_blockNumber.
        """
        self.getlogs_queries: list[tuple[int, int]] = []
        self._logs = logs
        self._span_cap_blocks = span_cap_blocks
        self._latest_block = latest_block
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one JSON-RPC request against the capped gateway fixture."""
        payload: dict[str, Any] = json.loads(request.content.decode("utf-8"))
        method = payload["method"]
        params = payload["params"]
        if method == "eth_blockNumber":
            return self._result(hex(self._latest_block))
        if method == "eth_getBlockByNumber":
            return self._result(self._block_header(params[0]))
        assert method == "eth_getLogs"
        query = params[0]
        from_block = int(query["fromBlock"], 16)
        to_block = int(query["toBlock"], 16)
        self.getlogs_queries.append((from_block, to_block))
        if to_block - from_block + 1 > self._span_cap_blocks:
            # The verbatim observed rejection: a JSON-RPC error body served
            # over HTTP 200 with the lowercase invalid-params wording.
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32602, "message": "invalid params"},
                },
            )
        served = [
            log for log in self._logs if from_block <= int(log["blockNumber"], 16) <= to_block
        ]
        return self._result(served)

    def _block_header(self, block_hex: str) -> dict[str, str]:
        """Serve one block header under the fixture cadence."""
        block_number = int(block_hex, 16)
        cadence_block = min(max(block_number, 0), self._latest_block)
        return {
            "number": hex(block_number),
            "timestamp": hex(int(fixture_block_timestamp(cadence_block).timestamp())),
        }

    def _result(self, result: object) -> httpx.Response:
        """Wrap one value as a successful JSON-RPC response."""
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def boundary_span_logs() -> list[dict[str, Any]]:
    """Place swaps at every page boundary of the eight-page window.

    The production window pages 80..1079, 1080..2079, ..., 7080..8000, so
    the fixture places one swap in the last block of every page and the
    first block of the next, plus two in the window's opening block and one
    at the final inclusive end block - the exact blocks where off-by-one,
    overlap, or truncation bugs would drop or duplicate records.

    Returns:
        Raw Swap logs at every reconstruction page boundary.
    """
    logs: list[dict[str, Any]] = []
    # A swap predating the window start must never be served or collected.
    logs.append(
        swap_log(RANGING_WINDOW_START_BLOCK - 1, 0, 1 << 96, pool_address=GATEWAY_POOL_ADDRESS)
    )
    # Two swaps in the opening block exercise same-block log-index ordering.
    logs.append(
        swap_log(RANGING_WINDOW_START_BLOCK, 1, (1 << 96) + 5, pool_address=GATEWAY_POOL_ADDRESS)
    )
    logs.append(swap_log(RANGING_WINDOW_START_BLOCK, 0, 1 << 96, pool_address=GATEWAY_POOL_ADDRESS))
    ratio = 1 << 96
    for page_start in range(
        RANGING_WINDOW_START_BLOCK + PUBLIC_GATEWAY_LOG_SPAN_CAP_BLOCKS,
        GATEWAY_LATEST_BLOCK + 1,
        PUBLIC_GATEWAY_LOG_SPAN_CAP_BLOCKS,
    ):
        boundary_last = page_start - 1
        boundary_first = page_start
        ratio += 7
        logs.append(swap_log(boundary_last, 0, ratio, pool_address=GATEWAY_POOL_ADDRESS))
        logs.append(swap_log(boundary_first, 2, ratio + 3, pool_address=GATEWAY_POOL_ADDRESS))
    # One swap in the final inclusive end block of the last page.
    logs.append(swap_log(GATEWAY_LATEST_BLOCK, 3, ratio + 11, pool_address=GATEWAY_POOL_ADDRESS))
    return logs


def production_ranging_backend(
    transport: PublicGatewaySpanCapTransport,
) -> EventHistoryRpcBackend:
    """Build the backend exactly as the policy engine's ranging read builds it.

    Args:
        transport: The capped public-gateway fixture endpoint.

    Returns:
        The production-configured offline backend.
    """
    return EventHistoryRpcBackend(
        transport=transport,
        sleep=no_sleep,
        page_delay_seconds=0.0,
        log_window_blocks=RANGING_READ_LOG_WINDOW_BLOCKS,
    )


def test_ranging_log_window_sits_at_the_public_gateway_span_cap() -> None:
    """The locked ranging window never exceeds the gateway's measured cap."""
    assert RANGING_READ_LOG_WINDOW_BLOCKS == PUBLIC_GATEWAY_LOG_SPAN_CAP_BLOCKS
    assert RANGING_READ_LOG_WINDOW_BLOCKS < PRE_FIX_RANGING_LOG_WINDOW_BLOCKS


def test_pre_fix_window_fails_closed_with_the_production_journal_message() -> None:
    """A window above the cap reproduces the exact production RPC refusal.

    The pre-fix 2,000-block window - the value the failed preflight of
    52d7846 deployed - sends its first eth_getLogs above the gateway cap and
    must fail closed with the verbatim journal message rather than retry,
    substitute, or return partial evidence.
    """
    transport = PublicGatewaySpanCapTransport(boundary_span_logs())
    backend = EventHistoryRpcBackend(
        transport=transport,
        sleep=no_sleep,
        page_delay_seconds=0.0,
        log_window_blocks=PRE_FIX_RANGING_LOG_WINDOW_BLOCKS,
    )

    with pytest.raises(HistoryUnavailableError, match=r"RPC error -32602: invalid params"):
        build_ranging_evidence(
            backend,
            gateway_candidate(),
            snapshot_price_usdc=Decimal("2"),
            stock_decimals=18,
        )


def test_ranging_read_covers_the_full_lookback_through_capped_pages() -> None:
    """The production read pages the full 4.4 hours at the gateway cap exactly.

    Every eth_getLogs span stays at or below the 1,000-block cap, full pages
    sit exactly at it, the pages tile the reconstruction window with no gap
    or overlap, and every boundary swap survives exactly once in mined
    order - complete evidence, not a truncated or duplicated read.
    """
    transport = PublicGatewaySpanCapTransport(boundary_span_logs())
    evidence = build_ranging_evidence(
        production_ranging_backend(transport),
        gateway_candidate(),
        snapshot_price_usdc=Decimal("2"),
        stock_decimals=18,
    )

    # The locked production lookback drove the reconstruction window.
    assert timedelta(hours=4.4) == RANGING_READ_LOOKBACK
    queries = transport.getlogs_queries
    assert queries, "the ranging read must page swap logs"
    # Every accepted span respects the gateway cap, and every full page sits
    # exactly at it - never wider, never unnecessarily narrower.
    spans = [to_block - from_block + 1 for from_block, to_block in queries]
    assert all(span <= PUBLIC_GATEWAY_LOG_SPAN_CAP_BLOCKS for span in spans)
    assert spans[:-1] == [PUBLIC_GATEWAY_LOG_SPAN_CAP_BLOCKS] * (len(spans) - 1)
    # Exact inclusive paging: consecutive, non-overlapping, gap-free windows
    # covering the reconstruction window from its first block to the head.
    assert queries[0][0] == RANGING_WINDOW_START_BLOCK
    assert queries[-1][1] == GATEWAY_LATEST_BLOCK
    for (_, previous_to), (next_from, _) in pairwise(queries):
        assert next_from == previous_to + 1

    # Every boundary swap is collected exactly once, ordered as mined, with
    # no pre-window record, no duplicate, and the final end block included.
    expected_blocks = [RANGING_WINDOW_START_BLOCK, RANGING_WINDOW_START_BLOCK]
    expected_indices = [0, 1]
    for page_start in range(
        RANGING_WINDOW_START_BLOCK + PUBLIC_GATEWAY_LOG_SPAN_CAP_BLOCKS,
        GATEWAY_LATEST_BLOCK + 1,
        PUBLIC_GATEWAY_LOG_SPAN_CAP_BLOCKS,
    ):
        expected_blocks.extend([page_start - 1, page_start])
        expected_indices.extend([0, 2])
    expected_blocks.append(GATEWAY_LATEST_BLOCK)
    expected_indices.append(3)
    collected = [(point.block_number, point.log_index) for point in evidence.trailing_path]
    assert collected == list(zip(expected_blocks, expected_indices, strict=True))
    assert len(set(collected)) == len(collected)
    # The fee window spans the whole reconstruction cadence, proving the
    # ordered points carry real spread-out timestamps, not one instant.
    expected_window_seconds = FIXTURE_BLOCK_SECONDS * (
        GATEWAY_LATEST_BLOCK - RANGING_WINDOW_START_BLOCK
    )
    assert evidence.fee_window_seconds == expected_window_seconds
    assert evidence.fee_window_notional_usd > 0
    assert evidence.realized_daily_volatility is not None
    assert evidence.realized_daily_volatility > 0


def test_live_strategy_sources_reports_per_pool_ranging_progress() -> None:
    """The live sources journal one completion line per pool read.

    LiveStrategySources passes its progress and timer into the lazily built
    ranging backend, so the per-pool line lands beside the backend's own
    phase lines: pool address, trailing point count, and elapsed seconds -
    all secret-free and all offline through the injected transport.
    """
    from test_history import FIXTURE_LATEST_BLOCK, FixtureRpcTransport

    from aero_bot.strategy import LiveStrategySources

    transport = FixtureRpcTransport(
        logs=[
            swap_log(996, 0, 1 << 96, pool_address=GATEWAY_POOL_ADDRESS),
            swap_log(999, 0, 1 << 97, pool_address=GATEWAY_POOL_ADDRESS),
        ],
        latest_block=FIXTURE_LATEST_BLOCK,
    )
    lines: list[str] = []
    clock = {"now": 0.0}

    def stepped_timer() -> float:
        clock["now"] += 5.0
        return clock["now"]

    sources = LiveStrategySources(
        rpc_url="https://fixture.example",
        sugar_address="0x2222222222222222222222222222222222222222",
        transport=transport,
        progress=lines.append,
        sleep=no_sleep,
        timer=stepped_timer,
    )

    evidence = sources.ranging_evidence(
        gateway_candidate(),
        snapshot_price_usdc=Decimal("2"),
        stock_decimals=18,
        observed_at=fixture_block_timestamp(GATEWAY_LATEST_BLOCK),
    )

    assert evidence is not None
    assert len(evidence.trailing_path) == 2
    # The backend's three phase lines, then the strategy's per-pool line.
    # The production 4.4-hour lookback predates the fixture genesis, so the
    # reconstruction opens at block 0 and pages 0..999 then 1000..1000 at the
    # capped 1,000-block window.
    assert lines == [
        f"pool {GATEWAY_POOL_ADDRESS}: timestamp search probed 10 block(s) in 5.0s",
        f"pool {GATEWAY_POOL_ADDRESS}: swap logs read 2 window(s) holding 2 event(s) in 5.0s",
        f"pool {GATEWAY_POOL_ADDRESS}: block headers read 2 unique block(s) through "
        f"1 request(s) at batch size 10 in 5.0s",
        f"ranging evidence for {GATEWAY_POOL_ADDRESS}: 2 point(s) in 35.0s",
    ]


def test_live_strategy_sources_failure_line_carries_elapsed_time() -> None:
    """A failed ranging read journals the elapsed time beside the reason."""
    from test_history import FixtureRpcTransport

    from aero_bot.strategy import LiveStrategySources

    transport = FixtureRpcTransport(failure_mode="fatal_rpc_error")
    lines: list[str] = []
    clock = {"now": 0.0}

    def stepped_timer() -> float:
        clock["now"] += 5.0
        return clock["now"]

    sources = LiveStrategySources(
        rpc_url="https://fixture.example",
        sugar_address="0x2222222222222222222222222222222222222222",
        transport=transport,
        progress=lines.append,
        sleep=no_sleep,
        timer=stepped_timer,
    )

    evidence = sources.ranging_evidence(
        gateway_candidate(),
        snapshot_price_usdc=Decimal("2"),
        stock_decimals=18,
        observed_at=fixture_block_timestamp(GATEWAY_LATEST_BLOCK),
    )

    assert evidence is None
    assert lines == [
        f"ranging evidence read failed for {GATEWAY_POOL_ADDRESS} after 5.0s: "
        "RPC error -32601: method not found; entries and voluntary recenters "
        "defer fail-closed"
    ]


# ---------------------------------------------------------------------------
# The operator's deterministic counting reproduction, pinned as a test.
# ---------------------------------------------------------------------------

# The production-shaped counting fixture the operator measured: two-second
# synthetic blocks under head 52,187,561 (the block run 2 verified at its
# +66s journal line) with one swap every 26 blocks for the active shape
# (AAPLc's observed cadence), driven through the real production constants.
PROOF_HEAD_BLOCK = 52_187_561
PROOF_BLOCK_SECONDS = 2
PROOF_GENESIS_TIMESTAMP = 1_700_000_000 - PROOF_HEAD_BLOCK * PROOF_BLOCK_SECONDS
PROOF_SWAP_EVERY_BLOCKS = 26
# The locked 4.4-hour lookback over two-second blocks spans 7,920 seconds of
# history, so the reconstruction window opens 7,920 blocks below the head and
# tiles seven full 1,000-block windows plus one 921-block tail window.
PROOF_RECONSTRUCTION_BLOCKS = RANGING_READ_LOOKBACK / timedelta(seconds=PROOF_BLOCK_SECONDS)
PROOF_FULL_WINDOW_COUNT = 7
PROOF_TAIL_WINDOW_SPAN = 921
PROOF_STOCK_ADDRESS = GATEWAY_STOCK_ADDRESS
# Native USDC on Base, the quote side of every B20 pool.
PROOF_QUOTE_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
# The unbatched baseline the operator's reproduction measured: one head read,
# one latest header plus one header per binary-search probe, and one
# eth_getLogs per window - 27 headers and 8 windows at this fixture shape.
PROOF_QUIET_REQUESTS = 36
PROOF_HEADER_READS_BEFORE_EVENTS = 27
PROOF_LOG_WINDOW_REQUESTS = 8
# The active shape's event-block header reads: 309 unique event blocks, two
# of which the timestamp search already cached, leave 307 single reads at the
# unbatched baseline - the amplification this repair removes.
PROOF_ACTIVE_POINTS = 309
PROOF_ACTIVE_EVENT_HEADER_READS = 307
PROOF_ACTIVE_REQUESTS = PROOF_QUIET_REQUESTS + PROOF_ACTIVE_EVENT_HEADER_READS


class CountingProductionShapeTransport(httpx.MockTransport):
    """Serve the operator's counting reproduction while counting HTTP requests.

    The handler answers every method like a healthy read-only endpoint over
    the synthetic two-second cadence and records, per HTTP round trip rather
    than per JSON-RPC entry, how many single and batched requests the real
    backend sends - the exact profile the operator measured against the
    1,800-second dry-cycle budget. An optional mode answers every batch
    request with the endpoint's own non-list rejection body, exactly like the
    documented ``-32014 maximum 10 calls in 1 batch`` refusal.
    """

    def __init__(self, *, active: bool, reject_batches: bool = False) -> None:
        """Configure the counting fixture.

        Args:
            active: Serve one swap every 26 blocks inside every log window,
                the active pool's observed cadence; False serves no logs.
            reject_batches: Answer every JSON-RPC batch request with the
                endpoint's own non-list batch-rejection body.
        """
        self.http_requests: Counter[str] = Counter()
        self.method_entries: Counter[str] = Counter()
        self.window_spans: Counter[int] = Counter()
        self.served_header_blocks: set[int] = set()
        self.event_blocks: set[int] = set()
        self.rejected_batches: list[list[dict[str, Any]]] = []
        self._active = active
        self._reject_batches = reject_batches
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one HTTP round trip, counting it by batched-ness."""
        payload: object = json.loads(request.content.decode("utf-8"))
        entries = payload if isinstance(payload, list) else [payload]
        assert isinstance(entries, list)
        is_batch = isinstance(payload, list)
        self.http_requests["batch" if is_batch else "single"] += 1
        if is_batch and self._reject_batches:
            self.rejected_batches.append(entries)
            # The endpoint's own batch rejection: a non-list body served over
            # HTTP 200, exactly like the documented maximum-batch error.
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32014, "message": "maximum 10 calls in 1 batch"},
                },
            )
        responses = [self._serve_entry(entry) for entry in entries]
        if is_batch:
            return httpx.Response(200, json=responses)
        return httpx.Response(200, json=responses[0])

    def _serve_entry(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Serve one JSON-RPC entry against the synthetic production shape."""
        method = entry["method"]
        params = entry.get("params", [])
        self.method_entries[method] += 1
        if method == "eth_blockNumber":
            result: object = hex(PROOF_HEAD_BLOCK)
        elif method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            self.served_header_blocks.add(number)
            result = {
                "number": hex(number),
                "timestamp": hex(PROOF_GENESIS_TIMESTAMP + number * PROOF_BLOCK_SECONDS),
            }
        elif method == "eth_getLogs":
            from_block = int(params[0]["fromBlock"], 16)
            to_block = int(params[0]["toBlock"], 16)
            self.window_spans[to_block - from_block + 1] += 1
            logs: list[dict[str, Any]] = []
            if self._active:
                for log_index, block in enumerate(
                    range(from_block, to_block + 1, PROOF_SWAP_EVERY_BLOCKS)
                ):
                    self.event_blocks.add(block)
                    logs.append(
                        swap_log(block, log_index, 1 << 96, pool_address=GATEWAY_POOL_ADDRESS)
                    )
            result = logs
        else:
            raise AssertionError(f"unexpected RPC method {method}")
        return {"jsonrpc": "2.0", "id": entry.get("id"), "result": result}


def proof_backend(
    transport: CountingProductionShapeTransport,
    *,
    header_batch_size: int = 1,
    progress: Callable[[str], None] | None = None,
) -> EventHistoryRpcBackend:
    """Build the ranging backend with the real production read constants.

    Args:
        transport: The counting fixture endpoint.
        header_batch_size: Header-read batching to exercise; one is the
            unbatched wire shape the operator's baseline measured.
        progress: Optional progress collector receiving the backend's lines.

    Returns:
        The offline backend configured exactly like the production ranging
        read: the locked log window and the 4.4-hour lookback.
    """
    return EventHistoryRpcBackend(
        log_window_blocks=RANGING_READ_LOG_WINDOW_BLOCKS,
        transport=transport,
        sleep=no_sleep,
        page_delay_seconds=0.0,
        header_batch_size=header_batch_size,
        progress=progress,
    )


def fetch_proof_path(backend: EventHistoryRpcBackend) -> PoolPricePath:
    """Reconstruct the proof pool's path over the production lookback.

    Args:
        backend: Offline backend serving the counting fixture.

    Returns:
        The reconstructed path over the locked 4.4-hour lookback.
    """
    return backend.fetch_price_path(
        pool_address=GATEWAY_POOL_ADDRESS,
        token_address=PROOF_STOCK_ADDRESS,
        token0_address=PROOF_STOCK_ADDRESS,
        token1_address=PROOF_QUOTE_ADDRESS,
        token_decimals=18,
        quote_decimals=6,
        lookback=RANGING_READ_LOOKBACK,
    )


def path_fingerprint(path: PoolPricePath) -> dict[str, Any]:
    """Strip the per-call observation instant so two runs compare equal.

    Args:
        path: One reconstructed path.

    Returns:
        Every field of the path except ``observed_at``, which carries the
        wall clock of the individual run and cannot match across calls.
    """
    fingerprint = path.model_dump()
    fingerprint.pop("observed_at")
    return fingerprint


def test_request_count_proof_pins_the_unbatched_production_baseline() -> None:
    """The unbatched wire shape sends exactly the operator's measured counts.

    At ``header_batch_size=1`` - the production default the strategy backend
    historically left untouched - a quiet pool costs exactly 36 HTTP requests
    (one eth_blockNumber, 27 eth_getBlockByNumber, eight eth_getLogs) and the
    active shape costs exactly 343 (the same 36 plus 307 one-per-event-block
    header reads), while the log windows tile the 4.4-hour lookback as seven
    1,000-block pages plus one 921-block tail. This is the amplification the
    two timed-out preflight dry cycles paid for.
    """
    quiet = CountingProductionShapeTransport(active=False)
    quiet_path = fetch_proof_path(proof_backend(quiet))

    assert quiet_path.points == ()
    assert dict(quiet.http_requests) == {"single": PROOF_QUIET_REQUESTS}
    assert dict(quiet.method_entries) == {
        "eth_blockNumber": 1,
        "eth_getBlockByNumber": PROOF_HEADER_READS_BEFORE_EVENTS,
        "eth_getLogs": PROOF_LOG_WINDOW_REQUESTS,
    }
    assert dict(quiet.window_spans) == {
        RANGING_READ_LOG_WINDOW_BLOCKS: PROOF_FULL_WINDOW_COUNT,
        PROOF_TAIL_WINDOW_SPAN: 1,
    }
    assert sum(quiet.window_spans.values()) == PROOF_LOG_WINDOW_REQUESTS

    active = CountingProductionShapeTransport(active=True)
    active_path = fetch_proof_path(proof_backend(active))

    assert len(active_path.points) == PROOF_ACTIVE_POINTS
    assert dict(active.http_requests) == {"single": PROOF_ACTIVE_REQUESTS}
    # The extra reads over the quiet baseline are exactly the event blocks'
    # headers: 307 single reads for 309 unique event blocks, two of which the
    # timestamp search already cached.
    assert (
        active.method_entries["eth_getBlockByNumber"] - quiet.method_entries["eth_getBlockByNumber"]
        == PROOF_ACTIVE_EVENT_HEADER_READS
    )
    assert dict(active.window_spans) == dict(quiet.window_spans)


def test_request_count_proof_batches_headers_with_identical_results() -> None:
    """Batched header reads drop the active pool to 67 identical-result requests.

    At the public-endpoint-verified ``MAX_HEADER_BATCH_SIZE`` the quiet pool
    is unchanged at 36 requests (a quiet pool has no event blocks to batch),
    and the active pool collapses its 307 single header reads into
    ceil(307/10) = 31 batched requests for exactly 67 HTTP round trips - while
    returning an identical ``PoolPricePath``: same points in the same order
    with the same timestamps, prices, and window bounds.
    """
    quiet = CountingProductionShapeTransport(active=False)
    quiet_path = fetch_proof_path(proof_backend(quiet, header_batch_size=MAX_HEADER_BATCH_SIZE))

    assert quiet_path.points == ()
    assert dict(quiet.http_requests) == {"single": PROOF_QUIET_REQUESTS}

    unbatched = CountingProductionShapeTransport(active=True)
    unbatched_path = fetch_proof_path(proof_backend(unbatched))
    batched = CountingProductionShapeTransport(active=True)
    batched_path = fetch_proof_path(proof_backend(batched, header_batch_size=MAX_HEADER_BATCH_SIZE))

    batched_header_requests = math.ceil(PROOF_ACTIVE_EVENT_HEADER_READS / MAX_HEADER_BATCH_SIZE)
    assert dict(batched.http_requests) == {
        "single": PROOF_QUIET_REQUESTS,
        "batch": batched_header_requests,
    }
    assert sum(batched.http_requests.values()) == (PROOF_QUIET_REQUESTS + batched_header_requests)
    # Identical history with meaningfully fewer round trips, never less data.
    assert path_fingerprint(batched_path) == path_fingerprint(unbatched_path)
    assert [point.timestamp for point in batched_path.points] == [
        point.timestamp for point in unbatched_path.points
    ]
    assert (batched_path.from_block, batched_path.to_block) == (
        unbatched_path.from_block,
        unbatched_path.to_block,
    )
    # The fixture served the same event headers either way.
    assert batched.event_blocks == unbatched.event_blocks


def test_request_count_proof_bounds_rejection_to_one_wasted_request() -> None:
    """A rejecting endpoint costs exactly one wasted request, full coverage.

    When the endpoint answers every batch request with its own non-list
    rejection body, the backend downgrades once, re-reads every still-missing
    header through the single-header path, and still returns coverage
    identical to the unbatched run - at the cost of exactly one rejected
    batch attempt over the whole read, the pinned wasted-request bound.
    """
    unbatched = CountingProductionShapeTransport(active=True)
    unbatched_path = fetch_proof_path(proof_backend(unbatched))
    lines: list[str] = []
    rejected = CountingProductionShapeTransport(active=True, reject_batches=True)
    rejected_path = fetch_proof_path(
        proof_backend(rejected, header_batch_size=MAX_HEADER_BATCH_SIZE, progress=lines.append)
    )

    # Identical results: every point, timestamp, and bound survives.
    assert path_fingerprint(rejected_path) == path_fingerprint(unbatched_path)
    # Exactly one rejected batch attempt, then only single reads: the total
    # cost is the unbatched 343 plus the one wasted request.
    assert len(rejected.rejected_batches) == 1
    assert dict(rejected.http_requests) == {
        "single": PROOF_ACTIVE_REQUESTS,
        "batch": 1,
    }
    # Exactly one downgrade line naming the permanent downgrade.
    downgrade_lines = [line for line in lines if "downgrading this backend" in line]
    assert len(downgrade_lines) == 1
    # Every event block's header was still read through the single path.
    assert rejected.event_blocks <= rejected.served_header_blocks
