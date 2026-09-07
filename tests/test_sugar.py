"""Behavior tests for the read-only LP Sugar enumeration backend."""

import json
from importlib.resources import files
from typing import Any

import httpx
import pytest

from aero_bot.sugar import (
    ALL_FUNCTION_SELECTOR,
    DEFAULT_PAGE_SIZE,
    LP_FIELD_LAYOUT,
    LpSugarRecord,
    LpSugarRpcBackend,
    decode_lp_page,
)
from aero_bot.venues import (
    AERO_TOKEN_ADDRESS,
    BASE_USDC_ADDRESS,
    SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
    PoolKind,
)

# Fixture identities mirror official contracts without claiming live observations.
B20_ADDRESS = "0xb20000000000000000000078ee7ce2fe4908108c"
POOL_ADDRESS = "0x1111111111111111111111111111111111111111"
POOL_ADDRESS_ALT = "0x1212121212121212121212121212121212121212"
GAUGE_ADDRESS = "0x2222222222222222222222222222222222222222"
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
ZERO_ADDRESS = "0x" + "0" * 40
# A fixture block identifier is pinned so every page shares one coherent state.
FIXTURE_BLOCK = 50977853


def encode_word(value: int) -> bytes:
    """Encode one integer as an unsigned 32-byte ABI word.

    Args:
        value: The unsigned or negative Python integer.

    Returns:
        The two's-complement big-endian word.
    """
    return (value % (1 << 256)).to_bytes(32, "big")


def encode_address_word(address: str) -> bytes:
    """Encode one EVM address as a right-padded ABI word.

    Args:
        address: A 0x-prefixed 20-byte address.

    Returns:
        The address shifted into the low 160 bits of one word.
    """
    return encode_word(int(address, 16))


def encode_record_head(record: dict[str, object]) -> bytes:
    """Encode one Lp tuple as its 28-word static head plus string tail.

    Args:
        record: Field values keyed by ABI field name.

    Returns:
        The complete tuple encoding including the dynamic symbol tail.
    """
    head_words: list[bytes] = []
    for field_name, _ in LP_FIELD_LAYOUT:
        if field_name == "symbol":
            # The dynamic string slot holds its byte offset relative to the tuple start.
            head_words.append(encode_word(28 * 32))
            continue
        value = record[field_name]
        if field_name in {
            "lp",
            "token0",
            "token1",
            "gauge",
            "fee",
            "bribe",
            "factory",
            "emissions_token",
            "nfpm",
            "alm",
            "root",
        }:
            head_words.append(encode_address_word(str(value)))
        elif isinstance(value, bool):
            head_words.append(encode_word(int(value)))
        elif isinstance(value, int):
            head_words.append(encode_word(value))
        else:
            raise TypeError(f"unsupported fixture field {field_name}")
    symbol_bytes = str(record["symbol"]).encode("utf-8")
    # String data is one length word followed by right-padded UTF-8 bytes.
    tail = encode_word(len(symbol_bytes)) + symbol_bytes.ljust(
        (len(symbol_bytes) + 31) // 32 * 32, b"\x00"
    )
    return b"".join(head_words) + tail


def encode_lp_page(records: list[dict[str, object]]) -> str:
    """Encode a list of Lp tuples as a complete all() return value.

    Args:
        records: Field dictionaries in ABI field naming.

    Returns:
        The 0x-prefixed hexadecimal ABI encoding of the tuple array.
    """
    element_encodings = [encode_record_head(record) for record in records]
    # The offset table starts immediately after the array length word.
    offsets_block_start = 2 * 32 + 32 * len(records)
    offset_words = []
    running = 0
    for element in element_encodings:
        offset_words.append(encode_word(offsets_block_start - 2 * 32 + running))
        running += len(element)
    return (
        "0x"
        + (
            encode_word(32)
            + encode_word(len(records))
            + b"".join(offset_words)
            + b"".join(element_encodings)
        ).hex()
    )


def lp_record(**overrides: object) -> dict[str, object]:
    """Build one default Slipstream B20/native-USDC fixture record.

    Args:
        **overrides: ABI-named fields changed to exercise one behavior.

    Returns:
        A complete field dictionary matching the vendored Lp layout.
    """
    values: dict[str, object] = {
        "lp": POOL_ADDRESS,
        "symbol": "vSlipstream-B20/USDC",
        "decimals": 18,
        "liquidity": 123_456,
        "type": 10,
        "tick": -5,
        "sqrt_ratio": 1 << 96,
        "token0": B20_ADDRESS,
        "reserve0": 10**18,
        "staked0": 10**17,
        "token1": BASE_USDC_ADDRESS,
        "reserve1": 5_000_000,
        "staked1": 1_000_000,
        "gauge": GAUGE_ADDRESS,
        "gauge_liquidity": 9_999,
        "gauge_alive": True,
        "fee": "0x" + "33" * 20,
        "bribe": "0x" + "44" * 20,
        "factory": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
        "emissions": 4_494_371_922_759_724,
        "emissions_token": AERO_TOKEN_ADDRESS,
        "pool_fee": 500,
        "unstaked_fee": 100_000,
        "token0_fees": 1,
        "token1_fees": 2,
        "nfpm": "0x" + "55" * 20,
        "alm": ZERO_ADDRESS,
        "root": ZERO_ADDRESS,
    }
    values.update(overrides)
    return values


class FixtureRpcTransport(httpx.MockTransport):
    """Serve deterministic JSON-RPC responses without touching the network."""

    def __init__(
        self,
        pages_by_offset: dict[int, str],
        failures_before_success: int = 0,
        error_code: int = -32016,
    ) -> None:
        """Configure the fixture endpoint with optional transient failures.

        Args:
            pages_by_offset: Encoded all() pages keyed by their pagination offset.
            failures_before_success: Rate-limit failures served before success.
            error_code: The JSON-RPC error code used for transient failures.
        """
        # Request counting lets tests assert bounded retry behavior exactly.
        self.calls: list[dict[str, Any]] = []
        self._pages_by_offset = pages_by_offset
        self._remaining_failures = failures_before_success
        self._error_code = error_code
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Answer one JSON-RPC request with configured fixture evidence."""
        payload = json.loads(request.content.decode("utf-8"))
        self.calls.append(payload)
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            # Rate-limit failures reuse the observed public-endpoint wording.
            message = "over rate limit" if self._error_code == -32016 else "execution reverted"
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": self._error_code, "message": message},
                },
            )
        if payload["method"] == "eth_blockNumber":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": 1, "result": hex(FIXTURE_BLOCK)}
            )
        # The eth_call calldata is 0x, the selector, then two 32-byte arguments.
        calldata = payload["params"][0]["data"]
        offset = int(calldata[74:138], 16)
        page = self._pages_by_offset.get(offset)
        if page is None:
            raise AssertionError(f"unexpected pagination offset {offset}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": page})


def no_sleep(_seconds: float) -> None:
    """Discard retry delays so failure-path tests run instantly."""
    return None


def test_decode_page_round_trips_one_slipstream_record() -> None:
    """A well-formed page decodes every typed field with addresses normalized."""
    # The fixture record exercises signed ticks, live gauges, and AERO emissions.
    encoded = encode_lp_page([lp_record()])
    records = decode_lp_page(bytes.fromhex(encoded[2:]))

    assert len(records) == 1
    record = records[0]
    assert record.lp_address == POOL_ADDRESS
    assert record.symbol == "vSlipstream-B20/USDC"
    assert record.tick_spacing == 10
    assert record.current_tick == -5
    assert record.sqrt_ratio == 1 << 96
    assert record.pool_active_liquidity == 123_456
    assert record.token0_address == B20_ADDRESS
    assert record.token1_address == BASE_USDC_ADDRESS.lower()
    assert record.reserve0 == 10**18
    assert record.staked1 == 1_000_000
    assert record.gauge_address == GAUGE_ADDRESS
    assert record.gauge_alive is True
    assert record.factory_address == SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS.lower()
    assert record.emissions_per_second == 4_494_371_922_759_724
    assert record.emissions_token_address == AERO_TOKEN_ADDRESS.lower()
    assert record.pool_fee_ppm == 500
    assert record.unstaked_fee_ppm == 100_000


def test_decode_page_maps_absent_contracts_to_none() -> None:
    """Zero gauge and emissions-token addresses decode to explicit absence."""
    # A gaugeless pool reports the zero address for both gauge fields.
    encoded = encode_lp_page(
        [lp_record(gauge=ZERO_ADDRESS, emissions_token=ZERO_ADDRESS, emissions=0)]
    )
    record = decode_lp_page(bytes.fromhex(encoded[2:]))[0]

    assert record.gauge_address is None
    assert record.emissions_token_address is None
    assert record.emissions_per_second == 0


def test_decode_page_rejects_malformed_encodings() -> None:
    """Structurally invalid responses fail closed instead of yielding records."""
    # A valid page provides the baseline for each mutation below.
    valid = encode_lp_page([lp_record()])
    valid_bytes = bytes.fromhex(valid[2:])
    # Each malformed candidate violates one structural invariant of the encoding.
    malformed = [
        # Truncated body cannot contain a complete word.
        valid_bytes[:31],
        # A wrong outer offset is not the single-dynamic-return layout.
        b"\x00" * 31 + b"\x40",
        # A count above the contract cap signals corrupt output.
        encode_word(32) + encode_word(501),
        # A count word pointing beyond the body is out of bounds.
        encode_word(32) + encode_word(2),
    ]
    for candidate in malformed:
        with pytest.raises(ValueError):
            decode_lp_page(candidate)


def test_decode_page_rejects_invalid_pool_type() -> None:
    """A type word outside the documented Sugar encoding is rejected."""
    # The int24 type field only defines positive spacing, 0, and -1.
    encoded = encode_lp_page([lp_record(type=-2)])

    with pytest.raises(ValueError, match="invalid pool type"):
        decode_lp_page(bytes.fromhex(encoded[2:]))


def test_decode_page_rejects_unaligned_element_offset() -> None:
    """A non-word-aligned element offset signals a corrupt offset table."""
    # The mutation corrupts the first element offset word to an odd byte offset.
    valid = bytearray(bytes.fromhex(encode_lp_page([lp_record()])[2:]))
    valid[64:96] = encode_word(33)

    with pytest.raises(ValueError, match="word aligned"):
        decode_lp_page(bytes(valid))


def test_decode_page_rejects_out_of_bounds_symbol_offset() -> None:
    """A symbol offset pointing beyond the response fails closed."""
    # The mutation replaces the symbol offset slot with an implausible value.
    valid = bytearray(bytes.fromhex(encode_lp_page([lp_record()])[2:]))
    # The element head starts after the outer offset, length, and one offset word.
    element_head_start = 3 * 32
    # The symbol slot is the second word of the static head.
    valid[element_head_start + 32 : element_head_start + 64] = encode_word(1 << 20)

    with pytest.raises(ValueError, match="out of bounds"):
        decode_lp_page(bytes(valid))


def test_pool_kind_mapping_covers_documented_type_values() -> None:
    """Tick spacing maps to Slipstream while 0 and -1 map to classic kinds."""
    from aero_bot.sugar import _pool_kind_for_tick_spacing

    assert _pool_kind_for_tick_spacing(10) is PoolKind.SLIPSTREAM
    assert _pool_kind_for_tick_spacing(1) is PoolKind.SLIPSTREAM
    assert _pool_kind_for_tick_spacing(0) is PoolKind.CLASSIC_STABLE
    assert _pool_kind_for_tick_spacing(-1) is PoolKind.CLASSIC_VOLATILE
    with pytest.raises(ValueError, match="invalid pool type"):
        _pool_kind_for_tick_spacing(-2)


def test_bundled_abi_fragment_matches_decoder_layout() -> None:
    """The vendored ABI fragment and the decoder layout cannot drift apart."""
    # The bundled resource is the reviewable vendored ABI evidence.
    document = files("aero_bot").joinpath("lp_sugar_abi.json").read_text(encoding="utf-8")
    all_entry = json.loads(document)[0]

    assert all_entry["name"] == "all"
    assert all_entry["stateMutability"] == "view"
    assert [item["type"] for item in all_entry["inputs"]] == ["uint256", "uint256"]
    components = all_entry["outputs"][0]["components"]
    assert [(item["name"], item["type"]) for item in components] == list(LP_FIELD_LAYOUT)


def test_discover_paginates_and_scopes_pairs_to_b20_and_usdc() -> None:
    """Pagination completes on a short page and returns only pair-scoped candidates."""
    # Two full pages and one empty terminal page exercise the termination rule.
    transport = FixtureRpcTransport(
        pages_by_offset={
            0: encode_lp_page(
                [
                    lp_record(),
                    # A WETH/USDC pool is out of scope and must not become a candidate.
                    lp_record(lp=POOL_ADDRESS_ALT, token0=WETH_ADDRESS, symbol="vUSDC/WETH"),
                ]
            ),
            2: encode_lp_page(
                [
                    # A B20 pool quoted in WETH is equally out of scope.
                    lp_record(
                        lp=POOL_ADDRESS_ALT, token1=WETH_ADDRESS, symbol="vSlipstream-B20/WETH"
                    ),
                    # A classic B20/USDC pool stays in scope for adapter policy checks.
                    lp_record(lp=POOL_ADDRESS_ALT, type=-1, symbol="vAMM-B20/USDC"),
                ]
            ),
            4: encode_lp_page([]),
        }
    )
    backend = LpSugarRpcBackend(page_size=2, transport=transport, sleep=no_sleep)
    batch = backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)

    assert len(batch.candidates) == 2
    assert batch.candidates[0].pool_address == POOL_ADDRESS
    assert batch.candidates[0].pool_kind is PoolKind.SLIPSTREAM
    assert batch.candidates[1].pool_kind is PoolKind.CLASSIC_VOLATILE
    # The batch retains the complete enumeration size before pair scoping.
    assert batch.enumerated_pool_count == 4
    assert (
        batch.source
        == f"lp-sugar:0x27fc745390d1f4baF8D184FBd97748340f786634@block:{FIXTURE_BLOCK}".lower()
    )
    assert batch.observed_at is not None
    # The endpoint saw the pinned block query plus exactly three pages.
    assert [call["method"] for call in transport.calls] == [
        "eth_blockNumber",
        "eth_call",
        "eth_call",
        "eth_call",
    ]
    # Every eth_call targets only the Sugar contract with the fixed all() selector.
    for call in transport.calls[1:]:
        assert call["params"][0]["to"].startswith("0x27fc")
        assert call["params"][0]["data"].startswith("0x" + ALL_FUNCTION_SELECTOR)


def test_discover_retries_rate_limited_requests_with_backoff() -> None:
    """Transient rate limiting is retried with exponential backoff and then succeeds."""
    delays: list[float] = []
    transport = FixtureRpcTransport(
        pages_by_offset={0: encode_lp_page([lp_record()])},
        failures_before_success=2,
    )
    backend = LpSugarRpcBackend(
        page_size=DEFAULT_PAGE_SIZE,
        transport=transport,
        sleep=delays.append,
    )
    batch = backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)

    # Two transient failures produced two exponential backoff waits before success.
    assert delays == [0.5, 1.0]
    # The pinned block query retried twice and succeeded on its third attempt.
    assert len(transport.calls) == 4
    assert [call["method"] for call in transport.calls] == [
        "eth_blockNumber",
        "eth_blockNumber",
        "eth_blockNumber",
        "eth_call",
    ]
    assert batch.enumerated_pool_count == 1


def test_discover_fails_closed_after_exhausted_retries() -> None:
    """Persistent rate limiting fails the enumeration instead of looping."""
    from aero_bot.venues import PoolDiscoveryUnavailableError

    transport = FixtureRpcTransport(
        pages_by_offset={},
        failures_before_success=100,
    )
    backend = LpSugarRpcBackend(
        page_size=DEFAULT_PAGE_SIZE,
        max_attempts=2,
        transport=transport,
        sleep=no_sleep,
    )

    with pytest.raises(PoolDiscoveryUnavailableError, match="after 2 attempts"):
        backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)


def test_discover_fails_immediately_on_contract_revert() -> None:
    """A non-transient JSON-RPC error is never retried."""
    from aero_bot.venues import PoolDiscoveryUnavailableError

    transport = FixtureRpcTransport(
        pages_by_offset={},
        failures_before_success=1,
        error_code=-32000,
    )
    backend = LpSugarRpcBackend(transport=transport, sleep=no_sleep)

    with pytest.raises(PoolDiscoveryUnavailableError, match="-32000"):
        backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)
    # Exactly one request was made because the revert is deterministic.
    assert len(transport.calls) == 1


def test_discover_retries_http_rate_limit_status() -> None:
    """An HTTP 429 response is transient and retried before succeeding."""

    class HttpLimitedTransport(httpx.MockTransport):
        """Serve one HTTP 429 response, then the complete fixture enumeration."""

        def __init__(self) -> None:
            self.attempts = 0
            super().__init__(self._handle)

        def _handle(self, request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            self.attempts += 1
            if self.attempts == 1:
                return httpx.Response(429, text="too many requests")
            if payload["method"] == "eth_blockNumber":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "result": hex(FIXTURE_BLOCK)}
                )
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": 1, "result": encode_lp_page([lp_record()])},
            )

    backend = LpSugarRpcBackend(transport=HttpLimitedTransport(), sleep=no_sleep)
    batch = backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)

    assert batch.enumerated_pool_count == 1


def test_discover_fails_closed_on_oversized_response() -> None:
    """A response above the configured byte bound fails immediately."""
    from aero_bot.venues import PoolDiscoveryUnavailableError

    backend = LpSugarRpcBackend(
        max_response_bytes=1,
        transport=FixtureRpcTransport(pages_by_offset={0: encode_lp_page([lp_record()])}),
        sleep=no_sleep,
    )

    with pytest.raises(PoolDiscoveryUnavailableError, match="above the configured limit"):
        backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)


def test_discover_retries_transport_errors() -> None:
    """Connection failures are transient and retried before failing closed."""

    class FlakyTransport(httpx.MockTransport):
        """Fail the first request at the transport layer, then delegate."""

        def __init__(self) -> None:
            self.attempts = 0
            super().__init__(self._handle)

        def _handle(self, request: httpx.Request) -> httpx.Response:
            self.attempts += 1
            if self.attempts == 1:
                raise httpx.ConnectError("connection refused")
            payload = json.loads(request.content.decode("utf-8"))
            if payload["method"] == "eth_blockNumber":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "result": hex(FIXTURE_BLOCK)}
                )
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": encode_lp_page([lp_record()]),
                },
            )

    backend = LpSugarRpcBackend(transport=FlakyTransport(), sleep=no_sleep)
    batch = backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)

    assert batch.enumerated_pool_count == 1


def test_discover_rejects_non_hex_result_payload() -> None:
    """A result that is not a 0x-prefixed payload fails closed as malformed."""
    from aero_bot.venues import PoolDiscoveryUnavailableError

    class PlainTextTransport(httpx.MockTransport):
        """Return a non-hex result field for the first eth_call."""

        def __init__(self) -> None:
            super().__init__(self._handle)

        def _handle(self, request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content.decode("utf-8"))
            if payload["method"] == "eth_blockNumber":
                return httpx.Response(
                    200, json={"jsonrpc": "2.0", "id": 1, "result": hex(FIXTURE_BLOCK)}
                )
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "deadbeef"})

    backend = LpSugarRpcBackend(transport=PlainTextTransport(), sleep=no_sleep)

    with pytest.raises(PoolDiscoveryUnavailableError, match="failed decoding"):
        backend.discover(frozenset({B20_ADDRESS}), BASE_USDC_ADDRESS)


@pytest.mark.parametrize(
    "overrides",
    [
        {"page_size": 0},
        {"page_size": 501},
        {"timeout_seconds": 0},
        {"max_attempts": 0},
        {"page_delay_seconds": -1},
        {"max_response_bytes": 0},
        {"sugar_address": "0x1234"},
    ],
)
def test_backend_rejects_invalid_configuration(overrides: dict[str, Any]) -> None:
    """Invalid bounds and addresses fail before any request is attempted."""
    with pytest.raises(ValueError):
        LpSugarRpcBackend(**overrides)


def test_record_model_rejects_unknown_fields() -> None:
    """The immutable record model forbids unexpected source fields."""
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        LpSugarRecord.model_validate(
            {
                "lp_address": POOL_ADDRESS,
                "symbol": "fixture",
                "tick_spacing": 10,
                "current_tick": 0,
                "sqrt_ratio": 0,
                "pool_active_liquidity": 0,
                "token0_address": B20_ADDRESS,
                "reserve0": 0,
                "staked0": 0,
                "token1_address": BASE_USDC_ADDRESS,
                "reserve1": 0,
                "staked1": 0,
                "gauge_address": None,
                "gauge_liquidity": 0,
                "gauge_alive": True,
                "factory_address": SLIPSTREAM_GAUGES_V3_FACTORY_ADDRESS,
                "emissions_per_second": 0,
                "emissions_token_address": None,
                "pool_fee_ppm": 500,
                "unstaked_fee_ppm": 100_000,
                "token0_fees": 0,
                "token1_fees": 0,
                "unexpected_field": 1,
            }
        )
