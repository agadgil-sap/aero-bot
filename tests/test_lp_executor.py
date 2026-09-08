"""Behavior tests for the capped manual LP lifecycle execution module."""

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal, localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

import httpx
import pytest
from eth_abi.abi import decode
from eth_account import Account

from aero_bot.audit import AuditEventType, AuditStore
from aero_bot.executor import (
    AERODROME_ROUTER_ADDRESS,
    AERODROME_VOLATILE_FACTORY_ADDRESS,
    DEFAULT_APPROVAL_STANDING_CAP_USDC,
    ERC20_ALLOWANCE_SELECTOR,
    ERC20_BALANCE_OF_SELECTOR,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REFUSED,
    SAFE_ADDRESS_ENV,
    ExecutionUnavailableError,
    ExecutorRpcBackend,
    build_approval_calldata,
    build_swap_path,
)
from aero_bot.history import price_usdc_per_stock
from aero_bot.lp_calldata import (
    MAX_UINT128,
    LpCollectParams,
    LpDecreaseLiquidityParams,
    LpMintParams,
    build_gauge_get_reward_calldata,
    build_gauge_withdraw_calldata,
    build_lp_burn_calldata,
    build_lp_collect_calldata,
    build_lp_decrease_liquidity_calldata,
    build_lp_mint_calldata,
)
from aero_bot.lp_executor import (
    ERC721_TOKEN_OF_OWNER_BY_INDEX_SELECTOR,
    NFPM_INCREASE_LIQUIDITY_TOPIC0,
    LpExecutionRefusalCode,
    LpExecutionRefusalError,
    LpExecutionRole,
    LpLifecycleExecutor,
    LpSafeExecutionPolicy,
    main,
)
from aero_bot.lp_pins import LpPoolPin, LpPoolPinStore
from aero_bot.lp_plan import (
    DEFAULT_MINT_SLIPPAGE_TOLERANCE,
    LpExecutionPolicy,
    LpPlanRefusalError,
    position_amounts_at_sqrt_ratio,
)
from aero_bot.registry import B20AssetListing, B20RegistryResult, RegistryStatus
from aero_bot.safe_tx import (
    CHECK_SIGNATURES_SELECTOR,
    SAFE_NONCE_SELECTOR,
    SafeTransactionRpcBackend,
)
from aero_bot.venues import (
    AERO_TOKEN_ADDRESS,
    BASE_USDC_ADDRESS,
    SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
    PoolCandidate,
    PoolDiscoveryResult,
    PoolDiscoveryStatus,
    PoolKind,
    VenueId,
)

# Fixture identities mirror official contracts without claiming live observations.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
POOL_ADDRESS = "0x1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a"
GAUGE_ADDRESS = "0x3111111111111111111111111111111111111111"
NFPM_ADDRESS = "0xe1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"
# The live CLGaugeFactory the gauge's own gaugeFactory() view names; penalty
# views live there, not on the gauge or the v3 gauges factory.
GAUGE_FACTORY_ADDRESS = "0x385293cae378c813f16f0c1334d774adddf56abb"
SAFE_ADDRESS = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
# A distinct Safe the environment-override test threads through the CLI.
OVERRIDE_SAFE_ADDRESS = "0x5aa5aa5aa5aa5aa5aa5aa5aa5aa5aa5aa5aa5aa5"
# The fixed fixture clock makes deadlines and audit timestamps deterministic.
BASE_NOW = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
# The fixture stock uses eight decimals like every real B20 stock.
STOCK_DECIMALS = 8
# The fixture pool grids on ten-tick spacings like the live AAPLc pool.
LP_TICK_SPACING = 10
# A current tick of -15 anchors at -20, so a one-spacing range spans [-30, -10).
LP_CURRENT_TICK = -15
LP_ANCHOR_TICK = -20
LP_RANGE_LOWER = -30
LP_RANGE_UPPER = -10
# The fixture sqrt price sits exactly at the anchor tick, strictly inside the
# derived range, mirroring the planner's exact per-tick formula.
with localcontext() as _fixture_context:
    _fixture_context.prec = 60
    LP_SQRT_RATIO = int((Decimal("1.0001") ** Decimal(-10) * (1 << 96)).to_integral_value())
# A large active liquidity keeps the one-percent depth cap far from binding.
LP_ACTIVE_LIQUIDITY = 10**20
# One million whole USDC of reserve keeps the swap impact a single small tranche.
FIXTURE_USDC_RESERVE = 1_000_000_000_000
# A reserve this size pushes the modeled impact just over the tranche threshold
# while staying below the refusal ceiling.
TRANCHE_USDC_RESERVE = 4_600_000_000
# The fixture Safe holds ten whole USDC, enough for a seven-USDC entry.
FIXTURE_SAFE_USDC_UNITS = 10_000_000
# Held stock large enough to cover the stock side without any balancing swap.
FIXTURE_SAFE_STOCK_UNITS = 10**9
# The fixture gas price sits far below the one-gwei cap.
FIXTURE_GAS_PRICE_WEI = 100_000_000
# The fixture Safe balance clears the ETH floor.
FIXTURE_SAFE_ETH_WEI = 10**16
# The canary mint budget for every fixture dry run.
MINT_BUDGET_USDC = Decimal("7")
# One whole USDC in raw six-decimal units.
ONE_USDC_UNITS = 1_000_000
# The explicit one-spacing half width every fixture directive carries.
MINT_WIDTH_SPACINGS = 1
# The whole-word allowance fixtures that satisfy every skip condition.
SATISFIED_ALLOWANCE_UNITS = 10**9
# The fixture penalty window: deposited 1000 seconds ago under a 300-second
# minimum and a total-forfeiture rate, so the window has already cleared.
PENALTY_DEPOSIT_AGE_SECONDS = 1000
PENALTY_MIN_STAKE_SECONDS = 300
PENALTY_RATE_BPS = 10_000
# A fixture AERO price for status quotes; one USDC keeps the math readable.
FIXTURE_AERO_PRICE_USDC = Decimal("1")
# The scripted canonical USDC/AERO pair: 1 AERO priced at the fixture price.
AERO_POOL_ADDRESS = "0x" + "ee" * 20


def word_hex(value: int) -> str:
    """Encode one integer as the 0x-prefixed 32-byte word JSON-RPC returns."""
    return "0x" + value.to_bytes(32, "big").hex()


def signed_word(value: int) -> bytes:
    """Encode one signed integer as its two's-complement 32-byte word."""
    return (value % 2**256).to_bytes(32, "big")


def address_word(address: str) -> str:
    """Encode one lowercase address as its low-160-bit word hex."""
    return address[2:].rjust(64, "0")


def address_argument(calldata: str, index: int) -> str:
    """Read one address argument out of a word-aligned calldata payload."""
    word = calldata[2 + 8 + 64 * index : 2 + 8 + 64 * (index + 1)]
    return "0x" + word[24:]


def make_candidate(**overrides: object) -> PoolCandidate:
    """Build one accepted official B20/native-USDC Slipstream candidate.

    Args:
        **overrides: Candidate fields changed for one behavior test.

    Returns:
        A validated immutable pool candidate with USDC as token0 and the
        NFPM, gauge, and active liquidity the LP lifecycle resolves.
    """
    values: dict[str, object] = {
        "pool_address": POOL_ADDRESS,
        "factory_address": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
        "token0_address": BASE_USDC_ADDRESS,
        "token1_address": B20_ADDRESS,
        "pool_kind": PoolKind.SLIPSTREAM,
        "tick_spacing": LP_TICK_SPACING,
        "current_tick": LP_CURRENT_TICK,
        "sqrt_ratio": LP_SQRT_RATIO,
        "pool_fee_ppm": 500,
        "unstaked_fee_ppm": 100_000,
        "reserve0": FIXTURE_USDC_RESERVE,
        "reserve1": 40_000_000_000,
        "staked0": 250_000_000,
        "staked1": 3 * 10**STOCK_DECIMALS,
        "gauge_address": GAUGE_ADDRESS,
        "gauge_liquidity": 9_999,
        "gauge_alive": True,
        "emissions_per_second": 4_494_371_922_759_724,
        "emissions_token_address": AERO_TOKEN_ADDRESS,
        "nfpm_address": NFPM_ADDRESS,
        "pool_active_liquidity": LP_ACTIVE_LIQUIDITY,
    }
    values.update(overrides)
    return PoolCandidate.model_validate(values)


def make_pool_pin(**overrides: object) -> LpPoolPin:
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
        "stock_decimals": STOCK_DECIMALS,
        "pinned_at": BASE_NOW,
        "pinned_block": 100,
        "discovery_source": "lp-sugar:fixture@block:100",
    }
    values.update(overrides)
    return LpPoolPin.model_validate(values)


def make_discovery(
    pools: tuple[PoolCandidate, ...] | None = None,
    status: PoolDiscoveryStatus = PoolDiscoveryStatus.VERIFIED,
    snapshot_block: int | None = 123,
    observed_at: datetime | None = BASE_NOW,
) -> PoolDiscoveryResult:
    """Build one adapter-shaped discovery result.

    Args:
        pools: Accepted pool candidates carried by the result.
        status: Discovery status the executor must handle.
        snapshot_block: Sugar snapshot pin block, or None for the bare shape.
        observed_at: Snapshot observation time, or None when absent.

    Returns:
        A validated immutable discovery result.
    """
    return PoolDiscoveryResult(
        venue=VenueId.AERODROME,
        status=status,
        source=f"lp-sugar:fixture@block:{snapshot_block}",
        observed_at=observed_at,
        snapshot_block=snapshot_block,
        pools=pools if pools is not None else (make_candidate(),),
        diagnostics=("fixture summary",),
    )


def make_registry(
    status: RegistryStatus = RegistryStatus.VERIFIED,
    assets: tuple[B20AssetListing, ...] | None = None,
) -> B20RegistryResult:
    """Build one registry result with one fixture FIXc listing by default.

    Args:
        status: Registry status the executor must gate on.
        assets: Listings exposed, defaulting to one FIXc listing.

    Returns:
        A validated immutable registry result.
    """
    listing = B20AssetListing(
        symbol="FIXc",
        name="Fixture Stock",
        address=B20_ADDRESS,
        explorer_url="https://basescan.org/token/fixture",
    )
    return B20RegistryResult(
        status=status,
        source_url="https://www.base.org/stocks",
        source_observed_at=date(2026, 9, 1) if status is RegistryStatus.VERIFIED else None,
        assets=assets if assets is not None else (listing,),
        diagnostic="fixture registry",
    )


class FakeSources:
    """Serve the registry, discovery, and decimals one LP run consumes."""

    def __init__(
        self,
        registry: B20RegistryResult | None = None,
        discovery: PoolDiscoveryResult | None = None,
        decimals: dict[str, int] | None = None,
    ) -> None:
        """Configure the served sources with verified defaults.

        Args:
            registry: Registry result served to every load_registry call.
            discovery: Discovery result served to every discover_pools call.
            decimals: Decimal counts served per token address.
        """
        self._registry = registry if registry is not None else make_registry()
        self._discovery = discovery if discovery is not None else make_discovery()
        self._decimals = decimals if decimals is not None else {B20_ADDRESS: STOCK_DECIMALS}
        # Counting serves proves the fast path never enumerates.
        self.discover_calls = 0

    def load_registry(self) -> B20RegistryResult:
        """Return the configured registry result."""
        return self._registry

    def discover_pools(self) -> PoolDiscoveryResult:
        """Return the configured discovery result, counting every call."""
        self.discover_calls += 1
        return self._discovery

    def read_token_decimals(self, token_address: str) -> int:
        """Return the configured decimal count for one token."""
        return self._decimals[token_address.lower()]


class LpRpcScript:
    """Serve scripted JSON-RPC results for the LP executor's read calls."""

    def __init__(
        self,
        *,
        gas_price_wei: int = FIXTURE_GAS_PRICE_WEI,
        safe_eth_wei: int = FIXTURE_SAFE_ETH_WEI,
        usdc_balance_units: int = FIXTURE_SAFE_USDC_UNITS,
        stock_balance_units: int = 0,
        nfpm_held_positions: int = 0,
        nfpm_balance_hex: str | None = None,
        held_token_ids: list[int] | None = None,
        router_allowance_units: int = 0,
        stock_router_allowance_units: int = 0,
        nfpm_usdc_allowance_units: int = 0,
        nfpm_stock_allowance_units: int = 0,
        owner_addresses: dict[int, str] | None = None,
        operator_approved: bool = False,
        position_words: list[bytes] | None = None,
        approval_gas_estimate: int | None = 60_000,
        swap_gas_estimate: int | None = 200_000,
        mint_gas_estimate: int | None = 400_000,
        deposit_gas_estimate: int | None = 120_000,
        gauge_earned_units: int = 0,
        gauge_rewards_units: int = 0,
        penalty_rate_bps: int = PENALTY_RATE_BPS,
        min_stake_seconds: int = PENALTY_MIN_STAKE_SECONDS,
        deposit_age_seconds: int = PENALTY_DEPOSIT_AGE_SECONDS,
        penalty_reads_revert: bool = False,
        withdraw_gas_estimate: int | None = 130_000,
        decrease_gas_estimate: int | None = 160_000,
        collect_gas_estimate: int | None = 90_000,
        burn_gas_estimate: int | None = 60_000,
        get_reward_gas_estimate: int | None = 110_000,
        allow_broadcasts: bool = False,
        relayer_eth_wei: int = 10**15,
        relayer_starting_nonce: int = 3,
        receipt_status: int = 1,
        receipt_present: bool = True,
        estimate_reverts_after: int | None = None,
        estimate_revert_message: str = "execution reverted: PSC",
        estimate_gs026_lag_calls: int = 0,
        estimate_gs026_lag_from: int = 1,
        fast_block_number: int = 51_000_000,
        fast_sqrt_ratio: int | None = None,
        fast_current_tick: int | None = None,
        fast_active_liquidity: int = LP_ACTIVE_LIQUIDITY,
        fast_staked_liquidity: int = 9_999,
        fast_usdc_reserve_units: int = FIXTURE_USDC_RESERVE,
        fast_reward_rate_units: int = 4_494_371_922_759_724,
        fast_token1_address: str | None = None,
        fast_views_revert: bool = False,
        aero_price_usdc: Decimal | None = Decimal("0.5"),
    ) -> None:
        """Configure every scripted answer the LP executor's calls receive.

        Args:
            gas_price_wei: Suggested gas price served to eth_gasPrice.
            safe_eth_wei: Balance served for the Safe address.
            usdc_balance_units: The Safe's USDC balanceOf answer.
            stock_balance_units: The Safe's stock balanceOf answer.
            nfpm_held_positions: The Safe's NFPM position-NFT balanceOf answer.
            nfpm_balance_hex: Raw result served for that NFPM balanceOf read,
                bypassing the held-positions word to script malformed returns.
            held_token_ids: Token ids served by tokenOfOwnerByIndex in order;
                an index past the list (or None with a positive balance)
                reverts like an out-of-range enumeration.
            router_allowance_units: Standing USDC allowance for the router.
            stock_router_allowance_units: Standing stock allowance for the
                router, backing the exit-swap approval skip.
            nfpm_usdc_allowance_units: Standing USDC allowance for the NFPM.
            nfpm_stock_allowance_units: Standing stock allowance for the NFPM.
            owner_addresses: ownerOf answers keyed by token id; missing ids
                revert like an unminted position.
            operator_approved: The NFPM isApprovedForAll answer for the gauge.
            position_words: Twelve encoded words for the positions view, or
                None to make that view revert.
            approval_gas_estimate: Gas served for approve-style inner calls,
                or None to make those estimates revert.
            swap_gas_estimate: Gas served for the router swap inner call, or
                None to make that estimate revert.
            mint_gas_estimate: Gas served for the mint inner call, or None to
                make that estimate revert.
            deposit_gas_estimate: Gas served for the deposit inner call, or
                None to make that estimate revert.
            gauge_earned_units: The gauge earned(address,uint256) answer.
            gauge_rewards_units: The gauge rewards(uint256) checkpoint answer.
            penalty_rate_bps: The factory penaltyRate() answer.
            min_stake_seconds: The factory minStakeTimes(pool) answer.
            deposit_age_seconds: How long ago depositTimestamp(tokenId) sits;
                the window clears at deposit plus the minimum stake time.
            penalty_reads_revert: Make every penalty-window read revert, so the
                fail-closed path can be exercised.
            withdraw_gas_estimate: Gas served for the gauge withdraw, or None
                to make that estimate revert.
            decrease_gas_estimate: Gas served for decreaseLiquidity, or None
                to make that estimate revert.
            collect_gas_estimate: Gas served for NFPM collect, or None to make
                that estimate revert.
            burn_gas_estimate: Gas served for NFPM burn, or None to make that
                estimate revert.
            get_reward_gas_estimate: Gas served for gauge getReward, or None
                to make that estimate revert.
            allow_broadcasts: Whether eth_sendRawTransaction is served; the
                default keeps the no-broadcast trap for every dry-run path.
            relayer_eth_wei: Balance served for the relaying EOA address.
            relayer_starting_nonce: First pending nonce served per send.
            receipt_status: Status word served for included deliveries.
            receipt_present: Whether receipts are served at all; False keeps
                every poll empty so the bounded wait can time out.
            estimate_reverts_after: Make every estimateGas call past this
                count revert with estimate_revert_message, counting both the
                build-time and execute-time estimates.
            estimate_revert_message: The revert message for the cap above.
            estimate_gs026_lag_calls: How many estimateGas calls revert with
                a GS026 lag marker before resuming the scripted answers.
            estimate_gs026_lag_from: The 1-based estimateGas call the GS026
                lag window starts at, so tests can target execute-time
                estimates behind the build-time ones.
            fast_block_number: Block served to the fast path's eth_blockNumber.
            fast_sqrt_ratio: Live sqrtPriceX96 served in the pool's slot0;
                defaults to the discovery fixture's price so both paths agree.
            fast_current_tick: Live tick served in the pool's slot0 word.
            fast_active_liquidity: Live liquidity() answer for the depth cap.
            fast_staked_liquidity: Live stakedLiquidity() answer.
            fast_usdc_reserve_units: USDC balanceOf(pool) answer, the swap
                impact base on the fast path.
            fast_reward_rate_units: The gauge's live rewardRate() answer.
            fast_token1_address: Overrides the pool's token1 identity answer
                to script a stale pin's identity mismatch.
            fast_views_revert: Every fast-path pool view reverts, scripting an
                unreadable known pool.
            aero_price_usdc: Live USDC/AERO price served to the price read;
                None makes that read revert so the fail-closed path tests.
        """
        self.gas_price_wei = gas_price_wei
        self.safe_eth_wei = safe_eth_wei
        self.usdc_balance_units = usdc_balance_units
        self.stock_balance_units = stock_balance_units
        self.nfpm_held_positions = nfpm_held_positions
        self.nfpm_balance_hex = nfpm_balance_hex
        self.held_token_ids = held_token_ids
        self.router_allowance_units = router_allowance_units
        self.stock_router_allowance_units = stock_router_allowance_units
        self.nfpm_usdc_allowance_units = nfpm_usdc_allowance_units
        self.nfpm_stock_allowance_units = nfpm_stock_allowance_units
        self.owner_addresses = owner_addresses if owner_addresses is not None else {}
        self.operator_approved = operator_approved
        self.position_words = position_words
        self.approval_gas_estimate = approval_gas_estimate
        self.swap_gas_estimate = swap_gas_estimate
        self.mint_gas_estimate = mint_gas_estimate
        self.deposit_gas_estimate = deposit_gas_estimate
        self.gauge_earned_units = gauge_earned_units
        self.gauge_rewards_units = gauge_rewards_units
        self.penalty_rate_bps = penalty_rate_bps
        self.min_stake_seconds = min_stake_seconds
        self.deposit_timestamp = int(BASE_NOW.timestamp()) - deposit_age_seconds
        self.penalty_reads_revert = penalty_reads_revert
        self.withdraw_gas_estimate = withdraw_gas_estimate
        self.decrease_gas_estimate = decrease_gas_estimate
        self.collect_gas_estimate = collect_gas_estimate
        self.burn_gas_estimate = burn_gas_estimate
        self.get_reward_gas_estimate = get_reward_gas_estimate
        self.broadcasts: list[str] = []
        self.estimate_requests: list[str] = []
        self.allow_broadcasts = allow_broadcasts
        self.relayer_eth_wei = relayer_eth_wei
        self.relayer_next_nonce = relayer_starting_nonce
        self.receipt_status = receipt_status
        self.receipt_present = receipt_present
        self.estimate_reverts_after = estimate_reverts_after
        self.estimate_revert_message = estimate_revert_message
        self.estimate_gs026_lag_remaining = estimate_gs026_lag_calls
        self.estimate_gs026_lag_from = estimate_gs026_lag_from
        self.fast_block_number = fast_block_number
        self.fast_sqrt_ratio = LP_SQRT_RATIO if fast_sqrt_ratio is None else fast_sqrt_ratio
        self.fast_current_tick = LP_CURRENT_TICK if fast_current_tick is None else fast_current_tick
        self.fast_active_liquidity = fast_active_liquidity
        self.fast_staked_liquidity = fast_staked_liquidity
        self.fast_usdc_reserve_units = fast_usdc_reserve_units
        self.fast_reward_rate_units = fast_reward_rate_units
        self.fast_token1_address = fast_token1_address
        self.fast_views_revert = fast_views_revert
        self.aero_price_usdc = aero_price_usdc
        self.inclusion_blocks = 51_000_000

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one JSON-RPC request from the scripted state."""
        call = json.loads(request.content)
        method = str(call["method"])
        params = call["params"]
        result: object
        if method == "eth_gasPrice":
            result = hex(self.gas_price_wei)
        elif method == "eth_getBalance":
            if str(params[0]).lower() == SAFE_ADDRESS:
                result = hex(self.safe_eth_wei)
            else:
                result = hex(self.relayer_eth_wei)
        elif method == "eth_getTransactionCount":
            result = hex(self.relayer_next_nonce)
            self.relayer_next_nonce += 1
        elif method == "eth_blockNumber":
            result = hex(self.fast_block_number)
        elif method == "eth_call":
            result = self._eth_call(str(params[0]["to"]).lower(), str(params[0]["data"]))
        elif method == "eth_estimateGas":
            calldata = str(params[0]["data"])
            self.estimate_requests.append(calldata)
            result = self._estimate(calldata)
        elif method == "eth_sendRawTransaction":
            if not self.allow_broadcasts:
                # A broadcast attempt without the confirmed execute path is a
                # containment failure and fails the test.
                self.broadcasts.append(str(params[0]))
                raise AssertionError("the LP executor must never broadcast anything")
            self.broadcasts.append(str(params[0]))
            result = "0x" + f"{len(self.broadcasts):064x}"
        elif method == "eth_getTransactionReceipt":
            if not self.receipt_present:
                result = None
            else:
                self.inclusion_blocks += 1
                result = {
                    "status": hex(self.receipt_status),
                    "blockNumber": hex(self.inclusion_blocks),
                    "gasUsed": hex(80_000),
                    "effectiveGasPrice": hex(self.gas_price_wei),
                }
        else:
            raise AssertionError(f"unexpected LP executor RPC method {method}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    def _eth_call(self, to_address: str, data: str) -> str:
        """Answer one read-only contract call from the scripted token state."""
        if to_address == AERODROME_VOLATILE_FACTORY_ADDRESS and data.startswith("0x79bc57d5"):
            if self.aero_price_usdc is None:
                raise _ScriptedRevertError("AERO price unavailable")
            return word_hex(int(AERO_POOL_ADDRESS, 16))
        if to_address == AERO_POOL_ADDRESS:
            if self.aero_price_usdc is None:
                raise _ScriptedRevertError("AERO price unavailable")
            if data.startswith("0x0dfe1681"):
                return word_hex(int(AERO_TOKEN_ADDRESS, 16))
            if data.startswith("0x443cb4bc"):
                return word_hex(10**18)  # one whole AERO
            if data.startswith("0x5a76f25e"):
                return word_hex(int(self.aero_price_usdc * 10**6))
        usdc_token = BASE_USDC_ADDRESS.lower()
        if to_address == POOL_ADDRESS:
            return self._pool_view(data)
        if data.startswith(f"0x{ERC20_ALLOWANCE_SELECTOR}"):
            spender = address_argument(data, 1)
            if to_address == usdc_token:
                if spender == AERODROME_ROUTER_ADDRESS:
                    return word_hex(self.router_allowance_units)
                if spender == NFPM_ADDRESS:
                    return word_hex(self.nfpm_usdc_allowance_units)
            elif to_address == B20_ADDRESS and spender == NFPM_ADDRESS:
                return word_hex(self.nfpm_stock_allowance_units)
            elif (
                to_address == B20_ADDRESS
                and spender == AERODROME_ROUTER_ADDRESS
                and data.startswith(f"0x{ERC20_ALLOWANCE_SELECTOR}")
            ):
                return word_hex(self.stock_router_allowance_units)
            raise AssertionError(f"unexpected allowance read to {to_address} for {spender}")
        if data.startswith(f"0x{ERC20_BALANCE_OF_SELECTOR}"):
            if to_address == usdc_token:
                balance_owner = address_argument(data, 0)
                if balance_owner == POOL_ADDRESS:
                    return word_hex(self.fast_usdc_reserve_units)
                return word_hex(self.usdc_balance_units)
            if to_address == B20_ADDRESS:
                return word_hex(self.stock_balance_units)
            if to_address == NFPM_ADDRESS:
                if self.nfpm_balance_hex is not None:
                    return self.nfpm_balance_hex
                return word_hex(self.nfpm_held_positions)
            raise AssertionError(f"unexpected balanceOf read on {to_address}")
        if data.startswith("0x6352211e"):
            token_id = int(data[2 + 8 : 2 + 8 + 64], 16)
            owner = self.owner_addresses.get(token_id)
            if owner is None:
                raise _ScriptedRevertError("ERC721: invalid token ID")
            return word_hex(int(owner, 16))
        if data.startswith("0x2f745c59"):
            index = int(data[2 + 8 + 64 : 2 + 8 + 128], 16)
            if self.held_token_ids is None or index >= len(self.held_token_ids):
                raise _ScriptedRevertError("ERC721: owner index out of range")
            return word_hex(self.held_token_ids[index])
        if data.startswith("0xe985e9c5"):
            return word_hex(1 if self.operator_approved else 0)
        if data.startswith("0x99fbab88"):
            if self.position_words is None:
                raise _ScriptedRevertError("NFPM: unknown token ID")
            return "0x" + b"".join(self.position_words).hex()
        if to_address == GAUGE_ADDRESS:
            if data.startswith("0x3e491d47"):
                self._require_penalty_reads()
                return word_hex(self.gauge_earned_units)
            if data.startswith("0xf301af42"):
                self._require_penalty_reads()
                return word_hex(self.gauge_rewards_units)
            if data.startswith("0x0d52333c"):
                self._require_penalty_reads()
                return word_hex(int(GAUGE_FACTORY_ADDRESS, 16))
            if data.startswith("0xf7c618c1"):
                if self.fast_views_revert:
                    raise _ScriptedRevertError("gauge views unavailable")
                return word_hex(int(AERO_TOKEN_ADDRESS, 16))
            if data.startswith("0x7b0a47ee"):
                if self.fast_views_revert:
                    raise _ScriptedRevertError("gauge views unavailable")
                return word_hex(self.fast_reward_rate_units)
            if data.startswith("0x4ede8c85"):
                self._require_penalty_reads()
                return word_hex(self.deposit_timestamp)
        if to_address == GAUGE_FACTORY_ADDRESS:
            if data.startswith("0xd6b7494f"):
                self._require_penalty_reads()
                return word_hex(self.penalty_rate_bps)
            if data.startswith("0xe782453b"):
                self._require_penalty_reads()
                return word_hex(self.min_stake_seconds)
            if data.startswith("0x47ccca02"):
                return word_hex(int(NFPM_ADDRESS, 16))
        raise AssertionError(f"unexpected LP eth_call payload {data[:10]}")

    def _pool_view(self, data: str) -> str:
        """Answer one fast-path identity or state view on the fixture pool."""
        if self.fast_views_revert:
            raise _ScriptedRevertError("pool views unavailable")
        if data.startswith("0x0dfe1681"):
            return word_hex(int(BASE_USDC_ADDRESS, 16))
        if data.startswith("0xd21220a7"):
            token1 = B20_ADDRESS if self.fast_token1_address is None else self.fast_token1_address
            return word_hex(int(token1, 16))
        if data.startswith("0xd0c93a7c"):
            return word_hex(LP_TICK_SPACING)
        if data.startswith("0xa6f19c84"):
            return word_hex(int(GAUGE_ADDRESS, 16))
        if data.startswith("0xc45a0155"):
            return word_hex(int(SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS, 16))
        if data.startswith("0x3850c7bd"):
            return (
                "0x"
                + word_hex(self.fast_sqrt_ratio)[2:]
                + signed_word(self.fast_current_tick).hex()
            )
        if data.startswith("0x1a686502"):
            return word_hex(self.fast_active_liquidity)
        if data.startswith("0x3ab04b20"):
            return word_hex(self.fast_staked_liquidity)
        raise AssertionError(f"unexpected pool view payload {data[:10]}")

    def _require_penalty_reads(self) -> None:
        """Raise the scripted revert when penalty-state reads must fail."""
        if self.penalty_reads_revert:
            raise _ScriptedRevertError("penalty state unavailable")

    def _estimate(self, calldata: str) -> str:
        """Answer one gas estimate by the inner call embedded in the exec data."""
        if (
            self.estimate_gs026_lag_remaining > 0
            and len(self.estimate_requests) >= self.estimate_gs026_lag_from
        ):
            self.estimate_gs026_lag_remaining -= 1
            raise _ScriptedRevertError("GS026")
        if (
            self.estimate_reverts_after is not None
            and len(self.estimate_requests) > self.estimate_reverts_after
        ):
            raise _ScriptedRevertError(
                self.estimate_revert_message.removeprefix("execution reverted: ")
            )
        inner = self._inner_calldata(calldata)
        selector = inner[:4].hex()
        if selector == "095ea7b3":
            return self._estimate_or_revert(self.approval_gas_estimate, "no allowance")
        if selector == "3593564c":
            return self._estimate_or_revert(self.swap_gas_estimate, "too little")
        if selector == "b5007d1f":
            return self._estimate_or_revert(self.mint_gas_estimate, "mint refused")
        if selector == "a22cb465":
            return self._estimate_or_revert(self.approval_gas_estimate, "operator refused")
        if selector == "b6b55f25":
            return self._estimate_or_revert(self.deposit_gas_estimate, "deposit refused")
        if selector == "2e1a7d4d":
            return self._estimate_or_revert(self.withdraw_gas_estimate, "withdraw refused")
        if selector == "0c49ccbe":
            return self._estimate_or_revert(self.decrease_gas_estimate, "decrease refused")
        if selector == "fc6f7865":
            return self._estimate_or_revert(self.collect_gas_estimate, "collect refused")
        if selector == "42966c68":
            return self._estimate_or_revert(self.burn_gas_estimate, "burn refused")
        if selector == "1c4b774b":
            return self._estimate_or_revert(self.get_reward_gas_estimate, "reward refused")
        raise AssertionError(f"unexpected inner selector {selector}")

    @staticmethod
    def _inner_calldata(calldata: str) -> bytes:
        """Extract the inner call bytes from one execTransaction payload."""
        words = decode(
            [
                "address",
                "uint256",
                "bytes",
                "uint8",
                "uint256",
                "uint256",
                "uint256",
                "address",
                "address",
                "bytes",
            ],
            bytes.fromhex(calldata[10:]),
        )
        return cast("bytes", words[2])

    @staticmethod
    def _estimate_or_revert(estimate: int | None, message: str) -> str:
        """Return one estimate hex, or raise the scripted revert marker."""
        if estimate is None:
            raise _ScriptedRevertError(message)
        return hex(estimate)

    def transport(self) -> httpx.MockTransport:
        """Build the deterministic HTTP transport over this script."""
        script = self

        def handler(request: httpx.Request) -> httpx.Response:
            try:
                return script.handler(request)
            except _ScriptedRevertError as revert:
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {"code": 3, "message": f"execution reverted: {revert}"},
                    },
                )

        return httpx.MockTransport(handler)


class _ScriptedRevertError(Exception):
    """Signal one scripted in-contract revert from inside the handler."""


class SafeRpcScript:
    """Serve scripted Safe nonces and signature verdicts over read-only calls."""

    def __init__(self, nonce_reads: list[int], signature_verdicts: list[bool]) -> None:
        """Configure the nonce and verdict sequences consumed by one run.

        Args:
            nonce_reads: Safe nonce served per fetch_live_nonce call, in order.
            signature_verdicts: checkSignatures verdict per validation call.
        """
        self.nonce_reads = list(nonce_reads)
        self.signature_verdicts = list(signature_verdicts)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one Safe read from the scripted sequences."""
        call = json.loads(request.content)
        data = str(call["params"][0]["data"])
        if data.startswith(f"0x{SAFE_NONCE_SELECTOR}"):
            return self._result(word_hex(self.nonce_reads.pop(0)))
        if data.startswith(f"0x{CHECK_SIGNATURES_SELECTOR}"):
            if not self.signature_verdicts.pop(0):
                return httpx.Response(
                    200,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "error": {"code": 3, "message": "GS026"},
                    },
                )
            return self._result("0x")
        raise AssertionError(f"unexpected Safe RPC call {data[:10]}")

    def transport(self) -> httpx.MockTransport:
        """Build the deterministic HTTP transport over this script."""
        return httpx.MockTransport(self.handler)

    @staticmethod
    def _result(result: object) -> httpx.Response:
        """Build one successful JSON-RPC response."""
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def make_lp_executor(
    *,
    sources: FakeSources | None = None,
    rpc_script: LpRpcScript | None = None,
    safe_script: SafeRpcScript | None = None,
    audit_path: Path | None = None,
    sleep: Callable[[float], None] | None = None,
    timer: Callable[[], float] | None = None,
    receipt_script: LpRpcScript | None = None,
    pool_pin_store: LpPoolPinStore | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[LpLifecycleExecutor, LpRpcScript, SafeRpcScript]:
    """Assemble one LP executor over fully scripted transport boundaries.

    Args:
        sources: Fake quote sources, verified by default.
        rpc_script: LP RPC script, defaulting to a clean five-step entry.
        safe_script: Safe RPC script, matching nonces and accepted signatures.
        audit_path: Optional real audit-store path for persistence tests.
        sleep: Optional injected delay for the execute path's bounded waits.
        timer: Optional injected monotonic clock for the execute path's
            bounded receipt wait.
        receipt_script: Optional second RPC script polled for receipts; when
            present it joins the receipt-backend rotation behind the primary.
        pool_pin_store: Optional pin store arming the known-pool fast path.
        now: Optional injected wall clock; defaults to the fixture instant.

    Returns:
        The executor plus both scripts so tests can inspect the exchanges.
    """
    if rpc_script is None:
        rpc_script = LpRpcScript()
    expected_steps = 5
    if safe_script is None:
        safe_script = SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * expected_steps)
    audit_sink = AuditStore(audit_path) if audit_path is not None else None
    receipt_backends = [
        ExecutorRpcBackend(rpc_url="https://fixture.example", transport=rpc_script.transport())
    ]
    if receipt_script is not None:
        receipt_backends.append(
            ExecutorRpcBackend(
                rpc_url="https://receipts.example", transport=receipt_script.transport()
            )
        )
    executor = LpLifecycleExecutor(
        policy=LpSafeExecutionPolicy(),
        plan_policy=LpExecutionPolicy(),
        safe_address=SAFE_ADDRESS,
        sources=sources if sources is not None else FakeSources(),
        rpc=ExecutorRpcBackend(rpc_url="https://fixture.example", transport=rpc_script.transport()),
        safe_rpc=SafeTransactionRpcBackend(
            rpc_url="https://fixture.example",
            safe_address=SAFE_ADDRESS,
            transport=safe_script.transport(),
        ),
        audit_sink=audit_sink,
        now=now if now is not None else (lambda: BASE_NOW),
        sleep=sleep if sleep is not None else (lambda _seconds: None),
        **({"timer": timer} if timer is not None else {}),
        receipt_backends=receipt_backends,
        pool_pin_store=pool_pin_store,
    )
    return executor, rpc_script, safe_script


def fixture_deadline() -> int:
    """Return the deterministic deadline the fixture clock produces."""
    return int(BASE_NOW.timestamp()) + 480


def decode_inner(calldata: str) -> bytes:
    """Extract the inner call bytes one execTransaction payload carries."""
    return LpRpcScript._inner_calldata(calldata)


# ---------------------------------------------------------------------------
# Mint dry-run composition
# ---------------------------------------------------------------------------


def test_dry_run_mint_composes_the_full_entry_sequence() -> None:
    """A cash-poor Safe composes allowance, swap, approvals, and the mint."""
    executor, rpc_script, _ = make_lp_executor()
    key = Account.create()

    report = executor.dry_run_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(key.key))

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (
        LpExecutionRole.ROUTER_ALLOWANCE,
        LpExecutionRole.BALANCING_SWAP,
        LpExecutionRole.NFPM_USDC_ALLOWANCE,
        LpExecutionRole.NFPM_STOCK_ALLOWANCE,
        LpExecutionRole.MINT,
    )
    assert [transaction.nonce for transaction in report.transactions] == [4, 5, 6, 7, 8]
    assert all(transaction.signature_verified for transaction in report.transactions)
    assert all(transaction.gas_estimate is not None for transaction in report.transactions)
    assert report.plan.position_range.tick_lower == LP_RANGE_LOWER
    assert report.plan.position_range.tick_upper == LP_RANGE_UPPER
    assert report.plan.balancing_swap.required
    assert report.relayer_address == key.address.lower()
    assert report.ephemeral_key is False
    assert any("untracked position NFTs" in cap for cap in report.caps_enforced)
    assert any("in-range depth" in cap for cap in report.caps_enforced)
    # Nothing was broadcast anywhere in the attempt.
    assert rpc_script.broadcasts == []
    assert len(rpc_script.estimate_requests) == 5


def test_dry_run_mint_encodes_every_inner_call_from_the_plan() -> None:
    """Each built transaction carries exactly the plan-derived inner calldata."""
    executor, rpc_script, _ = make_lp_executor()

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    plan = report.plan
    usdc_desired = plan.amounts.amount0_desired_units
    stock_desired = plan.amounts.amount1_desired_units
    swap = plan.balancing_swap
    tolerance = plan.amounts.slippage_tolerance_fraction
    amount_out_min = int(
        (Decimal(swap.expected_stock_units) * (Decimal(1) - tolerance)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    expected = {
        LpExecutionRole.ROUTER_ALLOWANCE: build_approval_calldata(
            AERODROME_ROUTER_ADDRESS, int(DEFAULT_APPROVAL_STANDING_CAP_USDC * 10**6)
        ),
        LpExecutionRole.NFPM_USDC_ALLOWANCE: build_approval_calldata(NFPM_ADDRESS, usdc_desired),
        LpExecutionRole.NFPM_STOCK_ALLOWANCE: build_approval_calldata(NFPM_ADDRESS, stock_desired),
        LpExecutionRole.MINT: build_lp_mint_calldata(
            LpMintParams(
                token0_address=BASE_USDC_ADDRESS,
                token1_address=B20_ADDRESS,
                tick_spacing=LP_TICK_SPACING,
                tick_lower=LP_RANGE_LOWER,
                tick_upper=LP_RANGE_UPPER,
                amount0_desired_units=plan.amounts.amount0_desired_units,
                amount1_desired_units=plan.amounts.amount1_desired_units,
                amount0_min_units=plan.amounts.amount0_min_units,
                amount1_min_units=plan.amounts.amount1_min_units,
                recipient_address=SAFE_ADDRESS,
                deadline=fixture_deadline(),
                sqrt_price_x96=0,
            )
        ),
    }
    for transaction, calldata in zip(
        report.transactions, rpc_script.estimate_requests, strict=True
    ):
        inner = decode_inner(calldata)
        if transaction.role is LpExecutionRole.BALANCING_SWAP:
            commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], inner[4:])
            recipient, amount_in, minimum, _, _, _ = decode(
                ["address", "uint256", "uint256", "bytes", "bool", "uint256"], inputs[0]
            )
            assert commands == b"\x00"
            assert deadline == fixture_deadline()
            assert recipient == SAFE_ADDRESS
            assert amount_in == swap.usdc_in_units
            assert minimum == amount_out_min
        else:
            assert "0x" + inner.hex() == expected[transaction.role]


def test_dry_run_mint_skips_satisfied_allowances_and_the_swap() -> None:
    """Held stock and standing allowances collapse the sequence to the mint."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            stock_balance_units=FIXTURE_SAFE_STOCK_UNITS,
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
        ),
        safe_script=SafeRpcScript(nonce_reads=[9], signature_verdicts=[True]),
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    assert not report.plan.balancing_swap.required
    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.MINT,)
    assert report.transactions[0].nonce == 9
    assert report.router_usdc_allowance_units == SATISFIED_ALLOWANCE_UNITS
    assert len(rpc_script.estimate_requests) == 1


def test_dry_run_mint_reports_a_rejected_signature_honestly() -> None:
    """A non-owner key still builds, but every verdict reports rejection."""
    executor, _, _ = make_lp_executor(
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[False] * 5)
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    assert len(report.transactions) == 5
    assert all(not transaction.signature_verified for transaction in report.transactions)
    assert all("GS026" in transaction.signature_diagnostic for transaction in report.transactions)


def test_dry_run_mint_labels_estimate_reverts_behind_predecessors() -> None:
    """A sequenced transaction's reverting estimate carries the expected note."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(swap_gas_estimate=None),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    swap_transaction = report.transactions[1]
    assert swap_transaction.role is LpExecutionRole.BALANCING_SWAP
    assert swap_transaction.gas_estimate is None
    assert "estimate reverted" in swap_transaction.gas_estimate_diagnostic
    assert "predecessors in the sequence remain unexecuted" in (
        swap_transaction.gas_estimate_diagnostic
    )
    # The mint's own estimate still succeeded.
    assert report.transactions[4].gas_estimate is not None


def test_dry_run_mint_labels_a_first_transaction_estimate_revert_plainly() -> None:
    """A lone sequence's reverting estimate carries no predecessor excuse."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            stock_balance_units=FIXTURE_SAFE_STOCK_UNITS,
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            mint_gas_estimate=None,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True]),
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    mint_transaction = report.transactions[0]
    assert mint_transaction.gas_estimate is None
    assert "estimate reverted" in mint_transaction.gas_estimate_diagnostic
    assert "predecessors" not in mint_transaction.gas_estimate_diagnostic


# ---------------------------------------------------------------------------
# Known-pool fast path
# ---------------------------------------------------------------------------


def test_full_sweeps_persist_the_verified_pool_pin(tmp_path: Path) -> None:
    """A sweep-resolved pool is pinned so the next run skips enumeration."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    sources = FakeSources()
    executor, _, _ = make_lp_executor(sources=sources, pool_pin_store=store)

    executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert sources.discover_calls == 1
    pins = store.load()
    assert set(pins) == {"fixc"}
    pin = pins["fixc"]
    assert pin.pool_address == POOL_ADDRESS
    assert pin.factory_address == SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS.lower()
    assert pin.token0_address == BASE_USDC_ADDRESS.lower()
    assert pin.token1_address == B20_ADDRESS
    assert pin.tick_spacing == LP_TICK_SPACING
    assert pin.gauge_address == GAUGE_ADDRESS
    assert pin.nfpm_address == NFPM_ADDRESS
    assert pin.stock_decimals == STOCK_DECIMALS
    assert pin.pinned_block == 123
    assert pin.discovery_source == "lp-sugar:fixture@block:123"


def test_known_pool_skips_enumeration_entirely(tmp_path: Path) -> None:
    """A pinned pool dry-runs with zero discovery calls and the fast-path label."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    store.save_pin(make_pool_pin())
    sources = FakeSources()
    executor, _, _ = make_lp_executor(
        sources=sources,
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
        pool_pin_store=store,
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    assert sources.discover_calls == 0
    assert report.plan.pool_address == POOL_ADDRESS
    assert any("known-pool fast path" in cap for cap in report.caps_enforced)
    assert any(
        "token within the USDC-plus-registry whitelist" in cap for cap in report.caps_enforced
    )


def test_known_pool_prices_from_live_state_reads(tmp_path: Path) -> None:
    """The fast path's plan derives from the live slot0 tick, not the sweep."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    store.save_pin(make_pool_pin())
    # A live tick of -25 anchors at -30, so the range spans [-40, -20) -
    # distinct from the sweep fixture's -15 tick and its [-30, -10) range.
    # The sqrt price follows the fixture convention (1.0001**t * 2**96),
    # which sits strictly inside that range's bound ratios.
    with localcontext() as live_context:
        live_context.prec = 60
        live_sqrt_ratio = int((Decimal("1.0001") ** Decimal(-15) * (1 << 96)).to_integral_value())
    executor, _, _ = make_lp_executor(
        sources=FakeSources(),
        rpc_script=LpRpcScript(fast_current_tick=-25, fast_sqrt_ratio=live_sqrt_ratio),
        pool_pin_store=store,
    )

    plan = executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert plan.position_range.tick_lower == -40
    assert plan.position_range.tick_upper == -20
    assert plan.snapshot_block == 51_000_000


def test_known_pool_identity_mismatch_falls_back_and_rewrites_the_pin(
    tmp_path: Path,
) -> None:
    """A stale pin re-enumerates, still resolves, and refreshes the pin."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    store.save_pin(make_pool_pin(pinned_block=100))
    sources = FakeSources()
    executor, _, _ = make_lp_executor(
        sources=sources,
        rpc_script=LpRpcScript(fast_token1_address=SAFE_ADDRESS),
        pool_pin_store=store,
    )

    plan = executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert sources.discover_calls == 1
    assert plan.pool_address == POOL_ADDRESS
    # The sweep's snapshot block proves the full path rebuilt the observation.
    assert plan.snapshot_block == 123
    refreshed = store.load()["fixc"]
    assert refreshed.pinned_block == 123
    assert refreshed.discovery_source == "lp-sugar:fixture@block:123"


def test_known_pool_unreadable_views_fall_back_to_discovery(tmp_path: Path) -> None:
    """Reverting pool views fail safe into the full sweep."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    store.save_pin(make_pool_pin())
    sources = FakeSources()
    executor, _, _ = make_lp_executor(
        sources=sources,
        rpc_script=LpRpcScript(fast_views_revert=True),
        pool_pin_store=store,
    )

    plan = executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert sources.discover_calls == 1
    assert plan.pool_address == POOL_ADDRESS


def test_known_pool_keeps_every_registry_refusal(tmp_path: Path) -> None:
    """Registry gates run before the fast path and still refuse with codes."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    store.save_pin(make_pool_pin())
    unverified_sources = FakeSources(registry=make_registry(status=RegistryStatus.UNAVAILABLE))
    unverified_executor, _, _ = make_lp_executor(sources=unverified_sources, pool_pin_store=store)
    with pytest.raises(LpExecutionRefusalError) as unverified:
        unverified_executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)
    assert unverified.value.code is LpExecutionRefusalCode.REGISTRY_UNVERIFIED

    unknown_executor, _, _ = make_lp_executor(pool_pin_store=store)
    with pytest.raises(LpExecutionRefusalError) as unknown:
        unknown_executor.plan_mint("NOPEc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)
    assert unknown.value.code is LpExecutionRefusalCode.SYMBOL_NOT_IN_REGISTRY
    assert unverified_sources.discover_calls == 0


def test_known_pool_enforces_every_planner_cap(tmp_path: Path) -> None:
    """The fast path refuses through the same planner caps as the sweep."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    store.save_pin(make_pool_pin())

    over_budget, _, _ = make_lp_executor(pool_pin_store=store)
    with pytest.raises(LpPlanRefusalError) as pool_cap:
        over_budget.plan_mint("FIXc", Decimal("100.01"), MINT_WIDTH_SPACINGS)
    assert pool_cap.value.code.value == "budget_above_pool_cap"

    shallow_pool, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(fast_active_liquidity=10**9), pool_pin_store=store
    )
    with pytest.raises(LpPlanRefusalError) as depth:
        shallow_pool.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)
    assert depth.value.code.value == "position_above_pool_depth_fraction"

    thin_reserve, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(fast_usdc_reserve_units=1_000_000_000),
        pool_pin_store=store,
    )
    with pytest.raises(LpPlanRefusalError) as impact:
        thin_reserve.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)
    assert impact.value.code.value == "swap_impact_above_ceiling"

    broke_safe, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(usdc_balance_units=ONE_USDC_UNITS), pool_pin_store=store
    )
    with pytest.raises(LpPlanRefusalError) as coverage:
        broke_safe.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)
    assert coverage.value.code.value == "insufficient_usdc_for_entry"


def test_known_pool_stale_snapshot_refuses(tmp_path: Path) -> None:
    """A fast-path read older than the staleness bound refuses honestly."""
    store = LpPoolPinStore(tmp_path / "lp_pool_pins.json")
    store.save_pin(make_pool_pin())
    first_call = [True]

    def stepping_clock() -> datetime:
        # The first read stamps the observation fresh; every later read ages.
        if first_call[0]:
            first_call[0] = False
            return BASE_NOW
        return BASE_NOW + timedelta(seconds=300)

    executor, _, _ = make_lp_executor(pool_pin_store=store, now=stepping_clock)
    with pytest.raises(LpExecutionRefusalError) as stale:
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)
    assert stale.value.code is LpExecutionRefusalCode.SNAPSHOT_STALE


# ---------------------------------------------------------------------------
# Execution-layer refusals
# ---------------------------------------------------------------------------


def test_mint_refuses_without_an_explicit_width() -> None:
    """A missing width override refuses with the derived-width catalog code."""
    executor, _, _ = make_lp_executor()

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, None)

    assert raised.value.code is LpExecutionRefusalCode.DERIVED_WIDTH_UNAVAILABLE
    assert "--width-ticks" in str(raised.value)


def test_mint_refuses_live_untracked_positions() -> None:
    """A Safe holding LIVE position NFTs refuses entry fail-closed."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            nfpm_held_positions=2,
            held_token_ids=[900, 901],
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert raised.value.code is LpExecutionRefusalCode.UNTRACKED_EXISTING_POSITIONS
    assert "900" in str(raised.value) and "901" in str(raised.value)


def test_mint_allows_empty_residual_nfts() -> None:
    """Empty residual NFTs carry no exposure and no longer block entry."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            nfpm_held_positions=1,
            held_token_ids=[5_703_026],
            position_words=make_position_words(liquidity=0, fees_owed0=0, fees_owed1=0),
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
    )

    report = executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    assert any("empty residual" in cap for cap in report.caps_enforced)


def test_enumeration_selectors_are_the_keccak_canonical_forms() -> None:
    """The enumeration selector and mint topic are pinned to keccak itself."""
    from eth_utils.crypto import keccak

    assert (
        "0x" + keccak(text="tokenOfOwnerByIndex(address,uint256)").hex()[:8]
        == "0x" + ERC721_TOKEN_OF_OWNER_BY_INDEX_SELECTOR
    )
    assert (
        "0x" + keccak(text="IncreaseLiquidity(uint256,uint128,uint256,uint256)").hex()
        == NFPM_INCREASE_LIQUIDITY_TOPIC0
    )


def test_safe_position_inventory_classifies_live_and_empty() -> None:
    """The inventory snapshot separates live positions from empty residuals."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            nfpm_held_positions=2,
            held_token_ids=[900, 901],
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
        )
    )

    snapshot = executor.safe_position_inventory("FIXc")

    assert [position.token_id for position in snapshot.live_positions] == [900, 901]
    assert snapshot.empty_count == 0

    empty_executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            nfpm_held_positions=1,
            held_token_ids=[5_703_026],
            position_words=make_position_words(liquidity=0, fees_owed0=0, fees_owed1=0),
        )
    )
    empty_snapshot = empty_executor.safe_position_inventory("FIXc")
    assert empty_snapshot.live_positions == ()
    assert empty_snapshot.empty_count == 1


def test_safe_position_inventory_refuses_when_enumeration_reverts() -> None:
    """A mid-enumeration revert refuses fail-closed as unreadable."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(nfpm_held_positions=1, held_token_ids=None)
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.safe_position_inventory("FIXc")

    assert raised.value.code is LpExecutionRefusalCode.ENUMERATION_UNREADABLE


def fixture_expected_exit_out_units(stock_whole: Decimal) -> int:
    """Floor the fixture exit swap's quoted USDC output for whole stock."""
    price = price_usdc_per_stock(LP_SQRT_RATIO, False, STOCK_DECIMALS, 6)
    return int((stock_whole * price * Decimal(10) ** 6).to_integral_value(rounding=ROUND_FLOOR))


def test_exit_swap_dry_run_builds_approval_and_reverse_swap() -> None:
    """The exit swap sells the entire stock balance through the router."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            stock_balance_units=5 * 10**7,
            stock_router_allowance_units=0,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 4),
    )

    report = executor.dry_run_exit_swap("FIXc", bytes(Account.create().key))

    assert report.stock_balance_units == 5 * 10**7
    expected_out = fixture_expected_exit_out_units(Decimal("0.5"))
    assert report.expected_out_units == expected_out
    assert report.amount_out_min_units == int(Decimal(expected_out) * Decimal("0.99"))
    assert [transaction.role for transaction in report.transactions] == [
        LpExecutionRole.STOCK_ROUTER_ALLOWANCE,
        LpExecutionRole.EXIT_SWAP,
    ]
    # The estimate requests carry the built inner calls: the swap's exact
    # input is the entire stock balance and its path runs stock -> USDC, the
    # reverse of every balancing swap.
    inner_calls = [decode_inner(calldata) for calldata in rpc_script.estimate_requests]
    assert "0x" + inner_calls[0].hex() == build_approval_calldata(
        AERODROME_ROUTER_ADDRESS, 5 * 10**7
    )
    commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], inner_calls[1][4:])
    recipient, amount_in, minimum, path, _, _ = decode(
        ["address", "uint256", "uint256", "bytes", "bool", "uint256"], inputs[0]
    )
    assert commands == b"\x00"
    assert deadline == fixture_deadline()
    assert recipient == SAFE_ADDRESS
    assert amount_in == 5 * 10**7
    assert minimum == int(Decimal(expected_out) * Decimal("0.99"))
    assert path == bytes.fromhex(
        build_swap_path(B20_ADDRESS, BASE_USDC_ADDRESS, LP_TICK_SPACING)[2:]
    )


def test_exit_swap_skips_approval_when_allowance_suffices() -> None:
    """A sufficient standing stock allowance collapses to the bare swap."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            stock_balance_units=5 * 10**7,
            stock_router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 2),
    )

    report = executor.dry_run_exit_swap("FIXc", bytes(Account.create().key))

    assert [transaction.role for transaction in report.transactions] == [LpExecutionRole.EXIT_SWAP]
    assert report.router_stock_allowance_units == SATISFIED_ALLOWANCE_UNITS


def test_exit_swap_refuses_a_zero_stock_balance() -> None:
    """Nothing to sell refuses before any preflight or build."""
    executor, _, _ = make_lp_executor(rpc_script=LpRpcScript(stock_balance_units=0))

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_exit_swap("FIXc", bytes(Account.create().key))

    assert raised.value.code is LpExecutionRefusalCode.STOCK_BALANCE_ZERO


def test_exit_swap_refuses_output_above_the_per_pool_cap() -> None:
    """An out-of-band inventory quoting past the pilot cap refuses."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(stock_balance_units=200 * 10**STOCK_DECIMALS)
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_exit_swap("FIXc", bytes(Account.create().key))

    assert raised.value.code is LpExecutionRefusalCode.EXIT_OUTPUT_ABOVE_POOL_CAP


def test_execute_exit_swap_broadcasts_both_steps(tmp_path: Path) -> None:
    """A confirmed exit swap broadcasts approval then swap, audited."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, rpc_script, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=LpRpcScript(
            stock_balance_units=5 * 10**7,
            stock_router_allowance_units=0,
            allow_broadcasts=True,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 4),
    )

    report = executor.execute_exit_swap("FIXc", bytes(Account.create().key), confirm_broadcast=True)

    assert report.completed is True
    assert [step.role for step in report.steps] == [
        LpExecutionRole.STOCK_ROUTER_ALLOWANCE,
        LpExecutionRole.EXIT_SWAP,
    ]
    assert [step.nonce for step in report.steps] == [4, 5]
    assert len(rpc_script.broadcasts) == 2
    records = AuditStore(audit_path).read_records(100)
    assert [record.event_type for record in records] == [
        AuditEventType.LP_EXIT_SWAP_PLANNED,
        AuditEventType.LP_TRANSACTION_BUILT,
        AuditEventType.LP_TRANSACTION_BUILT,
        AuditEventType.LP_EXECUTE_SENT,
        AuditEventType.LP_EXECUTE_CONFIRMED,
        AuditEventType.LP_EXECUTE_SENT,
        AuditEventType.LP_EXECUTE_CONFIRMED,
    ]
    planned = json.loads(records[0].payload_json)
    assert planned["symbol"] == "FIXc"
    assert planned["stock_balance_units"] == 5 * 10**7


def test_execute_exit_swap_refuses_without_confirmation(tmp_path: Path) -> None:
    """The exit swap refuses to broadcast without the explicit flag."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, rpc_script, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=LpRpcScript(stock_balance_units=2 * 10**STOCK_DECIMALS),
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.execute_exit_swap("FIXc", bytes(Account.create().key), confirm_broadcast=False)

    assert raised.value.code is LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING
    assert rpc_script.broadcasts == []
    records = AuditStore(audit_path).read_records(100)
    assert [record.event_type for record in records] == [AuditEventType.LP_REFUSED]


def test_mint_refuses_a_multi_tranche_balancing_swap() -> None:
    """A plan needing tranches refuses before anything is signed."""
    executor, _, _ = make_lp_executor(
        sources=FakeSources(
            discovery=make_discovery((make_candidate(reserve0=TRANCHE_USDC_RESERVE),))
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_mint(
            "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
        )

    assert raised.value.code is LpExecutionRefusalCode.MULTI_TRANCHE_SWAP_UNSUPPORTED


def test_mint_refuses_a_stale_snapshot() -> None:
    """A snapshot older than the staleness bound refuses the attempt."""
    executor, _, _ = make_lp_executor(
        sources=FakeSources(
            discovery=make_discovery(observed_at=datetime(2026, 9, 7, 11, 0, tzinfo=UTC))
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert raised.value.code is LpExecutionRefusalCode.SNAPSHOT_STALE


def test_mint_refuses_an_unknown_symbol() -> None:
    """A symbol outside the official registry refuses before discovery."""
    executor, _, _ = make_lp_executor()

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.plan_mint("NOPEc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)

    assert raised.value.code is LpExecutionRefusalCode.SYMBOL_NOT_IN_REGISTRY


def test_mint_refuses_above_the_gas_cap() -> None:
    """An endpoint gas price above the cap refuses before signing."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(gas_price_wei=2_000_000_000),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_mint(
            "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
        )

    assert raised.value.code is LpExecutionRefusalCode.GAS_PRICE_ABOVE_CAP


def test_mint_refuses_below_the_eth_floor() -> None:
    """A Safe ETH balance below the floor refuses before signing."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(safe_eth_wei=1),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 5),
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_mint(
            "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
        )

    assert raised.value.code is LpExecutionRefusalCode.SAFE_ETH_BELOW_FLOOR


def test_planner_refusals_surface_with_their_own_codes() -> None:
    """A planner cap refusal passes through with its catalog code intact."""
    executor, _, _ = make_lp_executor()

    with pytest.raises(LpPlanRefusalError) as raised:
        executor.plan_mint("FIXc", Decimal("100.01"), MINT_WIDTH_SPACINGS)

    assert "per-pool cap" in str(raised.value)


# ---------------------------------------------------------------------------
# Stake dry-run composition
# ---------------------------------------------------------------------------


def make_position_words(
    *,
    liquidity: int = 12_345,
    fees_owed0: int = 10,
    fees_owed1: int = 11,
    tick_lower: int = LP_RANGE_LOWER,
    tick_upper: int = LP_RANGE_UPPER,
) -> list[bytes]:
    """Encode one coherent twelve-word positions view for the fixture pool.

    Args:
        liquidity: The position's raw L units.
        fees_owed0: Checkpointed token-zero fees awaiting collection.
        fees_owed1: Checkpointed token-one fees awaiting collection.
        tick_lower: The position's inclusive lower tick.
        tick_upper: The position's exclusive upper tick.

    Returns:
        The twelve ABI words the NFPM positions view returns.
    """
    return [
        (0).to_bytes(32, "big"),
        bytes.fromhex(address_word("0x" + "00" * 20)),
        bytes.fromhex(address_word(BASE_USDC_ADDRESS)),
        bytes.fromhex(address_word(B20_ADDRESS)),
        LP_TICK_SPACING.to_bytes(32, "big"),
        signed_word(tick_lower),
        signed_word(tick_upper),
        liquidity.to_bytes(32, "big"),
        (0).to_bytes(32, "big"),
        (0).to_bytes(32, "big"),
        fees_owed0.to_bytes(32, "big"),
        fees_owed1.to_bytes(32, "big"),
    ]


def expected_exit_amounts(liquidity: int = 12_345) -> tuple[Decimal, Decimal]:
    """Compute the fixture position's two-sided amounts at the snapshot price.

    Args:
        liquidity: The position's raw L units.

    Returns:
        The raw token-zero and token-one amounts the full decrease returns.
    """
    return position_amounts_at_sqrt_ratio(
        LP_SQRT_RATIO, LP_RANGE_LOWER, LP_RANGE_UPPER, Decimal(liquidity)
    )


def expected_exit_minima(liquidity: int = 12_345) -> tuple[int, int]:
    """Compute the slippage-floored minima the decrease calldata carries.

    Args:
        liquidity: The position's raw L units.

    Returns:
        The floored token-zero and token-one minima.
    """
    amount0, amount1 = expected_exit_amounts(liquidity)
    tolerance = Decimal(1) - DEFAULT_MINT_SLIPPAGE_TOLERANCE
    return (
        int((amount0 * tolerance).to_integral_value(rounding=ROUND_FLOOR)),
        int((amount1 * tolerance).to_integral_value(rounding=ROUND_FLOOR)),
    )


def test_dry_run_stake_builds_the_pre_mint_sequence() -> None:
    """A not-yet-minted token still proves the machinery with honest labels."""
    executor, rpc_script, _ = make_lp_executor(
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True, True])
    )

    report = executor.dry_run_stake("FIXc", 77, bytes(Account.create().key))

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.NFPM_GAUGE_APPROVAL, LpExecutionRole.GAUGE_DEPOSIT)
    assert [transaction.nonce for transaction in report.transactions] == [6, 7]
    assert all(transaction.signature_verified for transaction in report.transactions)
    assert report.token_owner_address is None
    assert "not minted yet" in report.ownership_diagnostic
    assert report.position is None
    assert "unavailable" in report.position_diagnostic
    assert report.gauge_operator_approved is False
    assert rpc_script.broadcasts == []
    # The approval targets the gauge and the deposit carries the token id.
    approval_inner = decode_inner(rpc_script.estimate_requests[0])
    operator, approved = decode(["address", "bool"], approval_inner[4:])
    assert operator == GAUGE_ADDRESS
    assert approved is True
    deposit_inner = decode_inner(rpc_script.estimate_requests[1])
    (token_id,) = decode(["uint256"], deposit_inner[4:])
    assert token_id == 77


def test_dry_run_stake_on_an_owned_position_skips_the_approval() -> None:
    """An owned, operator-approved position collapses to the bare deposit."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            operator_approved=True,
            position_words=make_position_words(),
        ),
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True]),
    )

    report = executor.dry_run_stake("FIXc", 77, bytes(Account.create().key))

    assert report.token_owner_address == SAFE_ADDRESS
    assert report.ownership_diagnostic == ""
    assert report.gauge_operator_approved is True
    assert report.position is not None
    assert report.position.liquidity == 12_345
    assert report.position.tick_lower == LP_RANGE_LOWER
    assert report.position.tokens_owed0_units == 10
    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.GAUGE_DEPOSIT,)


# ---------------------------------------------------------------------------
# Unstake dry-run composition
# ---------------------------------------------------------------------------


def test_dry_run_unstake_builds_the_withdraw_sequence() -> None:
    """A staked, penalty-clear position builds exactly the gauge withdraw."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=5 * 10**18,
            gauge_rewards_units=45 * 10**17,
        ),
        safe_script=SafeRpcScript(nonce_reads=[9], signature_verdicts=[True]),
    )

    report = executor.dry_run_unstake("FIXc", 77, bytes(Account.create().key))

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.GAUGE_WITHDRAW,)
    assert report.transactions[0].nonce == 9
    assert all(transaction.signature_verified for transaction in report.transactions)
    assert report.accrued_aero_earned_units == 5 * 10**18
    assert report.accrued_aero_checkpoint_units == 45 * 10**17
    deposit_at = int(BASE_NOW.timestamp()) - PENALTY_DEPOSIT_AGE_SECONDS
    assert report.penalty.penalty_rate_bps == PENALTY_RATE_BPS
    assert report.penalty.min_stake_seconds == PENALTY_MIN_STAKE_SECONDS
    assert report.penalty.deposit_timestamp == deposit_at
    assert report.penalty.window_clears_at_timestamp == deposit_at + PENALTY_MIN_STAKE_SECONDS
    assert report.penalty.remaining_seconds == 0
    assert any("penalty window clear" in cap for cap in report.caps_enforced)
    assert rpc_script.broadcasts == []
    assert decode_inner(rpc_script.estimate_requests[0]) == bytes.fromhex(
        build_gauge_withdraw_calldata(77)[2:]
    )


def test_dry_run_unstake_refuses_an_unstaked_position() -> None:
    """A position the Safe itself holds has nothing staked to unstake."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS}, position_words=make_position_words()
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_unstake("FIXc", 77, bytes(Account.create().key))

    assert raised.value.code is LpExecutionRefusalCode.POSITION_NOT_STAKED


def test_dry_run_unstake_refuses_inside_the_penalty_window() -> None:
    """A live early-exit window with accrued emissions refuses the claim."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=10**18,
            deposit_age_seconds=100,
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_unstake("FIXc", 77, bytes(Account.create().key))

    assert raised.value.code is LpExecutionRefusalCode.WITHIN_PENALTY_WINDOW
    clears_at = int(BASE_NOW.timestamp()) - 100 + PENALTY_MIN_STAKE_SECONDS
    assert str(clears_at) in str(raised.value)


def test_dry_run_unstake_proceeds_inside_the_window_when_nothing_accrued() -> None:
    """An open window with zero accrued emissions forfeits nothing."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=0,
            deposit_age_seconds=100,
        ),
        safe_script=SafeRpcScript(nonce_reads=[9], signature_verdicts=[True]),
    )

    report = executor.dry_run_unstake("FIXc", 77, bytes(Account.create().key))

    assert report.penalty.remaining_seconds == 200
    assert tuple(transaction.role for transaction in report.transactions) == (
        LpExecutionRole.GAUGE_WITHDRAW,
    )


def test_dry_run_unstake_refuses_when_penalty_reads_revert() -> None:
    """Unreadable penalty state refuses the unstake rather than guessing."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            penalty_reads_revert=True,
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_unstake("FIXc", 77, bytes(Account.create().key))

    assert raised.value.code is LpExecutionRefusalCode.PENALTY_STATE_UNREADABLE


def test_exit_side_actions_refuse_an_unknown_token() -> None:
    """A token id the NFPM does not know refuses every exit-side action."""
    executor, _, _ = make_lp_executor()

    for action in (
        lambda: executor.dry_run_unstake("FIXc", 77, bytes(Account.create().key)),
        lambda: executor.dry_run_exit("FIXc", 77, bytes(Account.create().key)),
        lambda: executor.dry_run_collect("FIXc", 77, bytes(Account.create().key)),
        lambda: executor.position_status("FIXc", 77, FIXTURE_AERO_PRICE_USDC),
    ):
        with pytest.raises(LpExecutionRefusalError) as raised:
            action()
        assert raised.value.code is LpExecutionRefusalCode.POSITION_UNKNOWN


def test_exit_side_actions_refuse_a_foreign_owner() -> None:
    """A position owned outside the Safe-and-gauge pair refuses management."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: OVERRIDE_SAFE_ADDRESS},
            position_words=make_position_words(),
        )
    )

    for action in (
        lambda: executor.dry_run_unstake("FIXc", 77, bytes(Account.create().key)),
        lambda: executor.dry_run_exit("FIXc", 77, bytes(Account.create().key)),
        lambda: executor.position_status("FIXc", 77, FIXTURE_AERO_PRICE_USDC),
    ):
        with pytest.raises(LpExecutionRefusalError) as raised:
            action()
        assert raised.value.code is LpExecutionRefusalCode.POSITION_NOT_OWNED


# ---------------------------------------------------------------------------
# Withdraw dry-run composition
# ---------------------------------------------------------------------------


def test_dry_run_withdraw_builds_the_decrease_and_collect() -> None:
    """An owned in-range position decreases fully then collects the fees."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS}, position_words=make_position_words()
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True, True]),
    )

    report = executor.dry_run_exit("FIXc", 77, bytes(Account.create().key))

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.NFPM_DECREASE, LpExecutionRole.NFPM_COLLECT)
    assert [transaction.nonce for transaction in report.transactions] == [4, 5]
    assert report.range_state.value == "in_range"
    expected_amount0, expected_amount1 = expected_exit_amounts()
    min0, min1 = expected_exit_minima()
    assert report.amount0_units == expected_amount0
    assert report.amount1_units == expected_amount1
    assert report.amount0_min_units == min0
    assert report.amount1_min_units == min1
    assert report.fees_owed0_units == 10
    assert report.fees_owed1_units == 11
    assert rpc_script.broadcasts == []
    decrease_inner = decode_inner(rpc_script.estimate_requests[0])
    assert decrease_inner == bytes.fromhex(
        build_lp_decrease_liquidity_calldata(
            LpDecreaseLiquidityParams(
                token_id=77,
                liquidity=12_345,
                amount0_min_units=min0,
                amount1_min_units=min1,
                deadline=fixture_deadline(),
            )
        )[2:]
    )
    collect_inner = decode_inner(rpc_script.estimate_requests[1])
    assert collect_inner == bytes.fromhex(
        build_lp_collect_calldata(
            LpCollectParams(
                token_id=77,
                recipient_address=SAFE_ADDRESS,
                amount0_max_units=MAX_UINT128,
                amount1_max_units=MAX_UINT128,
            )
        )[2:]
    )


def test_dry_run_withdraw_refuses_a_staked_position() -> None:
    """The gauge's custody of the NFT blocks every NFPM-side operation."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS}, position_words=make_position_words()
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_exit("FIXc", 77, bytes(Account.create().key))

    assert raised.value.code is LpExecutionRefusalCode.POSITION_STAKED


def test_dry_run_withdraw_refuses_an_emptied_position() -> None:
    """No liquidity and no checkpointed fees leaves nothing to withdraw."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            position_words=make_position_words(liquidity=0, fees_owed0=0, fees_owed1=0),
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_exit("FIXc", 77, bytes(Account.create().key))

    assert raised.value.code is LpExecutionRefusalCode.POSITION_EMPTY


def test_dry_run_withdraw_skips_the_decrease_when_only_fees_remain() -> None:
    """An emptied-but-fee-bearing position collapses to the bare collect."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            position_words=make_position_words(liquidity=0, fees_owed0=5, fees_owed1=7),
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True]),
    )

    report = executor.dry_run_exit("FIXc", 77, bytes(Account.create().key))

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.NFPM_COLLECT,)
    assert report.fees_owed0_units == 5
    assert report.fees_owed1_units == 7
    assert rpc_script.estimate_requests != []


# ---------------------------------------------------------------------------
# Collect dry-run composition
# ---------------------------------------------------------------------------


def test_dry_run_collect_on_a_staked_position_claims_through_the_gauge() -> None:
    """A staked position claims emissions through the per-token getReward."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=25 * 10**17,
            gauge_rewards_units=24 * 10**17,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True]),
    )

    report = executor.dry_run_collect("FIXc", 77, bytes(Account.create().key))

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.GAUGE_GET_REWARD,)
    assert report.staked is True
    assert report.accrued_aero_earned_units == 25 * 10**17
    assert report.accrued_aero_checkpoint_units == 24 * 10**17
    assert report.fees_owed0_units == 0
    assert report.fees_owed1_units == 0
    assert rpc_script.broadcasts == []
    assert decode_inner(rpc_script.estimate_requests[0]) == bytes.fromhex(
        build_gauge_get_reward_calldata(77)[2:]
    )


def test_dry_run_collect_on_an_unstaked_position_sweeps_the_nfpm() -> None:
    """An unstaked position sweeps its checkpointed fees through collect."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS}, position_words=make_position_words()
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True]),
    )

    report = executor.dry_run_collect("FIXc", 77, bytes(Account.create().key))

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (LpExecutionRole.NFPM_COLLECT,)
    assert report.staked is False
    assert report.accrued_aero_earned_units == 0
    assert report.penalty is None
    assert report.fees_owed0_units == 10
    assert report.fees_owed1_units == 11
    assert rpc_script.estimate_requests != []


def test_dry_run_collect_refuses_inside_the_penalty_window() -> None:
    """A staked claim inside a live window with emissions at stake refuses."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=10**18,
            deposit_age_seconds=100,
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_collect("FIXc", 77, bytes(Account.create().key))

    assert raised.value.code is LpExecutionRefusalCode.WITHIN_PENALTY_WINDOW


# ---------------------------------------------------------------------------
# Recenter dry-run composition
# ---------------------------------------------------------------------------


# A liquidity of one times ten to the tenth recycles to roughly five and a
# half USDC, comfortably under every pilot cap while flooring both sides.
RECENTER_LIQUIDITY = 10**10


def expected_recycled_budget() -> Decimal:
    """Compute the budget the default recenter fixture recycles."""
    amount0, amount1 = expected_exit_amounts(RECENTER_LIQUIDITY)
    price = price_usdc_per_stock(LP_SQRT_RATIO, False, STOCK_DECIMALS, 6)
    return +(
        (amount1 + Decimal(11)) * Decimal(10) ** -STOCK_DECIMALS * price
        + (amount0 + Decimal(10)) * Decimal(10) ** -6
    )


def test_dry_run_recenter_staked_builds_the_full_cycle() -> None:
    """A staked position recycles through unstake, exit, burn, and remint."""
    executor, rpc_script, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            operator_approved=True,
        ),
        safe_script=SafeRpcScript(nonce_reads=[2], signature_verdicts=[True] * 5),
    )

    report = executor.dry_run_recenter(
        "FIXc", 77, MINT_WIDTH_SPACINGS, None, bytes(Account.create().key)
    )

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (
        LpExecutionRole.GAUGE_WITHDRAW,
        LpExecutionRole.NFPM_DECREASE,
        LpExecutionRole.NFPM_COLLECT,
        LpExecutionRole.NFPM_BURN,
        LpExecutionRole.MINT,
    )
    assert [transaction.nonce for transaction in report.transactions] == [2, 3, 4, 5, 6]
    assert all(transaction.signature_verified for transaction in report.transactions)
    assert report.staked is True
    assert report.plan.budget_usdc == expected_recycled_budget()
    assert report.plan.balancing_swap.required is False
    assert report.plan.position_range.tick_lower == LP_RANGE_LOWER
    assert report.plan.position_range.tick_upper == LP_RANGE_UPPER
    amount0, amount1 = expected_exit_amounts(RECENTER_LIQUIDITY)
    assert report.projected_usdc_units == int(
        (Decimal(FIXTURE_SAFE_USDC_UNITS) + amount0 + Decimal(10)).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )
    assert report.projected_stock_units == int(
        (amount1 + Decimal(11)).to_integral_value(rounding=ROUND_FLOOR)
    )
    assert "stake command" in report.restake_followup
    assert "no next-id view" in report.restake_followup
    assert rpc_script.broadcasts == []
    inner_calls = [decode_inner(request) for request in rpc_script.estimate_requests]
    min0, min1 = expected_exit_minima(RECENTER_LIQUIDITY)
    assert inner_calls[0] == bytes.fromhex(build_gauge_withdraw_calldata(77)[2:])
    assert inner_calls[1] == bytes.fromhex(
        build_lp_decrease_liquidity_calldata(
            LpDecreaseLiquidityParams(
                token_id=77,
                liquidity=RECENTER_LIQUIDITY,
                amount0_min_units=min0,
                amount1_min_units=min1,
                deadline=fixture_deadline(),
            )
        )[2:]
    )
    assert inner_calls[2] == bytes.fromhex(
        build_lp_collect_calldata(
            LpCollectParams(
                token_id=77,
                recipient_address=SAFE_ADDRESS,
                amount0_max_units=MAX_UINT128,
                amount1_max_units=MAX_UINT128,
            )
        )[2:]
    )
    assert inner_calls[3] == bytes.fromhex(build_lp_burn_calldata(77)[2:])


def test_dry_run_recenter_unstaked_skips_the_withdraw() -> None:
    """An unstaked position recenters through a bare exit, burn, and remint."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            nfpm_held_positions=1,
            held_token_ids=[77],
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            operator_approved=True,
        ),
        safe_script=SafeRpcScript(nonce_reads=[2], signature_verdicts=[True] * 4),
    )

    report = executor.dry_run_recenter(
        "FIXc", 77, MINT_WIDTH_SPACINGS, None, bytes(Account.create().key)
    )

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (
        LpExecutionRole.NFPM_DECREASE,
        LpExecutionRole.NFPM_COLLECT,
        LpExecutionRole.NFPM_BURN,
        LpExecutionRole.MINT,
    )
    assert report.staked is False


def test_dry_run_recenter_with_an_explicit_budget_swaps_the_shortfall() -> None:
    """A larger explicit budget composes the swap and approvals mid-sequence."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
        ),
        safe_script=SafeRpcScript(nonce_reads=[2], signature_verdicts=[True] * 10),
    )

    report = executor.dry_run_recenter(
        "FIXc", 77, MINT_WIDTH_SPACINGS, Decimal("12"), bytes(Account.create().key)
    )

    roles = tuple(transaction.role for transaction in report.transactions)
    assert roles == (
        LpExecutionRole.GAUGE_WITHDRAW,
        LpExecutionRole.NFPM_DECREASE,
        LpExecutionRole.NFPM_COLLECT,
        LpExecutionRole.NFPM_BURN,
        LpExecutionRole.ROUTER_ALLOWANCE,
        LpExecutionRole.BALANCING_SWAP,
        LpExecutionRole.NFPM_USDC_ALLOWANCE,
        LpExecutionRole.NFPM_STOCK_ALLOWANCE,
        LpExecutionRole.MINT,
        LpExecutionRole.NFPM_GAUGE_APPROVAL,
    )
    assert report.plan.budget_usdc == Decimal("12")
    assert report.plan.balancing_swap.required is True
    assert report.plan.balancing_swap.tranche_count == 1


def test_dry_run_recenter_refuses_without_an_explicit_width() -> None:
    """The recenter needs an explicit width until the solver path lands."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            nfpm_held_positions=1,
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_recenter("FIXc", 77, None, None, bytes(Account.create().key))

    assert raised.value.code is LpExecutionRefusalCode.DERIVED_WIDTH_UNAVAILABLE


def test_dry_run_recenter_refuses_extra_live_held_positions() -> None:
    """Live untracked NFTs beside the recentered one refuse the total cap."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            nfpm_held_positions=2,
            held_token_ids=[77, 900],
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
        )
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.dry_run_recenter(
            "FIXc", 77, MINT_WIDTH_SPACINGS, None, bytes(Account.create().key)
        )

    assert raised.value.code is LpExecutionRefusalCode.UNTRACKED_EXISTING_POSITIONS


def test_dry_run_recenter_surfaces_planner_cap_refusals() -> None:
    """A recenter budget above the per-pool cap refuses through the planner."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            nfpm_held_positions=1,
            held_token_ids=[77],
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
        )
    )

    with pytest.raises(LpPlanRefusalError) as raised:
        executor.dry_run_recenter(
            "FIXc", 77, MINT_WIDTH_SPACINGS, Decimal("100.01"), bytes(Account.create().key)
        )

    assert raised.value.code.value == "budget_above_pool_cap"


# ---------------------------------------------------------------------------
# Position status
# ---------------------------------------------------------------------------


def test_position_status_reports_a_staked_position_read_only() -> None:
    """A staked position reports value, emissions, and window without signing."""
    executor, rpc_script, safe_script = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=3 * 10**18,
            gauge_rewards_units=2 * 10**18,
        )
    )

    report = executor.position_status("FIXc", 77, FIXTURE_AERO_PRICE_USDC)

    assert report.token_owner_address == GAUGE_ADDRESS
    assert report.staked is True
    assert report.range_state.value == "in_range"
    assert report.snapshot_block == 123
    amount0, amount1 = expected_exit_amounts()
    assert report.amount0_units == amount0
    assert report.amount1_units == amount1
    price = price_usdc_per_stock(LP_SQRT_RATIO, False, STOCK_DECIMALS, 6)
    assert report.token0_value_usdc == +(amount0 * Decimal(10) ** -6)
    assert report.token1_value_usdc == +(amount1 * Decimal(10) ** -STOCK_DECIMALS * price)
    assert report.position_value_usdc == +(report.token0_value_usdc + report.token1_value_usdc)
    assert report.accrued_aero_earned_units == 3 * 10**18
    assert report.accrued_aero_checkpoint_units == 2 * 10**18
    assert report.penalty is not None
    assert report.penalty.remaining_seconds == 0
    # The pool-level quote is Aerodrome's displayed convention (the shared
    # conversion), including its width-family context line.
    emissions_per_second = 4_494_371_922_759_724
    annual_reward_usd = (
        Decimal(emissions_per_second)
        * FIXTURE_AERO_PRICE_USDC
        * Decimal(31_536_000)
        / Decimal(10) ** 18
    )
    staked_tvl = +(Decimal(250_000_000) * Decimal(10) ** -6 + Decimal(3) * price)
    assert report.quoted_emissions_apr is not None
    assert abs(report.quoted_emissions_apr - (annual_reward_usd / staked_tvl)) < Decimal("1e-20")
    assert "displayed convention" in report.apr_diagnostic
    assert report.unrealized_pnl_usdc is None
    assert "entry cost unknown" in report.pnl_diagnostic
    # Status is read-only: nothing was signed, estimated, or broadcast. The
    # Safe script's queues still hold every answer they were built with.
    assert rpc_script.estimate_requests == []
    assert rpc_script.broadcasts == []
    assert safe_script.nonce_reads == [4]
    assert safe_script.signature_verdicts == [True] * 5


def test_position_status_with_an_entry_cost_reports_the_pnl() -> None:
    """A supplied entry cost basis yields the unrealized P&L."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS}, position_words=make_position_words()
        )
    )

    report = executor.position_status(
        "FIXc", 77, FIXTURE_AERO_PRICE_USDC, entry_cost_usdc=Decimal("0.001")
    )

    assert report.entry_cost_usdc == Decimal("0.001")
    assert report.unrealized_pnl_usdc == +(report.position_value_usdc - Decimal("0.001"))
    assert report.accrued_aero_earned_units is None
    assert report.penalty is None
    assert any("unstaked" in line for line in report.diagnostics)


def test_position_status_quotes_no_apr_without_emissions() -> None:
    """A silent gauge reports no quote rather than a zero APR."""
    executor, _, _ = make_lp_executor(
        sources=FakeSources(
            discovery=make_discovery(
                pools=(make_candidate(emissions_per_second=0, emissions_token_address=None),)
            )
        ),
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS}, position_words=make_position_words()
        ),
    )

    report = executor.position_status("FIXc", 77, FIXTURE_AERO_PRICE_USDC)

    assert report.quoted_emissions_apr is None
    assert "no quoted emissions APR" in report.apr_diagnostic


# ---------------------------------------------------------------------------
# Audit persistence
# ---------------------------------------------------------------------------


def test_dry_run_mint_appends_its_audit_chain(tmp_path: Path) -> None:
    """One dry run appends the plan then every built transaction in order."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(audit_path=audit_path)

    executor.dry_run_mint(
        "FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS, bytes(Account.create().key)
    )

    store = AuditStore(audit_path)
    records = store.read_records(100)
    assert [record.event_type for record in records] == [
        AuditEventType.LP_MINT_PLANNED,
        *([AuditEventType.LP_TRANSACTION_BUILT] * 5),
    ]
    planned = json.loads(records[0].payload_json)
    assert planned["budget_usdc"] == "7"
    assert planned["tick_lower"] == LP_RANGE_LOWER
    assert planned["tick_upper"] == LP_RANGE_UPPER
    assert planned["balancing_swap_required"] is True
    assert planned["snapshot_block"] == 123
    built_roles = [json.loads(record.payload_json)["role"] for record in records[1:]]
    assert built_roles == [
        "router_allowance",
        "balancing_swap",
        "nfpm_usdc_allowance",
        "nfpm_stock_allowance",
        "mint",
    ]
    for record in records[1:]:
        payload = json.loads(record.payload_json)
        assert payload["signature_verified"] is True
        assert payload["mode"] == "dry_run"
    verification = store.verify_chain()
    assert verification.status.value == "verified"
    assert verification.record_count == 6


def test_refusals_append_their_catalog_code(tmp_path: Path) -> None:
    """A refused attempt appends exactly one refusal with its code."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(audit_path=audit_path)

    with pytest.raises(LpExecutionRefusalError):
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, None)

    store = AuditStore(audit_path)
    records = store.read_records(10)
    assert len(records) == 1
    assert records[0].event_type is AuditEventType.LP_REFUSED
    payload = json.loads(records[0].payload_json)
    assert payload["code"] == "derived_width_unavailable"
    assert payload["plan_code"] == ""
    assert payload["action"] == "mint"
    assert payload["symbol"] == "FIXc"


def test_planner_refusals_audit_their_plan_code(tmp_path: Path) -> None:
    """A planner refusal records its own code in the plan_code field."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(audit_path=audit_path)

    with pytest.raises(LpPlanRefusalError):
        executor.plan_mint("FIXc", Decimal("100.01"), MINT_WIDTH_SPACINGS)

    store = AuditStore(audit_path)
    records = store.read_records(10)
    assert len(records) == 1
    payload = json.loads(records[0].payload_json)
    assert payload["code"] == "budget_above_pool_cap"
    assert payload["plan_code"] == "budget_above_pool_cap"


def test_stake_dry_run_appends_its_audit_chain(tmp_path: Path) -> None:
    """One stake dry run appends its plan then both built transactions."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(
        audit_path=audit_path,
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True, True]),
    )

    executor.dry_run_stake("FIXc", 77, bytes(Account.create().key))

    store = AuditStore(audit_path)
    records = store.read_records(100)
    assert [record.event_type for record in records] == [
        AuditEventType.LP_STAKE_PLANNED,
        AuditEventType.LP_TRANSACTION_BUILT,
        AuditEventType.LP_TRANSACTION_BUILT,
    ]
    planned = json.loads(records[0].payload_json)
    assert planned["token_id"] == 77
    assert planned["token_owner_address"] is None
    assert planned["gauge_operator_approved"] is False


def test_unstake_dry_run_appends_its_audit_chain(tmp_path: Path) -> None:
    """One unstake dry run appends its plan then the built withdraw."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=5 * 10**18,
        ),
        safe_script=SafeRpcScript(nonce_reads=[9], signature_verdicts=[True]),
    )

    executor.dry_run_unstake("FIXc", 77, bytes(Account.create().key))

    store = AuditStore(audit_path)
    records = store.read_records(100)
    assert [record.event_type for record in records] == [
        AuditEventType.LP_UNSTAKE_PLANNED,
        AuditEventType.LP_TRANSACTION_BUILT,
    ]
    planned = json.loads(records[0].payload_json)
    assert planned["token_id"] == 77
    assert planned["accrued_aero_earned_units"] == 5 * 10**18
    assert planned["penalty_rate_bps"] == PENALTY_RATE_BPS
    assert planned["penalty_remaining_seconds"] == 0
    assert json.loads(records[1].payload_json)["role"] == "gauge_withdraw"
    assert store.verify_chain().status.value == "verified"


def test_recenter_dry_run_appends_its_audit_chain(tmp_path: Path) -> None:
    """One recenter dry run appends the mint plan, batch plan, then builds."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            operator_approved=True,
        ),
        safe_script=SafeRpcScript(nonce_reads=[2], signature_verdicts=[True] * 5),
    )

    executor.dry_run_recenter("FIXc", 77, MINT_WIDTH_SPACINGS, None, bytes(Account.create().key))

    store = AuditStore(audit_path)
    records = store.read_records(100)
    assert [record.event_type for record in records] == [
        AuditEventType.LP_MINT_PLANNED,
        AuditEventType.LP_RECENTER_PLANNED,
        *([AuditEventType.LP_TRANSACTION_BUILT] * 5),
    ]
    planned = json.loads(records[1].payload_json)
    assert planned["token_id"] == 77
    assert planned["staked"] is True
    assert Decimal(planned["budget_usdc"]) == expected_recycled_budget()
    assert planned["tick_lower"] == LP_RANGE_LOWER
    assert planned["tick_upper"] == LP_RANGE_UPPER
    assert "stake command" in planned["restake_followup"]
    built_roles = [json.loads(record.payload_json)["role"] for record in records[2:]]
    assert built_roles == [
        "gauge_withdraw",
        "nfpm_decrease_liquidity",
        "nfpm_collect",
        "nfpm_burn",
        "mint",
    ]


def test_status_appends_its_read_only_event(tmp_path: Path) -> None:
    """One status observation appends exactly its reported event."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, _, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS}, position_words=make_position_words()
        ),
    )

    executor.position_status("FIXc", 77, FIXTURE_AERO_PRICE_USDC)

    store = AuditStore(audit_path)
    records = store.read_records(100)
    assert [record.event_type for record in records] == [AuditEventType.LP_STATUS_REPORTED]
    payload = json.loads(records[0].payload_json)
    assert payload["symbol"] == "FIXc"
    assert payload["staked"] is True
    assert payload["aero_price_assumption_usdc"] == "1"
    assert payload["quoted_emissions_apr"] is not None


def test_exit_side_refusals_append_their_catalog_codes(tmp_path: Path) -> None:
    """Each exit-side refusal audits its own action and catalog code."""
    audit_path = tmp_path / "audit.sqlite3"
    staked_executor, _, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS}, position_words=make_position_words()
        ),
    )
    unstaked_executor, _, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS}, position_words=make_position_words()
        ),
    )

    with pytest.raises(LpExecutionRefusalError):
        staked_executor.dry_run_exit("FIXc", 77, bytes(Account.create().key))
    with pytest.raises(LpExecutionRefusalError):
        unstaked_executor.dry_run_unstake("FIXc", 77, bytes(Account.create().key))
    with pytest.raises(LpExecutionRefusalError):
        unstaked_executor.position_status("FIXc", 88, FIXTURE_AERO_PRICE_USDC)

    store = AuditStore(audit_path)
    records = store.read_records(100)
    assert [record.event_type for record in records] == [AuditEventType.LP_REFUSED] * 3
    pairs = [
        (json.loads(record.payload_json)["action"], json.loads(record.payload_json)["code"])
        for record in records
    ]
    assert pairs == [
        ("withdraw", "position_staked"),
        ("unstake", "position_not_staked"),
        ("status", "position_unknown"),
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def make_cli_settings(tmp_path: Path) -> SimpleNamespace:
    """Build the settings-shaped object the LP CLI reads."""
    return SimpleNamespace(
        base_rpc_url="https://fixture.example",
        lp_sugar_address="0x27fc745390d1f4baf8d184fbd97748340f786634",
        audit_database_path=tmp_path / "audit.sqlite3",
        lp_pool_pins_path=tmp_path / "lp_pool_pins.json",
    )


def test_cli_plan_mint_prints_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The plan subcommand prices the mint through the wired executor."""
    executor, _, _ = make_lp_executor()
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "plan",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
            ]
        )

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert f"[{LP_RANGE_LOWER}, {LP_RANGE_UPPER})" in output
    assert "balancing swap" in output


def test_cli_dry_run_mint_prints_the_json_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dry-run subcommand prints its complete typed model with --json."""
    executor, _, _ = make_lp_executor()
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
                "--ephemeral-key",
                "--json",
            ]
        )

    assert exit_code == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "dry_run"
    assert printed["ephemeral_key"] is True
    assert [t["role"] for t in printed["transactions"]][0] == "router_allowance"


def test_cli_dry_run_stake_prints_the_pre_mint_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The stake dry-run reports the unminted token honestly."""
    executor, _, _ = make_lp_executor(
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True, True])
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "stake",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "not minted yet" in output
    assert "[nfpm_gauge_approval]" in output
    assert "[gauge_deposit]" in output


def test_cli_dry_run_unstake_prints_the_penalty_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The unstake dry-run prints the window state and the withdraw step."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=5 * 10**18,
        ),
        safe_script=SafeRpcScript(nonce_reads=[9], signature_verdicts=[True]),
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "unstake",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "penalty window clear" in output
    assert "[gauge_withdraw]" in output
    assert "auto-claims the accrued emissions" in output


def test_cli_dry_run_withdraw_prints_the_json_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The withdraw dry-run prints its complete typed model with --json."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS}, position_words=make_position_words()
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True, True]),
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "withdraw",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--ephemeral-key",
                "--json",
            ]
        )

    assert exit_code == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "dry_run"
    assert [t["role"] for t in printed["transactions"]] == [
        "nfpm_decrease_liquidity",
        "nfpm_collect",
    ]
    assert printed["range_state"] == "in_range"


def test_cli_dry_run_collect_prints_the_claim_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The collect dry-run names the gauge claim path for a staked token."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=25 * 10**17,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True]),
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "collect",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "claim path gauge getReward" in output
    assert "[gauge_get_reward]" in output


def test_cli_dry_run_recenter_prints_the_restake_followup(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The recenter dry-run prints the batch and its restake follow-up."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            operator_approved=True,
        ),
        safe_script=SafeRpcScript(nonce_reads=[2], signature_verdicts=[True] * 5),
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "recenter",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--width-ticks",
                "1",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "[gauge_withdraw]" in output
    assert "[nfpm_burn]" in output
    assert "[mint]" in output
    assert "restake follow-up: the restake completes by running the stake command" in output


def test_cli_status_prints_the_pre_fix_apr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The status command quotes the pre-fix APR without any key material."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
            gauge_earned_units=3 * 10**18,
        )
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "status",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--aero-price",
                "1",
                "--entry-cost",
                "0.001",
            ]
        )

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "quoted emissions APR" in output
    assert "displayed convention" in output
    assert "unrealized P&L" in output
    assert "staked in the gauge" in output


def test_cli_unstake_refusal_exits_two(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unknown token on the unstake path exits two with its code."""
    executor, _, _ = make_lp_executor()
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "unstake",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_REFUSED
    assert "refused [position_unknown]" in capsys.readouterr().err


def test_cli_refusal_exits_two_with_the_catalog_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused dry run exits two and prints the catalog code."""
    executor, _, _ = make_lp_executor()
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            ["dry-run", "mint", "--symbol", "FIXc", "--amount", "7", "--ephemeral-key"]
        )

    assert exit_code == EXIT_REFUSED
    assert "refused [derived_width_unavailable]" in capsys.readouterr().err


def test_cli_fails_when_the_audit_store_is_unavailable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unavailable audit store exits one before any executor wiring."""
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore", side_effect=OSError("disk full")),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor") as executor_factory,
    ):
        exit_code = main(
            ["plan", "mint", "--symbol", "FIXc", "--amount", "7", "--width-ticks", "1"]
        )

    assert exit_code == EXIT_FAILURE
    assert "audit store is unavailable" in capsys.readouterr().err
    executor_factory.assert_not_called()


def test_cli_threads_the_environment_safe_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor and Safe backend wire to the environment's Safe."""
    executor, _, _ = make_lp_executor(
        safe_script=SafeRpcScript(nonce_reads=[6], signature_verdicts=[True, True])
    )
    monkeypatch.setenv(SAFE_ADDRESS_ENV, OVERRIDE_SAFE_ADDRESS)
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend") as safe_backend_factory,
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor) as factory,
    ):
        exit_code = main(
            [
                "dry-run",
                "stake",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_OK
    assert factory.call_args.kwargs["safe_address"] == OVERRIDE_SAFE_ADDRESS
    assert safe_backend_factory.call_args.kwargs["safe_address"] == OVERRIDE_SAFE_ADDRESS


def test_cli_never_attempts_a_broadcast_under_any_subcommand(
    tmp_path: Path,
) -> None:
    """Every wired subcommand path runs over a transport that fails on sends."""
    staked_executor, staked_rpc, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
            gauge_earned_units=5 * 10**18,
            router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_usdc_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            nfpm_stock_allowance_units=SATISFIED_ALLOWANCE_UNITS,
            operator_approved=True,
        ),
        # One nonce read per dry run: mint, stake, unstake, collect, recenter;
        # plan and status never reach the preflight chain.
        safe_script=SafeRpcScript(
            nonce_reads=[4, 6, 7, 8, 9],
            signature_verdicts=[True] * (5 + 2 + 1 + 1 + 5),
        ),
    )
    owned_executor, owned_rpc, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            nfpm_held_positions=1,
            position_words=make_position_words(liquidity=RECENTER_LIQUIDITY),
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True, True]),
    )
    for executor, arguments in (
        (
            staked_executor,
            ["plan", "mint", "--symbol", "FIXc", "--amount", "7", "--width-ticks", "1"],
        ),
        (
            staked_executor,
            [
                "dry-run",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
                "--ephemeral-key",
            ],
        ),
        (
            staked_executor,
            ["dry-run", "stake", "--symbol", "FIXc", "--token-id", "77", "--ephemeral-key"],
        ),
        (
            staked_executor,
            ["dry-run", "unstake", "--symbol", "FIXc", "--token-id", "77", "--ephemeral-key"],
        ),
        (
            owned_executor,
            ["dry-run", "withdraw", "--symbol", "FIXc", "--token-id", "77", "--ephemeral-key"],
        ),
        (
            staked_executor,
            ["dry-run", "collect", "--symbol", "FIXc", "--token-id", "77", "--ephemeral-key"],
        ),
        (
            staked_executor,
            [
                "dry-run",
                "recenter",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--width-ticks",
                "1",
                "--ephemeral-key",
            ],
        ),
        (
            staked_executor,
            ["status", "--symbol", "FIXc", "--token-id", "77", "--aero-price", "1"],
        ),
    ):
        with (
            patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
            patch("aero_bot.lp_executor.AuditStore"),
            patch("aero_bot.lp_executor.LiveExecutionSources"),
            patch("aero_bot.lp_executor.ExecutorRpcBackend"),
            patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
            patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
        ):
            assert main(arguments) == EXIT_OK
    assert staked_rpc.broadcasts == []
    assert owned_rpc.broadcasts == []


def test_malformed_reads_surface_as_unavailability() -> None:
    """A malformed on-chain read raises an unavailable failure, not a refusal."""
    executor, _, _ = make_lp_executor(rpc_script=LpRpcScript(nfpm_balance_hex="0x1234"))

    with pytest.raises(ExecutionUnavailableError):
        executor.plan_mint("FIXc", MINT_BUDGET_USDC, MINT_WIDTH_SPACINGS)


def test_cli_unavailable_reads_exit_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An unavailable read surfaces as a plain failure exit, not a refusal."""
    executor, _, _ = make_lp_executor(rpc_script=LpRpcScript(nfpm_balance_hex="0x1234"))
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "dry-run",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_FAILURE
    assert "failed:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Execute path (broadcast behind the explicit confirmation flag)
# ---------------------------------------------------------------------------


def test_execute_mint_refuses_without_broadcast_confirmation(tmp_path: Path) -> None:
    """Without the explicit flag the execute path refuses before building."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, rpc_script, _ = make_lp_executor(audit_path=audit_path)

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.execute_mint(
            "FIXc",
            MINT_BUDGET_USDC,
            MINT_WIDTH_SPACINGS,
            bytes(Account.create().key),
            confirm_broadcast=False,
        )

    assert raised.value.code is LpExecutionRefusalCode.BROADCAST_CONFIRMATION_MISSING
    assert rpc_script.broadcasts == []
    records = AuditStore(audit_path).read_records(10)
    assert [record.event_type for record in records] == [AuditEventType.LP_REFUSED]
    refusal = json.loads(records[0].payload_json)
    assert refusal["code"] == "broadcast_confirmation_missing"
    assert refusal["mode"] == "execute"
    assert refusal["action"] == "mint"


def test_execute_mint_broadcasts_every_step_in_nonce_order(tmp_path: Path) -> None:
    """A confirmed execute broadcasts one delivery per Safe nonce, audited."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, rpc_script, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=LpRpcScript(allow_broadcasts=True),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 10),
    )

    report = executor.execute_mint(
        "FIXc",
        MINT_BUDGET_USDC,
        MINT_WIDTH_SPACINGS,
        bytes(Account.create().key),
        confirm_broadcast=True,
    )

    assert report.completed is True
    assert report.halted_reason == ""
    assert [step.role for step in report.steps] == [
        LpExecutionRole.ROUTER_ALLOWANCE,
        LpExecutionRole.BALANCING_SWAP,
        LpExecutionRole.NFPM_USDC_ALLOWANCE,
        LpExecutionRole.NFPM_STOCK_ALLOWANCE,
        LpExecutionRole.MINT,
    ]
    assert [step.nonce for step in report.steps] == [4, 5, 6, 7, 8]
    assert [step.relayer_nonce for step in report.steps] == [3, 4, 5, 6, 7]
    assert all(step.status == "confirmed" for step in report.steps)
    assert all(step.fee_wei == 80_000 * FIXTURE_GAS_PRICE_WEI for step in report.steps)
    # The delivery gas limit buffers each fresh estimate by a fifth.
    for step, estimate in zip(
        report.steps, (60_000, 200_000, 60_000, 60_000, 400_000), strict=True
    ):
        assert step.delivery_gas_limit == int(estimate * 1.2)
    assert len(rpc_script.broadcasts) == 5

    records = AuditStore(audit_path).read_records(100)
    assert [record.event_type for record in records] == [
        AuditEventType.LP_MINT_PLANNED,
        *([AuditEventType.LP_TRANSACTION_BUILT] * 5),
        *([AuditEventType.LP_EXECUTE_SENT, AuditEventType.LP_EXECUTE_CONFIRMED] * 5),
    ]
    planned = json.loads(records[0].payload_json)
    assert planned["mode"] == "execute"
    first_sent = json.loads(records[6].payload_json)
    assert first_sent["action"] == "mint"
    assert first_sent["role"] == "router_allowance"
    assert first_sent["nonce"] == 4
    first_receipt = json.loads(records[7].payload_json)
    assert first_receipt["outcome"] == "confirmed"
    assert first_receipt["gas_used"] == 80_000
    assert AuditStore(audit_path).verify_chain().status.value == "verified"


def make_execute_stake_executor(
    audit_path: Path | None = None,
    *,
    script_kwargs: dict[str, object] | None = None,
    **executor_kwargs: object,
) -> tuple[LpLifecycleExecutor, LpRpcScript]:
    """Assemble a two-step stake executor over a broadcast-serving script."""
    script = LpRpcScript(
        owner_addresses={77: SAFE_ADDRESS},
        position_words=make_position_words(),
        allow_broadcasts=True,
        **(script_kwargs or {}),  # type: ignore[arg-type]
    )
    executor, _, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=script,
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 4),
        **executor_kwargs,  # type: ignore[arg-type]
    )
    return executor, script


def test_execute_halts_at_an_execute_time_estimate_revert(tmp_path: Path) -> None:
    """A fresh-estimate revert stops the sequence at the completed prefix."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, rpc_script = make_execute_stake_executor(
        audit_path=audit_path, script_kwargs={"estimate_reverts_after": 3}
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.execute_stake("FIXc", 77, bytes(Account.create().key), confirm_broadcast=True)

    assert raised.value.code is LpExecutionRefusalCode.ESTIMATE_REVERTED
    assert "stopping honestly" in str(raised.value)
    assert "PSC" in str(raised.value)
    # Two build estimates and one execute estimate succeeded; the second
    # execute estimate reverted, so exactly one step broadcast.
    assert len(rpc_script.broadcasts) == 1
    records = AuditStore(audit_path).read_records(100)
    assert [record.event_type for record in records] == [
        AuditEventType.LP_STAKE_PLANNED,
        AuditEventType.LP_TRANSACTION_BUILT,
        AuditEventType.LP_TRANSACTION_BUILT,
        AuditEventType.LP_EXECUTE_SENT,
        AuditEventType.LP_EXECUTE_CONFIRMED,
        AuditEventType.LP_REFUSED,
    ]
    refusal = json.loads(records[-1].payload_json)
    assert refusal["code"] == "estimate_reverted"
    assert refusal["mode"] == "execute"


def test_execute_refuses_when_the_relayer_floor_is_short(tmp_path: Path) -> None:
    """A relayer below the floor never reaches a broadcast."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, rpc_script = make_execute_stake_executor(
        audit_path=audit_path, script_kwargs={"relayer_eth_wei": 1}
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.execute_stake("FIXc", 77, bytes(Account.create().key), confirm_broadcast=True)

    assert raised.value.code is LpExecutionRefusalCode.RELAYER_ETH_INSUFFICIENT
    assert rpc_script.broadcasts == []


def test_execute_refuses_a_signature_rejected_at_execute_time(tmp_path: Path) -> None:
    """A live signature rejection at execute time halts before broadcasting."""
    audit_path = tmp_path / "audit.sqlite3"
    script = LpRpcScript(
        owner_addresses={77: SAFE_ADDRESS},
        position_words=make_position_words(),
        allow_broadcasts=True,
    )
    executor, _, _ = make_lp_executor(
        audit_path=audit_path,
        rpc_script=script,
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True, True, True, False]),
    )

    with pytest.raises(LpExecutionRefusalError) as raised:
        executor.execute_stake("FIXc", 77, bytes(Account.create().key), confirm_broadcast=True)

    assert raised.value.code is LpExecutionRefusalCode.SIGNATURE_REJECTED
    # The first step broadcast and confirmed; the second refused pre-send.
    assert len(script.broadcasts) == 1


def test_execute_reports_a_failed_delivery_and_halts(tmp_path: Path) -> None:
    """An on-chain revert marks the step failed and stops the sequence."""
    audit_path = tmp_path / "audit.sqlite3"
    executor, rpc_script = make_execute_stake_executor(
        audit_path=audit_path, script_kwargs={"receipt_status": 0}
    )

    report = executor.execute_stake("FIXc", 77, bytes(Account.create().key), confirm_broadcast=True)

    assert report.completed is False
    assert report.steps[0].status == "failed"
    assert len(report.steps) == 1
    assert "reverted on-chain" in report.steps[0].diagnostic
    assert len(rpc_script.broadcasts) == 1
    records = AuditStore(audit_path).read_records(100)
    assert records[4].event_type is AuditEventType.LP_EXECUTE_FAILED
    failed = json.loads(records[4].payload_json)
    assert failed["outcome"] == "failed"


def test_execute_reports_an_unconfirmed_delivery_as_a_warning(tmp_path: Path) -> None:
    """An exhausted receipt wait is a warning, never a failure relabel."""
    audit_path = tmp_path / "audit.sqlite3"
    clock_values = iter(float(value) for value in range(0, 10**7, 10_000))
    executor, rpc_script = make_execute_stake_executor(
        audit_path=audit_path,
        script_kwargs={"receipt_present": False},
        timer=lambda: next(clock_values),
    )

    report = executor.execute_stake("FIXc", 77, bytes(Account.create().key), confirm_broadcast=True)

    assert report.completed is False
    assert report.steps[0].status == "unconfirmed"
    assert "may still land" in report.steps[0].diagnostic
    assert len(rpc_script.broadcasts) == 1
    records = AuditStore(audit_path).read_records(100)
    # The send record exists; no confirmed or failed record was invented.
    assert records[3].event_type is AuditEventType.LP_EXECUTE_SENT
    assert not any(
        record.event_type in (AuditEventType.LP_EXECUTE_CONFIRMED, AuditEventType.LP_EXECUTE_FAILED)
        for record in records
    )


def test_execute_rotates_receipt_polling_across_backends() -> None:
    """A receipt found on the second endpoint completes the delivery."""
    primary = LpRpcScript(
        owner_addresses={77: SAFE_ADDRESS},
        position_words=make_position_words(),
        allow_broadcasts=True,
        receipt_present=False,
    )
    secondary = LpRpcScript(receipt_present=True)
    executor, _, _ = make_lp_executor(
        rpc_script=primary,
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 4),
        receipt_script=secondary,
    )

    report = executor.execute_stake("FIXc", 77, bytes(Account.create().key), confirm_broadcast=True)

    assert report.completed is True
    assert all(step.status == "confirmed" for step in report.steps)


def test_execute_re_reads_a_lagging_gs026_estimate() -> None:
    """A transient endpoint-lag GS026 earns bounded fresh re-reads."""
    sleeps: list[float] = []
    executor, rpc_script = make_execute_stake_executor(
        script_kwargs={"estimate_gs026_lag_calls": 2, "estimate_gs026_lag_from": 3},
        sleep=sleeps.append,
    )

    report = executor.execute_stake("FIXc", 77, bytes(Account.create().key), confirm_broadcast=True)

    assert report.completed is True
    assert sleeps == [4.0, 4.0]
    assert len(rpc_script.broadcasts) == 2


def test_cli_execute_refuses_without_the_confirmation_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The execute CLI refuses with exit two unless the flag is present."""
    executor, _, _ = make_lp_executor()
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "execute",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
                "--ephemeral-key",
            ]
        )

    assert exit_code == EXIT_REFUSED
    assert "refused [broadcast_confirmation_missing]" in capsys.readouterr().err


def test_cli_dry_run_exit_swap_prints_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The dry-run exit-swap CLI path builds and prints its report."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            stock_balance_units=5 * 10**7,
            stock_router_allowance_units=SATISFIED_ALLOWANCE_UNITS,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 2),
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(["dry-run", "exit-swap", "--symbol", "FIXc", "--ephemeral-key"])

    assert exit_code == EXIT_OK
    output = capsys.readouterr().out
    assert "nothing broadcast" in output
    assert "selling the entire" in output


def test_cli_execute_exit_swap_refuses_without_the_confirmation_flag(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The execute exit-swap CLI refuses with exit two unless flagged."""
    executor, _, _ = make_lp_executor(rpc_script=LpRpcScript(stock_balance_units=5 * 10**7))
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(["execute", "exit-swap", "--symbol", "FIXc", "--ephemeral-key"])

    assert exit_code == EXIT_REFUSED
    assert "refused [broadcast_confirmation_missing]" in capsys.readouterr().err


def test_cli_execute_mint_broadcasts_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A confirmed execute CLI run prints every broadcast hash and exits zero."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(allow_broadcasts=True),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 10),
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "execute",
                "mint",
                "--symbol",
                "FIXc",
                "--amount",
                "7",
                "--width-ticks",
                "1",
                "--ephemeral-key",
                "--confirm-broadcast",
                "--json",
            ]
        )

    assert exit_code == EXIT_OK
    printed = json.loads(capsys.readouterr().out)
    assert printed["mode"] == "execute"
    assert printed["completed"] is True
    assert [step["status"] for step in printed["steps"]] == ["confirmed"] * 5


def test_cli_execute_with_a_failed_delivery_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A delivery that reverts on-chain exits one after printing the failure."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            owner_addresses={77: SAFE_ADDRESS},
            position_words=make_position_words(),
            allow_broadcasts=True,
            receipt_status=0,
        ),
        safe_script=SafeRpcScript(nonce_reads=[4], signature_verdicts=[True] * 4),
    )
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
    ):
        exit_code = main(
            [
                "execute",
                "stake",
                "--symbol",
                "FIXc",
                "--token-id",
                "77",
                "--ephemeral-key",
                "--confirm-broadcast",
            ]
        )

    assert exit_code == EXIT_FAILURE
    output = capsys.readouterr().out
    assert "failed" in output
    assert "halted:" in output


def test_cli_full_discovery_flag_bypasses_the_pin_store(tmp_path: Path) -> None:
    """--full-discovery skips pin construction; normal runs construct it."""
    executor, _, _ = make_lp_executor()
    mint_arguments = [
        "plan",
        "mint",
        "--symbol",
        "FIXc",
        "--amount",
        "7",
        "--width-ticks",
        "1",
    ]
    with (
        patch("aero_bot.lp_executor.Settings", return_value=make_cli_settings(tmp_path)),
        patch("aero_bot.lp_executor.AuditStore"),
        patch("aero_bot.lp_executor.LiveExecutionSources"),
        patch("aero_bot.lp_executor.ExecutorRpcBackend"),
        patch("aero_bot.lp_executor.SafeTransactionRpcBackend"),
        patch("aero_bot.lp_executor.LpLifecycleExecutor", return_value=executor),
        patch("aero_bot.lp_executor.LpPoolPinStore") as pin_store_factory,
    ):
        assert main(["--full-discovery", *mint_arguments]) == EXIT_OK
        pin_store_factory.assert_not_called()

        assert main(mint_arguments) == EXIT_OK
        pin_store_factory.assert_called_once_with(make_cli_settings(tmp_path).lp_pool_pins_path)


def test_position_status_reads_the_aero_price_live_by_default() -> None:
    """Without an override the AERO price comes from the live pair read."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            aero_price_usdc=Decimal("0.75"),
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
        )
    )

    report = executor.position_status("FIXc", 77)

    assert report.aero_price_assumption_usdc == Decimal("0.75")
    assert report.quoted_emissions_apr is not None
    # The quote uses the live price: one AERO per second over the staked value
    # priced at 0.75 rather than an operator assumption.
    assert "AERO price 0.75" in report.apr_diagnostic


def test_position_status_fails_closed_when_the_live_aero_price_is_unreadable() -> None:
    """An unreadable live price refuses with the dedicated code."""
    executor, _, _ = make_lp_executor(
        rpc_script=LpRpcScript(
            aero_price_usdc=None,
            owner_addresses={77: GAUGE_ADDRESS},
            position_words=make_position_words(),
        )
    )

    with pytest.raises(LpExecutionRefusalError) as caught:
        executor.position_status("FIXc", 77)
    assert caught.value.code is LpExecutionRefusalCode.AERO_PRICE_UNREADABLE
