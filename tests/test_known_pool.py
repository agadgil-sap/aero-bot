"""Pin the decision surfaces' known-pool fast path and its live-verified math.

The regression these tests pin: the decide path (``aero-bot-decide`` and the
cycle's decide phase, dry runs included) used to run the full LP Sugar
enumeration on every single run - tens of thousands of pools against a
rate-limited public RPC - which presented to the operator as a multi-minute
silent stall. With a persisted pool pin the decision now resolves through a
handful of block-pinned views and never enumerates at all.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import pytest

from aero_bot.executor import ExecutorRpcBackend
from aero_bot.known_pool import (
    KNOWN_POOL_SOURCE,
    grid_cell_lower_tick,
    persist_decision_pool_pin,
    resolve_known_pool_candidate,
    staked_sides_for_gauge_liquidity,
)
from aero_bot.lp_pins import LpPoolPin, LpPoolPinStore
from aero_bot.registry import (
    B20AssetListing,
    B20RegistryResult,
    RegistryStatus,
)
from aero_bot.venues import (
    AERO_TOKEN_ADDRESS,
    BASE_USDC_ADDRESS,
    SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
    PoolCandidate,
    PoolDiscoveryResult,
    PoolDiscoveryStatus,
    VenueId,
    aerodrome_contract_evidence,
)

if TYPE_CHECKING:
    from aero_bot.strategy import LiveStrategySources

# Fixture identities mirror the LP executor's fixtures so both fast paths
# describe the same scripted pool.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
POOL_ADDRESS = "0x1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"
GAUGE_ADDRESS = "0x3111111111111111111111111111111111111111"
NFPM_ADDRESS = "0xe1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"
GAUGE_FACTORY_ADDRESS = "0x385293cae378c813f16f0c1334d774adddf56abb"
VOTER_ADDRESS = "0x16613524e02ad97edfef371bc883f2f5d6c480a5"
BASE_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
LP_TICK_SPACING = 10
LP_CURRENT_TICK = -11557
LP_STAKED_LIQUIDITY = 131_515_852_145_883
LP_ACTIVE_LIQUIDITY = 10**20
LP_SQRT_RATIO = 44_458_286_281_315_958_222_363_810_782
LP_REWARD_RATE = 4_494_371_922_759_724
LP_SWAP_FEE_PPM = 500
LP_UNSTAKED_FEE_PPM = 100_000
LP_RESERVE0 = 1_000_000_000_000
LP_RESERVE1 = 250_000_000_000
FAST_BLOCK_NUMBER = 51_000_000

# The live AAPLc capture (2026-09-09): the Sugar's own staked sides for the
# grid cell at tick -11557, spacing 10, reproduced by the derivation exactly.
LIVE_STAKED0 = 117_114_415_165
LIVE_STAKED1 = 0


def make_pin(**overrides: object) -> LpPoolPin:
    """Build one fixture pool pin matching the scripted pool identity.

    Args:
        **overrides: Pin fields changed for one behavior test.

    Returns:
        A validated immutable pin over the fixture pool's exact identity.
    """
    values: dict[str, object] = {
        "symbol": "FIXc",
        "pool_address": POOL_ADDRESS,
        "factory_address": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
        "token0_address": BASE_USDC_ADDRESS,
        "token1_address": B20_ADDRESS,
        "tick_spacing": LP_TICK_SPACING,
        "gauge_address": GAUGE_ADDRESS,
        "nfpm_address": NFPM_ADDRESS,
        "stock_decimals": 8,
        "pinned_at": BASE_NOW,
        "pinned_block": 100,
        "discovery_source": "lp-sugar:fixture@block:100",
    }
    values.update(overrides)
    return LpPoolPin.model_validate(values)


def make_listing() -> B20AssetListing:
    """Build one fixture registry listing for the FIXc stock."""
    return B20AssetListing.model_validate(
        {
            "symbol": "FIXc",
            "name": "Fixture",
            "address": B20_ADDRESS,
            "explorer_url": "https://base.token/token/fixture",
        }
    )


def make_candidate(**overrides: object) -> PoolCandidate:
    """Build one fixture sweep candidate for the FIXc pool.

    Args:
        **overrides: Candidate fields changed for one behavior test.

    Returns:
        A validated immutable candidate over the fixture pool.
    """
    values: dict[str, object] = {
        "pool_address": POOL_ADDRESS,
        "factory_address": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
        "token0_address": BASE_USDC_ADDRESS,
        "token1_address": B20_ADDRESS,
        "pool_kind": "slipstream",
        "tick_spacing": LP_TICK_SPACING,
        "current_tick": LP_CURRENT_TICK,
        "sqrt_ratio": LP_SQRT_RATIO,
        "pool_fee_ppm": LP_SWAP_FEE_PPM,
        "unstaked_fee_ppm": LP_UNSTAKED_FEE_PPM,
        "reserve0": LP_RESERVE0,
        "reserve1": LP_RESERVE1,
        "staked0": LIVE_STAKED0,
        "staked1": LIVE_STAKED1,
        "gauge_address": GAUGE_ADDRESS,
        "gauge_liquidity": LP_STAKED_LIQUIDITY,
        "gauge_alive": True,
        "emissions_per_second": LP_REWARD_RATE,
        "emissions_token_address": AERO_TOKEN_ADDRESS,
        "pool_active_liquidity": LP_ACTIVE_LIQUIDITY,
        "nfpm_address": NFPM_ADDRESS,
    }
    values.update(overrides)
    return PoolCandidate.model_validate(values)


def word_hex(value: int) -> str:
    """Encode one unsigned 32-byte ABI word as a 0x-prefixed hex string."""
    return "0x" + format(value, "064x")


def address_word(address: str) -> str:
    """Encode one address-valued ABI word."""
    return word_hex(int(address, 16))


def signed_word(value: int) -> str:
    """Encode one signed 32-byte ABI word."""
    return "0x" + format(value & ((1 << 256) - 1), "064x")


# One ABI-encoded empty dynamic array: outer offset 0x20 then length zero.
EMPTY_PAGE_RESULT = "0x" + "00" * 31 + "20" + "00" * 32


class KnownPoolRpcScript:
    """Serve the fast path's block-pinned reads and count Sugar enumeration."""

    def __init__(
        self,
        *,
        token0: str = BASE_USDC_ADDRESS,
        token1: str = B20_ADDRESS,
        tick_spacing: int = LP_TICK_SPACING,
        gauge: str = GAUGE_ADDRESS,
        factory: str = SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
        nfpm: str = NFPM_ADDRESS,
        is_pool: bool = True,
        gauge_alive: bool = True,
        reward_token: str = AERO_TOKEN_ADDRESS,
        reward_rate: int = LP_REWARD_RATE,
    ) -> None:
        """Configure every scripted answer the fast path's reads receive."""
        self.enumeration_calls = 0
        self.calls: list[tuple[str, str]] = []
        self._token0 = token0
        self._token1 = token1
        self._tick_spacing = tick_spacing
        self._gauge = gauge
        self._factory = factory
        self._nfpm = nfpm
        self._is_pool = is_pool
        self._gauge_alive = gauge_alive
        self._reward_token = reward_token
        self._reward_rate = reward_rate

    def transport(self) -> httpx.MockTransport:
        """Build the deterministic HTTP transport over this script."""
        script = self

        def handle(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            if body["method"] == "eth_blockNumber":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "result": hex(FAST_BLOCK_NUMBER)}
                )
            if body["method"] != "eth_call":
                raise AssertionError(f"unexpected method {body['method']}")
            call = body["params"][0]
            to_address = call["to"].lower()
            data = call["data"]
            script.calls.append((to_address, data[:10]))
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": script._eth_call(to_address, data)},
            )

        return httpx.MockTransport(handle)

    def _eth_call(self, to_address: str, data: str) -> str:
        """Answer one fast-path view read."""
        if data.startswith("0xb10daf7b"):
            self.enumeration_calls += 1
            raise AssertionError("the fast path must never enumerate the Sugar")
        if to_address == POOL_ADDRESS:
            if data.startswith("0x0dfe1681"):
                return address_word(self._token0)
            if data.startswith("0xd21220a7"):
                return address_word(self._token1)
            if data.startswith("0xd0c93a7c"):
                return word_hex(self._tick_spacing)
            if data.startswith("0xa6f19c84"):
                return address_word(self._gauge)
            if data.startswith("0xc45a0155"):
                return address_word(self._factory)
            if data.startswith("0x3850c7bd"):
                return (
                    word_hex(LP_SQRT_RATIO)[2:]
                    and "0x" + word_hex(LP_SQRT_RATIO)[2:] + signed_word(LP_CURRENT_TICK)[2:]
                )
            if data.startswith("0x1a686502"):
                return word_hex(LP_ACTIVE_LIQUIDITY)
            if data.startswith("0x3ab04b20"):
                return word_hex(LP_STAKED_LIQUIDITY)
        if to_address == BASE_USDC_ADDRESS.lower() and data.startswith("0x70a08231"):
            return word_hex(LP_RESERVE0)
        if to_address == B20_ADDRESS and data.startswith("0x70a08231"):
            return word_hex(LP_RESERVE1)
        if to_address == GAUGE_ADDRESS:
            if data.startswith("0x0d52333c"):
                return address_word(GAUGE_FACTORY_ADDRESS)
            if data.startswith("0xf7c618c1"):
                return address_word(self._reward_token)
            if data.startswith("0x7b0a47ee"):
                return word_hex(self._reward_rate)
        if to_address == GAUGE_FACTORY_ADDRESS and data.startswith("0x47ccca02"):
            return address_word(self._nfpm)
        if to_address == SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS.lower():
            if data.startswith("0x46c96aac"):
                return address_word(VOTER_ADDRESS)
            if data.startswith("0x5b16ebb7"):
                return word_hex(1 if self._is_pool else 0)
            if data.startswith("0x35458dcc"):
                return word_hex(LP_SWAP_FEE_PPM)
            if data.startswith("0x48cf7a43"):
                return word_hex(LP_UNSTAKED_FEE_PPM)
        if to_address == VOTER_ADDRESS and data.startswith("0x1703e5f9"):
            return word_hex(1 if self._gauge_alive else 0)
        raise AssertionError(f"unexpected eth_call to {to_address} data {data[:10]}")


def make_registry() -> B20RegistryResult:
    """Build one verified fixture registry containing only the FIXc listing."""
    return B20RegistryResult(
        status=RegistryStatus.VERIFIED,
        source_url="https://base.token/fixture",
        source_observed_at=BASE_NOW.date(),
        assets=(make_listing(),),
        diagnostic="fixture registry",
    )


def make_rpc(script: KnownPoolRpcScript) -> ExecutorRpcBackend:
    """Build one ExecutorRpcBackend over the scripted transport."""
    return ExecutorRpcBackend(
        rpc_url="https://example.invalid",
        transport=script.transport(),
        sleep=(lambda _seconds: None),
        timer=(lambda: 0.0),
    )


class TestGridCellConvention:
    """The Sugar's grid-cell truncation toward zero."""

    def test_negative_tick_truncates_toward_zero(self) -> None:
        """A negative unaligned tick selects the cell above the price."""
        assert grid_cell_lower_tick(-11557, 10) == -11550
        assert grid_cell_lower_tick(-11550, 10) == -11550
        assert grid_cell_lower_tick(-11549, 10) == -11540

    def test_positive_tick_truncates_toward_zero(self) -> None:
        """A positive unaligned tick selects the cell below the price."""
        assert grid_cell_lower_tick(11557, 10) == 11550
        assert grid_cell_lower_tick(0, 10) == 0

    def test_non_positive_spacing_refuses(self) -> None:
        """The spacing must be positive."""
        with pytest.raises(ValueError, match="tick_spacing must be positive"):
            grid_cell_lower_tick(-11557, 0)


class TestStakedSidesDerivation:
    """The derived staked sides reproduce the Sugar's own values exactly."""

    def test_live_aaplc_capture_below_the_cell(self) -> None:
        """The 2026-09-09 live record's sides reproduce integer-exactly."""
        sides = staked_sides_for_gauge_liquidity(
            LP_SQRT_RATIO, LP_CURRENT_TICK, LP_TICK_SPACING, LP_STAKED_LIQUIDITY
        )
        assert sides == (LIVE_STAKED0, LIVE_STAKED1)

    def test_price_inside_the_cell_gives_two_sides(self) -> None:
        """An in-cell price derives both positive staked sides."""
        from aero_bot.lp_plan import _sqrt_price_at_tick

        # Tick 11557 truncates onto the cell [11550, 11560); a price at the
        # cell's midpoint sits strictly inside, so both sides are positive.
        inside_sqrt = int(_sqrt_price_at_tick(11555))
        sides = staked_sides_for_gauge_liquidity(inside_sqrt, 11557, 10, LP_STAKED_LIQUIDITY)
        assert sides[0] > 0
        assert sides[1] > 0

    def test_negative_liquidity_refuses(self) -> None:
        """A negative gauge liquidity refuses."""
        with pytest.raises(ValueError, match="gauge_liquidity"):
            staked_sides_for_gauge_liquidity(LP_SQRT_RATIO, -11557, 10, -1)


class TestResolveKnownPoolCandidate:
    """The fast path verifies identity and builds the decision candidate."""

    def _resolve(self, script: KnownPoolRpcScript) -> tuple[PoolCandidate, int]:
        rpc = make_rpc(script)
        return resolve_known_pool_candidate(
            rpc, make_pin(), make_listing(), aerodrome_contract_evidence()
        )

    def test_happy_path_builds_the_complete_candidate(self) -> None:
        """Every candidate field comes from the block-pinned live reads."""
        script = KnownPoolRpcScript()
        candidate, block = self._resolve(script)
        assert block == FAST_BLOCK_NUMBER
        assert candidate.pool_address == POOL_ADDRESS
        assert candidate.factory_address == SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS.lower()
        assert candidate.token0_address == BASE_USDC_ADDRESS.lower()
        assert candidate.token1_address == B20_ADDRESS
        assert candidate.tick_spacing == LP_TICK_SPACING
        assert candidate.current_tick == LP_CURRENT_TICK
        assert candidate.sqrt_ratio == LP_SQRT_RATIO
        assert candidate.pool_fee_ppm == LP_SWAP_FEE_PPM
        assert candidate.unstaked_fee_ppm == LP_UNSTAKED_FEE_PPM
        assert candidate.reserve0 == LP_RESERVE0
        assert candidate.reserve1 == LP_RESERVE1
        assert candidate.staked0 == LIVE_STAKED0
        assert candidate.staked1 == LIVE_STAKED1
        assert candidate.gauge_address == GAUGE_ADDRESS
        assert candidate.gauge_liquidity == LP_STAKED_LIQUIDITY
        assert candidate.gauge_alive is True
        assert candidate.emissions_per_second == LP_REWARD_RATE
        assert candidate.emissions_token_address == AERO_TOKEN_ADDRESS.lower()
        assert candidate.pool_active_liquidity == LP_ACTIVE_LIQUIDITY
        assert candidate.nfpm_address == NFPM_ADDRESS
        # The fast path never enumerates the Sugar.
        assert script.enumeration_calls == 0

    def test_identity_mismatch_refuses(self) -> None:
        """A live identity that diverges from the pin refuses."""
        script = KnownPoolRpcScript(token1="0x" + "cc" * 20)
        with pytest.raises(ValueError, match="no longer matches the live pool"):
            self._resolve(script)

    def test_non_usdc_pair_refuses(self) -> None:
        """A pool whose pair is not the official B20/USDC pair refuses."""
        stranger = "0x" + "bb" * 20
        script = KnownPoolRpcScript(token0=B20_ADDRESS, token1=stranger)
        pin = make_pin(token0_address=B20_ADDRESS, token1_address=stranger)
        rpc = make_rpc(script)
        with pytest.raises(ValueError, match="not the official B20/native-USDC pair"):
            resolve_known_pool_candidate(rpc, pin, make_listing(), aerodrome_contract_evidence())

    def test_non_allowlisted_factory_refuses(self) -> None:
        """A pool reporting a factory outside the allowlist refuses."""
        drifted = "0x" + "99" * 20
        script = KnownPoolRpcScript(factory=drifted)
        pin = make_pin(factory_address=drifted)
        rpc = make_rpc(script)
        with pytest.raises(ValueError, match="outside the official Slipstream allowlist"):
            resolve_known_pool_candidate(rpc, pin, make_listing(), aerodrome_contract_evidence())

    def test_factory_membership_refuses(self) -> None:
        """A factory that does not claim the pool as its own refuses."""
        script = KnownPoolRpcScript(is_pool=False)
        with pytest.raises(ValueError, match="does not claim the pool"):
            self._resolve(script)

    def test_dead_gauge_refuses(self) -> None:
        """A gauge the Voter reports killed refuses."""
        script = KnownPoolRpcScript(gauge_alive=False)
        with pytest.raises(ValueError, match="is not alive"):
            self._resolve(script)

    def test_non_aero_emissions_refuse(self) -> None:
        """A gauge emitting a token other than official AERO refuses."""
        script = KnownPoolRpcScript(reward_token="0x" + "dd" * 20)
        with pytest.raises(ValueError, match="not emitting official AERO"):
            self._resolve(script)

    def test_zero_emission_rate_refuses(self) -> None:
        """A gauge with a zero reward rate refuses like the venue gate."""
        script = KnownPoolRpcScript(reward_rate=0)
        with pytest.raises(ValueError, match="not emitting official AERO"):
            self._resolve(script)


class TestPersistDecisionPoolPin:
    """The sweep fallback persists its verified identity for the next run."""

    def test_sweep_persists_the_pin(self, tmp_path: Path) -> None:
        """A verified sweep candidate is written through the pin store."""
        store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
        persist_decision_pool_pin(
            store,
            make_listing(),
            make_candidate(),
            8,
            123,
            BASE_NOW,
            "lp-sugar:fixture@block:123",
        )
        pins = store.load()
        assert set(pins) == {"fixc"}
        assert pins["fixc"].pool_address == POOL_ADDRESS
        assert pins["fixc"].pinned_block == 123

    def test_gaugeless_candidate_skips_the_pin(self, tmp_path: Path) -> None:
        """A candidate without a gauge never writes a pin."""
        store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
        persist_decision_pool_pin(
            store,
            make_listing(),
            make_candidate(gauge_address=None),
            8,
            123,
            BASE_NOW,
            "lp-sugar:fixture@block:123",
        )
        assert store.load() == {}


class TestDecisionFastPathWiring:
    """The strategy sources resolve through the pin and never enumerate."""

    def _sources(
        self,
        tmp_path: Path,
        script: KnownPoolRpcScript,
        monkeypatch: pytest.MonkeyPatch,
        execution_sources: object | None = None,
    ) -> tuple["LiveStrategySources", LpPoolPinStore]:
        import aero_bot.executor as executor_module
        import aero_bot.strategy as strategy_module
        from aero_bot.strategy import LiveStrategySources

        monkeypatch.setattr(
            executor_module,
            "load_official_b20_registry",
            make_registry,
        )
        if execution_sources is not None:
            monkeypatch.setattr(
                strategy_module,
                "LiveExecutionSources",
                lambda **_: execution_sources,
            )
        store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
        store.save_pin(make_pin())
        sources = LiveStrategySources(
            rpc_url="https://example.invalid",
            sugar_address="0x27fc745390d1f4baf8d184fbd97748340f786634",
            transport=script.transport(),
            pool_pin_store=store,
            sleep=(lambda _seconds: None),
            timer=(lambda: 0.0),
        )
        return sources, store

    def test_pinned_symbol_resolves_without_enumeration(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """THE REGRESSION: a pinned decide path makes zero Sugar enumeration."""
        script = KnownPoolRpcScript()
        sources, _ = self._sources(tmp_path, script, monkeypatch)
        candidate, block = sources.resolve_pool("FIXc")
        assert script.enumeration_calls == 0
        assert block == FAST_BLOCK_NUMBER
        assert candidate.pool_address == POOL_ADDRESS
        assert candidate.staked0 == LIVE_STAKED0

    def test_identity_drift_falls_back_to_the_sweep(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A drifted pin falls back to the verified sweep and re-pins."""
        script = KnownPoolRpcScript(nfpm="0x" + "ee" * 20)

        sweep_candidate = make_candidate()
        sweep = PoolDiscoveryResult(
            venue=VenueId.AERODROME,
            status=PoolDiscoveryStatus.VERIFIED,
            source="lp-sugar:fixture@block:123",
            observed_at=BASE_NOW,
            snapshot_block=123,
            pools=(sweep_candidate,),
            diagnostics=("fixture sweep",),
        )

        class FakeExecutionSources:
            """Serve one verified fixture sweep without any network."""

            def __init__(self) -> None:
                self.discover_calls = 0

            def discover_pools(self) -> PoolDiscoveryResult:
                self.discover_calls += 1
                return sweep

            def read_token_decimals(self, token_address: str) -> int:
                return 8

            def load_registry(self) -> B20RegistryResult:
                return make_registry()

        fake_sources = FakeExecutionSources()
        sources, store = self._sources(
            tmp_path, script, monkeypatch, execution_sources=fake_sources
        )
        candidate, block = sources.resolve_pool("FIXc")
        assert fake_sources.discover_calls == 1
        assert block == 123
        assert candidate.pool_address == sweep_candidate.pool_address
        # The sweep's verified identity replaced the drifted pin.
        pins = store.load()
        assert pins["fixc"].nfpm_address == NFPM_ADDRESS.lower()

    def test_unknown_symbol_refuses(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A symbol outside the registry refuses without any reads."""
        script = KnownPoolRpcScript()
        sources, _ = self._sources(tmp_path, script, monkeypatch)
        with pytest.raises(ValueError, match="not in the official B20 registry"):
            sources.resolve_pool("NOPEc")


class TestProgressReporting:
    """A sweep reports honest per-page and retry progress."""

    def test_page_progress_lines_reach_the_callback(self) -> None:
        """Each enumerated page emits one progress line to the callback."""
        from aero_bot.sugar import LpSugarRpcBackend

        lines: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            if body["method"] == "eth_blockNumber":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(100)})
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": EMPTY_PAGE_RESULT}
            )

        backend = LpSugarRpcBackend(
            rpc_url="https://example.invalid",
            page_size=1,
            sleep=lambda _seconds: None,
            transport=httpx.MockTransport(handler),
            progress=lines.append,
        )
        batch = backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)
        assert batch.candidates == ()
        assert lines == ["lp sugar enumeration: 0 pools enumerated at block 100"]

    def test_retry_backoff_reports_the_failure(self) -> None:
        """A retriable failure reports the attempt and its backoff."""
        from aero_bot.sugar import LpSugarRpcBackend

        lines: list[str] = []
        requests_seen: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            if body["method"] == "eth_blockNumber":
                requests_seen.append(1)
                if len(requests_seen) == 1:
                    return httpx.Response(429)
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(100)})
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": EMPTY_PAGE_RESULT}
            )

        backend = LpSugarRpcBackend(
            rpc_url="https://example.invalid",
            page_size=1,
            sleep=lambda _seconds: None,
            transport=httpx.MockTransport(handler),
            progress=lines.append,
        )
        backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)
        assert any(
            "rpc eth_blockNumber attempt 2 of 5 failed" in line and "backing off 0.5s" in line
            for line in lines
        )


class TestSugarPageDefault:
    """The default page size halves sweep requests against the contract cap."""

    def test_default_page_size_is_the_contract_cap(self) -> None:
        """The sweep pages at the Sugar's own maximum."""
        from aero_bot.sugar import DEFAULT_PAGE_SIZE, MAX_POOLS_PER_PAGE

        assert DEFAULT_PAGE_SIZE == MAX_POOLS_PER_PAGE == 500


def test_known_pool_source_label_is_stable() -> None:
    """The fast path's source label is a pinned constant."""
    assert KNOWN_POOL_SOURCE == "known-pool-fast-path"
