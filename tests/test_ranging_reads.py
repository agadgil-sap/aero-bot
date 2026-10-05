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
    RANGING_READ_LOOKBACK,
    EventHistoryRpcBackend,
    HistoryUnavailableError,
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
