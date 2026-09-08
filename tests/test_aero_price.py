"""Pin the live AERO price read backing the corrected emissions-APR quotes."""

import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from aero_bot.executor import (
    AERODROME_VOLATILE_FACTORY_ADDRESS,
    ExecutorRpcBackend,
    ExecutorRpcRevertError,
)
from aero_bot.venues import BASE_USDC_ADDRESS

POOL_ADDRESS = "0x" + "ab" * 20


def _word(value: int) -> str:
    return hex(value)[2:].rjust(64, "0")


def _address_word(address: str) -> str:
    return address[2:].lower().rjust(64, "0")


def make_backend(handler: Callable[[httpx.Request], httpx.Response]) -> ExecutorRpcBackend:
    """Build one backend over a scripted transport."""
    return ExecutorRpcBackend(
        rpc_url="https://fixture.example",
        transport=httpx.MockTransport(handler),
        sleep=lambda _: None,
    )


def test_fetch_aero_price_reads_the_canonical_pair_reserves() -> None:
    """The price is the USDC reserve over the AERO reserve, order-aware."""

    def handler(request: httpx.Request) -> httpx.Response:
        call = json.loads(request.content)
        data = call["params"][0]["data"]
        if data.startswith("0x79bc57d5"):
            # getPool(USDC, AERO, false) -> the canonical volatile pair.
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": "0x" + POOL_ADDRESS[2:].rjust(64, "0")},
            )
        if data.startswith("0x0dfe1681"):
            # token0() -> AERO sorts after USDC, so USDC is token1.
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": "0x" + "94" * 20,
                },
            )
        if data.startswith("0x443cb4bc"):
            # reserve0() -> 2 AERO.
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + _word(2 * 10**18)}
            )
        if data.startswith("0x5a76f25e"):
            # reserve1() -> 1.30 USDC.
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + _word(1_300_000)}
            )
        raise AssertionError(f"unexpected call {data[:10]}")

    price = make_backend(handler).fetch_aero_price_usdc()
    assert price == Decimal("0.65")


def test_fetch_aero_price_pins_the_requested_block_tag() -> None:
    """The rehearsal's anchor block pins every reserve read."""
    seen_blocks: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        call = json.loads(request.content)
        seen_blocks.append(call["params"][1])
        data = call["params"][0]["data"]
        if data.startswith("0x79bc57d5"):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": "0x" + POOL_ADDRESS[2:].rjust(64, "0")},
            )
        if data.startswith("0x0dfe1681"):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": "0x" + _address_word(BASE_USDC_ADDRESS)},
            )
        if data.startswith("0x443cb4bc"):
            # reserve0() -> 1 USDC (token0 is USDC here).
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + _word(10**6)}
            )
        if data.startswith("0x5a76f25e"):
            # reserve1() -> 1 AERO.
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + _word(10**18)}
            )
        raise AssertionError(f"unexpected call {data[:10]}")

    price = make_backend(handler).fetch_aero_price_usdc("0x64")
    assert price == Decimal(1)
    assert seen_blocks == ["0x64"] * 4


def test_fetch_aero_price_fails_closed_on_zero_reserves() -> None:
    """A zero reserve leg refuses rather than quoting zero or infinity."""

    def handler(request: httpx.Request) -> httpx.Response:
        call = json.loads(request.content)
        data = call["params"][0]["data"]
        if data.startswith("0x79bc57d5"):
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": "0x" + POOL_ADDRESS[2:].rjust(64, "0")},
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + _word(0)})

    with pytest.raises(ValueError, match="zero reserve"):
        make_backend(handler).fetch_aero_price_usdc()


def test_fetch_aero_price_surfaces_reverts() -> None:
    """A reverting factory lookup surfaces as the typed revert error."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": 1, "error": {"code": 3, "message": "execution reverted"}},
        )

    with pytest.raises(ExecutorRpcRevertError):
        make_backend(handler).fetch_aero_price_usdc()


def test_volatile_factory_constant_matches_aerodrome_mainnet() -> None:
    """The factory constant is Aerodrome's canonical volatile PoolFactory."""
    assert AERODROME_VOLATILE_FACTORY_ADDRESS == "0x420dd381b31aef6683db6b902084cb0ffece40da"
