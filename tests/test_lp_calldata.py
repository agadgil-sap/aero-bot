"""Behavior tests for the Slipstream LP lifecycle calldata layer."""

import pytest

from aero_bot.lp_calldata import (
    GAUGE_DEPOSIT_SELECTOR,
    GAUGE_EARNED_SELECTOR,
    GAUGE_GET_REWARD_SELECTOR,
    GAUGE_REWARDS_SELECTOR,
    GAUGE_WITHDRAW_SELECTOR,
    LP_COLLECT_SELECTOR,
    LP_DECREASE_LIQUIDITY_SELECTOR,
    LP_MINT_SELECTOR,
    LP_POSITIONS_SELECTOR,
    MAX_UINT128,
    LpCollectParams,
    LpDecreaseLiquidityParams,
    LpMintParams,
    build_gauge_deposit_calldata,
    build_gauge_earned_read_calldata,
    build_gauge_get_reward_calldata,
    build_gauge_rewards_read_calldata,
    build_gauge_withdraw_calldata,
    build_lp_burn_calldata,
    build_lp_collect_calldata,
    build_lp_decrease_liquidity_calldata,
    build_lp_mint_calldata,
    build_lp_positions_read_calldata,
    build_set_approval_for_all_calldata,
    decode_lp_positions_view,
)

# The canary Safe that would own every position in the fixed vectors.
SAFE_ADDRESS = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
# Native Base USDC, the token-zero side of the target AAPLc pool.
USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
# The Coinbase-issued AAPLc stock token, the pool's token-one side.
AAPLC_ADDRESS = "0xb200000000000000000000c2e324d24d7eecd1fb"
# The Gauges V3 Slipstream NFPM the AAPLc pool's Sugar record names; live
# verified as name() "Slipstream Position NFT v1", symbol() "AERO-CL-POS".
NFPM_ADDRESS = "0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53"
# The AAPLc/USDC pool's live CLGauge.
GAUGE_ADDRESS = "0x43021fbbd01b967704ab2379f6e90e2d367042f3"
# A real, gauge-staked AAPLc/USDC position observed live on 2026-09-08.
STAKED_TOKEN_ID = 5660106
# A fixed future timestamp shared by every deadline-bearing vector.
FIXED_DEADLINE = 1788800000
# One USDC in raw six-decimal units.
ONE_USDC_UNITS = 1_000_000


def mint_params() -> LpMintParams:
    """Build the fixed one-USDC, one-spacing mint used by the vectors."""
    return LpMintParams(
        token0_address=USDC_ADDRESS,
        token1_address=AAPLC_ADDRESS,
        tick_spacing=10,
        tick_lower=-11650,
        tick_upper=-11640,
        amount0_desired_units=ONE_USDC_UNITS,
        amount1_desired_units=3110,
        amount0_min_units=990_000,
        amount1_min_units=3080,
        recipient_address=SAFE_ADDRESS,
        deadline=FIXED_DEADLINE,
    )


# The complete canonical vectors below were produced by
# `cast calldata "<signature>" "<args>"` with foundry cast 1.8.1; the builders
# must reproduce them byte for byte.
CANONICAL_MINT_CALLDATA = (
    "0xb5007d1f000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    "000000000000000000000000b200000000000000000000c2e324d24d7eecd1fb"
    "000000000000000000000000000000000000000000000000000000000000000a"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffd27e"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffd288"
    "00000000000000000000000000000000000000000000000000000000000f4240"
    "0000000000000000000000000000000000000000000000000000000000000c26"
    "00000000000000000000000000000000000000000000000000000000000f1b30"
    "0000000000000000000000000000000000000000000000000000000000000c08"
    "000000000000000000000000b69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
    "000000000000000000000000000000000000000000000000000000006a9eec00"
    "0000000000000000000000000000000000000000000000000000000000000000"
)
CANONICAL_DECREASE_CALLDATA = (
    "0x0c49ccbe"
    + "0000000000000000000000000000000000000000000000000000000000565dca"
    + "0000000000000000000000000000000000000000000000000000001f7180251c"
    + "0000000000000000000000000000000000000000000000000000000000000000"
    + "0000000000000000000000000000000000000000000000000000000000000000"
    + "000000000000000000000000000000000000000000000000000000006a9eec00"
)
CANONICAL_COLLECT_CALLDATA = (
    "0xfc6f7865"
    + "0000000000000000000000000000000000000000000000000000000000565dca"
    + "000000000000000000000000b69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
    + "00000000000000000000000000000000ffffffffffffffffffffffffffffffff"
    + "00000000000000000000000000000000ffffffffffffffffffffffffffffffff"
)
CANONICAL_SET_APPROVAL_CALLDATA = (
    "0xa22cb46500000000000000000000000043021fbbd01b967704ab2379f6e90e2d367042f3"
    "0000000000000000000000000000000000000000000000000000000000000001"
)
CANONICAL_BURN_CALLDATA = (
    "0x42966c680000000000000000000000000000000000000000000000000000000000565dca"
)
CANONICAL_POSITIONS_READ_CALLDATA = (
    "0x99fbab880000000000000000000000000000000000000000000000000000000000565dca"
)
CANONICAL_DEPOSIT_CALLDATA = (
    "0xb6b55f250000000000000000000000000000000000000000000000000000000000565dca"
)
CANONICAL_WITHDRAW_CALLDATA = (
    "0x2e1a7d4d0000000000000000000000000000000000000000000000000000000000565dca"
)
CANONICAL_GET_REWARD_CALLDATA = (
    "0x1c4b774b0000000000000000000000000000000000000000000000000000000000565dca"
)
CANONICAL_EARNED_READ_CALLDATA = (
    "0x3e491d47000000000000000000000000b69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
    "0000000000000000000000000000000000000000000000000000000000565dca"
)
CANONICAL_REWARDS_READ_CALLDATA = (
    "0xf301af420000000000000000000000000000000000000000000000000000000000565dca"
)

# The live positions() return for the staked AAPLc/USDC token 5660106, fetched
# read-only on 2026-09-08; the decoder must reproduce every field exactly.
LIVE_POSITIONS_RETURN = (
    "0x0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    "000000000000000000000000b200000000000000000000c2e324d24d7eecd1fb"
    "000000000000000000000000000000000000000000000000000000000000000a"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffd260"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffd27e"
    "0000000000000000000000000000000000000000000000000000001f7180251c"
    "fffffffffffffffffffffffffffffffffff600a79238cbfde57dc3497959648b"
    "fffffffffffffffffffffffffffffffffffd10dd1cf285d0aa626b18ec088d49"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
)


def test_mint_calldata_matches_cast_canonical_vector() -> None:
    """The twelve-field mint encoding matches cast byte for byte."""
    assert build_lp_mint_calldata(mint_params()) == CANONICAL_MINT_CALLDATA
    assert CANONICAL_MINT_CALLDATA.startswith(f"0x{LP_MINT_SELECTOR}")


def test_decrease_liquidity_calldata_matches_cast_canonical_vector() -> None:
    """The decreaseLiquidity struct encoding matches cast byte for byte."""
    params = LpDecreaseLiquidityParams(
        token_id=STAKED_TOKEN_ID,
        liquidity=135_048_209_692,
        amount0_min_units=0,
        amount1_min_units=0,
        deadline=FIXED_DEADLINE,
    )
    assert build_lp_decrease_liquidity_calldata(params) == CANONICAL_DECREASE_CALLDATA
    assert CANONICAL_DECREASE_CALLDATA.startswith(f"0x{LP_DECREASE_LIQUIDITY_SELECTOR}")


def test_collect_calldata_matches_cast_canonical_vector() -> None:
    """The collect-everything encoding matches cast byte for byte."""
    params = LpCollectParams(
        token_id=STAKED_TOKEN_ID,
        recipient_address=SAFE_ADDRESS,
        amount0_max_units=MAX_UINT128,
        amount1_max_units=MAX_UINT128,
    )
    assert build_lp_collect_calldata(params) == CANONICAL_COLLECT_CALLDATA
    assert CANONICAL_COLLECT_CALLDATA.startswith(f"0x{LP_COLLECT_SELECTOR}")


def test_set_approval_for_all_calldata_matches_cast_canonical_vector() -> None:
    """The ERC721 gauge approval matches cast byte for byte."""
    approval = build_set_approval_for_all_calldata(GAUGE_ADDRESS, True)
    assert approval == CANONICAL_SET_APPROVAL_CALLDATA
    assert build_set_approval_for_all_calldata(GAUGE_ADDRESS, False).endswith("0" * 64)


def test_burn_calldata_matches_cast_canonical_vector() -> None:
    """The position burn encoding matches cast byte for byte."""
    assert build_lp_burn_calldata(STAKED_TOKEN_ID) == CANONICAL_BURN_CALLDATA


def test_positions_read_calldata_matches_cast_canonical_vector() -> None:
    """The positions view encoding matches cast byte for byte."""
    assert build_lp_positions_read_calldata(STAKED_TOKEN_ID) == CANONICAL_POSITIONS_READ_CALLDATA
    assert CANONICAL_POSITIONS_READ_CALLDATA.startswith(f"0x{LP_POSITIONS_SELECTOR}")


def test_gauge_lifecycle_calldata_matches_cast_canonical_vectors() -> None:
    """Deposit, withdraw, and getReward each match cast byte for byte."""
    assert build_gauge_deposit_calldata(STAKED_TOKEN_ID) == CANONICAL_DEPOSIT_CALLDATA
    assert CANONICAL_DEPOSIT_CALLDATA.startswith(f"0x{GAUGE_DEPOSIT_SELECTOR}")
    assert build_gauge_withdraw_calldata(STAKED_TOKEN_ID) == CANONICAL_WITHDRAW_CALLDATA
    assert CANONICAL_WITHDRAW_CALLDATA.startswith(f"0x{GAUGE_WITHDRAW_SELECTOR}")
    assert build_gauge_get_reward_calldata(STAKED_TOKEN_ID) == CANONICAL_GET_REWARD_CALLDATA
    assert CANONICAL_GET_REWARD_CALLDATA.startswith(f"0x{GAUGE_GET_REWARD_SELECTOR}")


def test_gauge_reward_read_calldata_matches_cast_canonical_vectors() -> None:
    """The earned and rewards views match cast byte for byte."""
    earned = build_gauge_earned_read_calldata(SAFE_ADDRESS, STAKED_TOKEN_ID)
    assert earned == CANONICAL_EARNED_READ_CALLDATA
    assert CANONICAL_EARNED_READ_CALLDATA.startswith(f"0x{GAUGE_EARNED_SELECTOR}")
    rewards = build_gauge_rewards_read_calldata(STAKED_TOKEN_ID)
    assert rewards == CANONICAL_REWARDS_READ_CALLDATA
    assert CANONICAL_REWARDS_READ_CALLDATA.startswith(f"0x{GAUGE_REWARDS_SELECTOR}")


def test_decode_positions_view_reproduces_live_staked_position() -> None:
    """The frozen live AAPLc/USDC return decodes to its observed fields."""
    view = decode_lp_positions_view(LIVE_POSITIONS_RETURN)
    assert view.nonce == 0
    assert view.operator_address == "0x0000000000000000000000000000000000000000"
    assert view.token0_address == USDC_ADDRESS
    assert view.token1_address == AAPLC_ADDRESS
    assert view.tick_spacing == 10
    assert view.tick_lower == -11680
    assert view.tick_upper == -11650
    assert view.liquidity == 135_048_209_692
    assert view.fee_growth_inside0_last_x128 == int(
        "0xfffffffffffffffffffffffffffffffffff600a79238cbfde57dc3497959648b", 16
    )
    assert view.fee_growth_inside1_last_x128 == int(
        "0xfffffffffffffffffffffffffffffffffffd10dd1cf285d0aa626b18ec088d49", 16
    )
    assert view.tokens_owed0_units == 0
    assert view.tokens_owed1_units == 0


def remint(**updates: object) -> LpMintParams:
    """Rebuild the fixed mint parameters with fresh validation after edits."""
    values = mint_params().model_dump()
    values.update(updates)
    return LpMintParams.model_validate(values)


def test_mint_params_reject_incoherent_ranges() -> None:
    """Inverted, off-grid, and empty mints are refused at validation."""
    with pytest.raises(ValueError, match="tick_lower must be below tick_upper"):
        remint(tick_lower=-11640, tick_upper=-11650)
    with pytest.raises(ValueError, match="multiples of the tick spacing"):
        remint(tick_lower=-11645, tick_upper=-11640)
    with pytest.raises(ValueError, match="positive token amount"):
        remint(amount0_desired_units=0, amount1_desired_units=0)
    with pytest.raises(ValueError, match="two distinct tokens"):
        remint(token1_address=USDC_ADDRESS)
    with pytest.raises(ValueError):
        remint(tick_lower=-9_000_000)


def test_collect_params_reject_empty_maxima() -> None:
    """A collect authorizing nothing is refused at validation."""
    with pytest.raises(ValueError, match="positive maximum"):
        LpCollectParams(
            token_id=STAKED_TOKEN_ID,
            recipient_address=SAFE_ADDRESS,
            amount0_max_units=0,
            amount1_max_units=0,
        )
    with pytest.raises(ValueError):
        LpCollectParams(
            token_id=STAKED_TOKEN_ID,
            recipient_address=SAFE_ADDRESS,
            amount0_max_units=MAX_UINT128 + 1,
            amount1_max_units=0,
        )


def test_decrease_params_reject_zero_liquidity() -> None:
    """A decrease of zero liquidity is refused at validation."""
    with pytest.raises(ValueError):
        LpDecreaseLiquidityParams(
            token_id=STAKED_TOKEN_ID,
            liquidity=0,
            amount0_min_units=0,
            amount1_min_units=0,
            deadline=FIXED_DEADLINE,
        )


def test_mint_calldata_normalizes_address_case() -> None:
    """Checksummed addresses encode identically to their lowercase forms."""
    checksummed = mint_params().model_copy(
        update={"token0_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}
    )
    assert build_lp_mint_calldata(checksummed) == CANONICAL_MINT_CALLDATA


def test_negative_token_ids_are_refused() -> None:
    """Every token-id-taking builder refuses negative ids."""
    with pytest.raises(ValueError, match="non-negative"):
        build_lp_burn_calldata(-1)
    with pytest.raises(ValueError, match="non-negative"):
        build_lp_positions_read_calldata(-1)
    with pytest.raises(ValueError, match="non-negative"):
        build_gauge_deposit_calldata(-1)
    with pytest.raises(ValueError, match="non-negative"):
        build_gauge_withdraw_calldata(-1)
    with pytest.raises(ValueError, match="non-negative"):
        build_gauge_get_reward_calldata(-1)
    with pytest.raises(ValueError, match="non-negative"):
        build_gauge_earned_read_calldata(SAFE_ADDRESS, -1)
    with pytest.raises(ValueError, match="non-negative"):
        build_gauge_rewards_read_calldata(-1)


def test_decode_positions_view_refuses_malformed_returns() -> None:
    """Short, non-hex, and off-grid returns are refused undecoded."""
    with pytest.raises(ValueError, match="instead of 384"):
        decode_lp_positions_view("0x" + "00" * 11)
    with pytest.raises(ValueError, match="hexadecimal"):
        decode_lp_positions_view("0x" + "zz" * 384)
    with pytest.raises(ValueError, match="0x-prefixed"):
        decode_lp_positions_view(LIVE_POSITIONS_RETURN[2:])
    off_grid = (
        LIVE_POSITIONS_RETURN[: 2 + 64 * 6]
        + format(-11645 & (2**256 - 1), "064x")
        + LIVE_POSITIONS_RETURN[2 + 64 * 7 :]
    )
    with pytest.raises(ValueError, match="multiples of the tick spacing"):
        decode_lp_positions_view(off_grid)


def test_param_models_are_immutable() -> None:
    """Built parameters cannot be mutated after validation."""
    params = mint_params()
    with pytest.raises(ValueError):
        params.tick_lower = -11645
    view = decode_lp_positions_view(LIVE_POSITIONS_RETURN)
    with pytest.raises(ValueError):
        view.liquidity = 1
