"""Canonical calldata and views for the Slipstream LP position lifecycle.

Aerodrome's Slipstream concentrated-liquidity program runs on three deployed
generations, and each generation has its own NonfungiblePositionManager. The
NFPM address for any pool therefore comes from the LP Sugar record's ``nfpm``
field (which the Sugar resolves as the pool's gauge-factory ``nft()``), never
from a hardcoded constant. This module is purely the encoding and decoding
layer: every builder is a pure function over validated arguments, every
selector is pinned by an offline canonical-vector test built from
``cast calldata``, and nothing here signs, broadcasts, or reads the network.

Every contract fact below was verified live read-only on Base against the
AAPLc/USDC pool's own generation (``0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53``,
the Gauges V3 NFPM, name() "Slipstream Position NFT v1", symbol()
"AERO-CL-POS") and cross-checked against Aerodrome's verified sources
(github.com/aerodrome-finance/slipstream, periphery/interfaces/
INonfungiblePositionManager.sol and gauge/interfaces/ICLGauge.sol):

- Slipstream's ``mint`` carries a twelfth struct field ``uint160
  sqrtPriceX96`` that Uniswap v3 does not have; pass zero when the pool
  exists. The eleven-field v3-style selector ``0x6d70c415`` does not exist on
  any deployed NFPM - every mint probe against it reverted with empty data.
  The twelve-field probe reached the token pull and reverted ``STF`` for an
  unapproved sender, proving the selector.
- ``decreaseLiquidity`` takes ``uint128`` liquidity; ``collect`` takes
  ``uint128`` maxima. Both structs and the twelve-word ``positions`` return
  were byte-verified against the live contract.
- The CLGauge exposes ``getReward(uint256)`` only; the classic-gauge shape
  ``getReward(uint256,address[])`` reverts with empty data on every deployed
  CL gauge and must never be encoded.
- Staking mechanics that shape the lifecycle elsewhere: ``deposit`` pulls the
  NFT with ``safeTransferFrom`` after an NFPM approval, both ``deposit`` and
  ``withdraw`` auto-sweep pending fees, ``withdraw`` also auto-claims
  emissions, and a staked position cannot decrease liquidity (the gauge holds
  the NFT). Gauges V3 charges a penalty (100 percent live) on claims or
  withdrawals before the pool's minimum stake time has elapsed.
"""

from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from aero_bot.domain import IMMUTABLE_MODEL_CONFIG, EvmAddress, normalize_evm_address

# keccak256("mint((address,address,int24,int24,int24,uint256,uint256,uint256,
# uint256,uint256,address,uint256,uint160))")[0:4], the twelve-field Slipstream
# mint entry point verified live on every deployed NFPM generation.
LP_MINT_SELECTOR = "b5007d1f"
# keccak256("decreaseLiquidity((uint256,uint128,uint256,uint256,uint256))")[0:4].
LP_DECREASE_LIQUIDITY_SELECTOR = "0c49ccbe"
# keccak256("collect((uint256,address,uint128,uint128))")[0:4].
LP_COLLECT_SELECTOR = "fc6f7865"
# keccak256("setApprovalForAll(address,bool)")[0:4], inherited ERC721 approval.
ERC721_SET_APPROVAL_FOR_ALL_SELECTOR = "a22cb465"
# keccak256("burn(uint256)")[0:4], clears an emptied position NFT.
ERC721_BURN_SELECTOR = "42966c68"
# keccak256("positions(uint256)")[0:4], the twelve-word position view.
LP_POSITIONS_SELECTOR = "99fbab88"
# keccak256("deposit(uint256)")[0:4], the CLGauge stake entry point.
GAUGE_DEPOSIT_SELECTOR = "b6b55f25"
# keccak256("withdraw(uint256)")[0:4], the CLGauge unstake entry point.
GAUGE_WITHDRAW_SELECTOR = "2e1a7d4d"
# keccak256("getReward(uint256)")[0:4], the per-token emissions claim.
GAUGE_GET_REWARD_SELECTOR = "1c4b774b"
# keccak256("earned(address,uint256)")[0:4], the per-token accrued-emissions read.
GAUGE_EARNED_SELECTOR = "3e491d47"
# keccak256("rewards(uint256)")[0:4], the checkpointed claimable emissions read.
GAUGE_REWARDS_SELECTOR = "f301af42"
# Every ABI word encoded or decoded by this module is exactly 32 bytes.
WORD_BYTES = 32
# Solidity int24 spans this signed range; every tick argument must fit it.
INT24_MIN = -(2**23)
INT24_MAX = 2**23 - 1
# collect() maxima are uint128; this is the collect-everything value.
MAX_UINT128 = 2**128 - 1
# The verified positions() return carries exactly twelve words.
POSITIONS_VIEW_WORDS = 12


class LpMintParams(BaseModel):
    """Hold every validated argument of one Slipstream mint call.

    Field order mirrors the on-chain MintParams struct exactly so the encoding
    is a straight head walk with no reordering.
    """

    # Frozen strict fields keep the hashed calldata bound to validated values.
    model_config = IMMUTABLE_MODEL_CONFIG

    # Token zero is the lower-address ERC20 of the pool pair.
    token0_address: EvmAddress
    # Token one is the higher-address ERC20 of the pool pair.
    token1_address: EvmAddress
    # The pool's positive Slipstream tick spacing, also the tick grid size.
    tick_spacing: Annotated[int, Field(gt=0, le=INT24_MAX)]
    # The inclusive lower range boundary on the spacing grid.
    tick_lower: Annotated[int, Field(ge=INT24_MIN, le=INT24_MAX)]
    # The exclusive upper range boundary on the spacing grid.
    tick_upper: Annotated[int, Field(ge=INT24_MIN, le=INT24_MAX)]
    # The desired token-zero input in raw token units.
    amount0_desired_units: Annotated[int, Field(ge=0)]
    # The desired token-one input in raw token units.
    amount1_desired_units: Annotated[int, Field(ge=0)]
    # The minimum accepted token-zero input after slippage.
    amount0_min_units: Annotated[int, Field(ge=0)]
    # The minimum accepted token-one input after slippage.
    amount1_min_units: Annotated[int, Field(ge=0)]
    # The address receiving the minted position NFT.
    recipient_address: EvmAddress
    # The unix timestamp after which the call reverts.
    deadline: Annotated[int, Field(gt=0)]
    # The initial pool price for pool creation; zero when the pool exists.
    sqrt_price_x96: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def require_coherent_range(self) -> Self:
        """Reject inverted, empty, or off-grid ranges and empty mints."""
        if self.tick_lower >= self.tick_upper:
            raise ValueError("tick_lower must be below tick_upper")
        if self.tick_lower % self.tick_spacing != 0 or self.tick_upper % self.tick_spacing != 0:
            raise ValueError("both range boundaries must be multiples of the tick spacing")
        if self.amount0_desired_units == 0 and self.amount1_desired_units == 0:
            raise ValueError("a mint must desire at least one positive token amount")
        if self.token0_address == self.token1_address:
            raise ValueError("a pool pair needs two distinct tokens")
        return self


class LpDecreaseLiquidityParams(BaseModel):
    """Hold every validated argument of one decreaseLiquidity call."""

    # Frozen strict fields keep the built calldata bound to validated values.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The position NFT whose liquidity is being decreased.
    token_id: Annotated[int, Field(ge=0)]
    # The exact liquidity amount to remove, in the pool's L units.
    liquidity: Annotated[int, Field(gt=0, le=MAX_UINT128)]
    # The minimum accepted token-zero output after slippage.
    amount0_min_units: Annotated[int, Field(ge=0)]
    # The minimum accepted token-one output after slippage.
    amount1_min_units: Annotated[int, Field(ge=0)]
    # The unix timestamp after which the call reverts.
    deadline: Annotated[int, Field(gt=0)]


class LpCollectParams(BaseModel):
    """Hold every validated argument of one collect call.

    Maxima are uint128 on-chain; passing ``MAX_UINT128`` for a side collects
    every fee and leftover token owed on that side.
    """

    # Frozen strict fields keep the built calldata bound to validated values.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The position NFT whose fees are being collected.
    token_id: Annotated[int, Field(ge=0)]
    # The address receiving the collected tokens.
    recipient_address: EvmAddress
    # The inclusive maximum token-zero amount to collect.
    amount0_max_units: Annotated[int, Field(ge=0, le=MAX_UINT128)]
    # The inclusive maximum token-one amount to collect.
    amount1_max_units: Annotated[int, Field(ge=0, le=MAX_UINT128)]

    @model_validator(mode="after")
    def require_nonempty_collect(self) -> Self:
        """Reject a collect authorized to move nothing."""
        if self.amount0_max_units == 0 and self.amount1_max_units == 0:
            raise ValueError("a collect must authorize at least one positive maximum")
        return self


class LpPositionView(BaseModel):
    """Represent one decoded twelve-word positions() return."""

    # Frozen strict fields preserve the exact on-chain observation.
    model_config = IMMUTABLE_MODEL_CONFIG

    # The position's incrementing replay-protection counter.
    nonce: Annotated[int, Field(ge=0)]
    # The approved operator address, zero when none is set.
    operator_address: EvmAddress
    # The pool's lower-address token.
    token0_address: EvmAddress
    # The pool's higher-address token.
    token1_address: EvmAddress
    # The pool's positive tick spacing.
    tick_spacing: Annotated[int, Field(gt=0)]
    # The position's inclusive lower range boundary.
    tick_lower: Annotated[int, Field(ge=INT24_MIN, le=INT24_MAX)]
    # The position's exclusive upper range boundary.
    tick_upper: Annotated[int, Field(ge=INT24_MIN, le=INT24_MAX)]
    # The position's live liquidity in the pool's L units.
    liquidity: Annotated[int, Field(ge=0, le=MAX_UINT128)]
    # The token-zero fee growth captured at the position's last checkpoint.
    fee_growth_inside0_last_x128: Annotated[int, Field(ge=0)]
    # The token-one fee growth captured at the position's last checkpoint.
    fee_growth_inside1_last_x128: Annotated[int, Field(ge=0)]
    # The checkpointed token-zero tokens owed, before live collect accounting.
    tokens_owed0_units: Annotated[int, Field(ge=0, le=MAX_UINT128)]
    # The checkpointed token-one tokens owed, before live collect accounting.
    tokens_owed1_units: Annotated[int, Field(ge=0, le=MAX_UINT128)]

    @model_validator(mode="after")
    def require_ordered_range(self) -> Self:
        """Reject an off-grid or inverted decoded range."""
        if self.tick_lower >= self.tick_upper:
            raise ValueError("decoded tick_lower must be below tick_upper")
        if self.tick_lower % self.tick_spacing != 0 or self.tick_upper % self.tick_spacing != 0:
            raise ValueError("decoded range boundaries must be multiples of the tick spacing")
        return self


def _word(value: int) -> bytes:
    """Encode one non-negative integer as its 32-byte big-endian ABI word.

    Args:
        value: Non-negative integer fitting one 256-bit word.

    Returns:
        The 32-byte encoding.

    Raises:
        ValueError: If the value is negative or does not fit the word.
    """
    if value < 0 or value >= 2**256:
        raise ValueError("ABI words encode unsigned integers below 2^256 only")
    return value.to_bytes(WORD_BYTES, "big")


def _signed_word(value: int) -> bytes:
    """Encode one integer as its 32-byte two's-complement ABI word.

    Args:
        value: Integer in the signed 256-bit range.

    Returns:
        The 32-byte two's-complement encoding.

    Raises:
        ValueError: If the value is outside the signed 256-bit range.
    """
    if not -(2**255) <= value < 2**255:
        raise ValueError("signed ABI words encode integers within 256-bit two's complement")
    return value.to_bytes(WORD_BYTES, "big", signed=True)


def _address_word(address: str) -> bytes:
    """Encode one normalized address as its low-160-bit ABI word.

    Args:
        address: The 0x-prefixed address being encoded.

    Returns:
        The 32-byte word holding the address in its low 160 bits.
    """
    return bytes.fromhex(normalize_evm_address(address)[2:].rjust(64, "0"))


def _selector_and_words(selector: str, words: bytes) -> str:
    """Join one selector with its complete head-word encoding.

    Args:
        selector: The eight-character canonical selector hex.
        words: The concatenated argument words.

    Returns:
        Complete 0x-prefixed calldata.
    """
    return f"0x{selector}{words.hex()}"


def build_lp_mint_calldata(params: LpMintParams) -> str:
    """ABI-encode one Slipstream position mint with its twelve-field struct.

    The MintParams tuple is fully static, so the encoding is the selector
    followed by twelve consecutive head words in struct-field order.

    Args:
        params: The validated mint parameters.

    Returns:
        Complete 0x-prefixed calldata for the NFPM's mint function.
    """
    words = (
        _address_word(params.token0_address)
        + _address_word(params.token1_address)
        + _signed_word(params.tick_spacing)
        + _signed_word(params.tick_lower)
        + _signed_word(params.tick_upper)
        + _word(params.amount0_desired_units)
        + _word(params.amount1_desired_units)
        + _word(params.amount0_min_units)
        + _word(params.amount1_min_units)
        + _address_word(params.recipient_address)
        + _word(params.deadline)
        + _word(params.sqrt_price_x96)
    )
    return _selector_and_words(LP_MINT_SELECTOR, words)


def build_lp_decrease_liquidity_calldata(params: LpDecreaseLiquidityParams) -> str:
    """ABI-encode one liquidity decrease with its five-field struct.

    Args:
        params: The validated decrease parameters.

    Returns:
        Complete 0x-prefixed calldata for the NFPM's decreaseLiquidity.
    """
    words = (
        _word(params.token_id)
        + _word(params.liquidity)
        + _word(params.amount0_min_units)
        + _word(params.amount1_min_units)
        + _word(params.deadline)
    )
    return _selector_and_words(LP_DECREASE_LIQUIDITY_SELECTOR, words)


def build_lp_collect_calldata(params: LpCollectParams) -> str:
    """ABI-encode one fee collection with its four-field struct.

    Args:
        params: The validated collect parameters.

    Returns:
        Complete 0x-prefixed calldata for the NFPM's collect function.
    """
    words = (
        _word(params.token_id)
        + _address_word(params.recipient_address)
        + _word(params.amount0_max_units)
        + _word(params.amount1_max_units)
    )
    return _selector_and_words(LP_COLLECT_SELECTOR, words)


def build_set_approval_for_all_calldata(operator_address: str, approved: bool) -> str:
    """ABI-encode the NFPM's ERC721 operator approval.

    Args:
        operator_address: The address approved to move position NFTs; staking
            approves the CLGauge so its deposit can pull the NFT.
        approved: True grants the operator approval, false revokes it.

    Returns:
        Complete 0x-prefixed calldata for setApprovalForAll.
    """
    words = _address_word(operator_address) + _word(1 if approved else 0)
    return _selector_and_words(ERC721_SET_APPROVAL_FOR_ALL_SELECTOR, words)


def build_lp_burn_calldata(token_id: int) -> str:
    """ABI-encode the burn of one emptied position NFT.

    Args:
        token_id: The position NFT to burn; only cleared positions can burn.

    Returns:
        Complete 0x-prefixed calldata for burn.

    Raises:
        ValueError: If the token id is negative.
    """
    if token_id < 0:
        raise ValueError("token_id must be non-negative")
    return _selector_and_words(ERC721_BURN_SELECTOR, _word(token_id))


def build_lp_positions_read_calldata(token_id: int) -> str:
    """ABI-encode the twelve-word positions view for one token id.

    Args:
        token_id: The position NFT whose on-chain state is read.

    Returns:
        Complete 0x-prefixed calldata for the positions view.

    Raises:
        ValueError: If the token id is negative.
    """
    if token_id < 0:
        raise ValueError("token_id must be non-negative")
    return _selector_and_words(LP_POSITIONS_SELECTOR, _word(token_id))


def decode_lp_positions_view(result: str) -> LpPositionView:
    """Decode one twelve-word positions() return into a typed view.

    The layout below was verified against the live Gauges V3 NFPM: real
    AAPLc/USDC positions decode to their known pool tokens, spacing-ten grid
    boundaries, and liquidity, and a burned token id reverts with the "ID"
    message before returning.

    Args:
        result: The 0x-prefixed hex return bytes of the positions view.

    Returns:
        The immutable decoded position view.

    Raises:
        ValueError: If the return is not exactly twelve whole ABI words or a
            decoded field fails its own coherence validation.
    """
    if not result.startswith("0x"):
        raise ValueError("positions view result must be 0x-prefixed")
    try:
        data = bytes.fromhex(result[2:])
    except ValueError as error:
        raise ValueError("positions view result must be hexadecimal") from error
    if len(data) != POSITIONS_VIEW_WORDS * WORD_BYTES:
        raise ValueError(
            f"positions view returned {len(data)} bytes instead of "
            f"{POSITIONS_VIEW_WORDS * WORD_BYTES}"
        )

    def unsigned_word(index: int) -> int:
        return int.from_bytes(data[index * WORD_BYTES : (index + 1) * WORD_BYTES], "big")

    def signed_word(index: int) -> int:
        return int.from_bytes(
            data[index * WORD_BYTES : (index + 1) * WORD_BYTES], "big", signed=True
        )

    def address_word(index: int) -> str:
        return "0x" + format(unsigned_word(index) & ((1 << 160) - 1), "040x")

    return LpPositionView.model_validate(
        {
            "nonce": unsigned_word(0),
            "operator_address": address_word(1),
            "token0_address": address_word(2),
            "token1_address": address_word(3),
            "tick_spacing": signed_word(4),
            "tick_lower": signed_word(5),
            "tick_upper": signed_word(6),
            "liquidity": unsigned_word(7),
            "fee_growth_inside0_last_x128": unsigned_word(8),
            "fee_growth_inside1_last_x128": unsigned_word(9),
            "tokens_owed0_units": unsigned_word(10),
            "tokens_owed1_units": unsigned_word(11),
        }
    )


def build_gauge_deposit_calldata(token_id: int) -> str:
    """ABI-encode one CLGauge stake of an approved position NFT.

    The gauge pulls the NFT with safeTransferFrom during the call, so the NFPM
    must carry an operator approval for the gauge first.

    Args:
        token_id: The position NFT being staked.

    Returns:
        Complete 0x-prefixed calldata for the gauge's deposit.

    Raises:
        ValueError: If the token id is negative.
    """
    if token_id < 0:
        raise ValueError("token_id must be non-negative")
    return _selector_and_words(GAUGE_DEPOSIT_SELECTOR, _word(token_id))


def build_gauge_withdraw_calldata(token_id: int) -> str:
    """ABI-encode one CLGauge unstake returning the NFT to its owner.

    Withdrawal also auto-claims accrued emissions and sweeps pending fees, and
    a Gauges V3 pool may penalize withdrawals before its minimum stake time.

    Args:
        token_id: The position NFT being unstaked.

    Returns:
        Complete 0x-prefixed calldata for the gauge's withdraw.

    Raises:
        ValueError: If the token id is negative.
    """
    if token_id < 0:
        raise ValueError("token_id must be non-negative")
    return _selector_and_words(GAUGE_WITHDRAW_SELECTOR, _word(token_id))


def build_gauge_get_reward_calldata(token_id: int) -> str:
    """ABI-encode one per-token emissions claim on the CLGauge.

    The Slipstream gauge claims by token id only; the classic-gauge
    ``(uint256,address[])`` shape does not exist on any deployed CL gauge.

    Args:
        token_id: The staked position NFT whose emissions are claimed.

    Returns:
        Complete 0x-prefixed calldata for the gauge's getReward.

    Raises:
        ValueError: If the token id is negative.
    """
    if token_id < 0:
        raise ValueError("token_id must be non-negative")
    return _selector_and_words(GAUGE_GET_REWARD_SELECTOR, _word(token_id))


def build_gauge_earned_read_calldata(depositor_address: str, token_id: int) -> str:
    """ABI-encode the per-token accrued-emissions view.

    The gauge reports earned amounts only for the address that staked the
    token; any other account reverts.

    Args:
        depositor_address: The address that staked the position.
        token_id: The staked position NFT being queried.

    Returns:
        Complete 0x-prefixed calldata for the gauge's earned view.

    Raises:
        ValueError: If the token id is negative.
    """
    if token_id < 0:
        raise ValueError("token_id must be non-negative")
    words = _address_word(depositor_address) + _word(token_id)
    return _selector_and_words(GAUGE_EARNED_SELECTOR, words)


def build_gauge_rewards_read_calldata(token_id: int) -> str:
    """ABI-encode the checkpointed claimable-emissions view.

    Args:
        token_id: The staked position NFT being queried.

    Returns:
        Complete 0x-prefixed calldata for the gauge's rewards view.

    Raises:
        ValueError: If the token id is negative.
    """
    if token_id < 0:
        raise ValueError("token_id must be non-negative")
    return _selector_and_words(GAUGE_REWARDS_SELECTOR, _word(token_id))
