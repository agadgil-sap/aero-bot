"""Live read-only proof for the ranging log-window repair (public data only).

Runs the REAL production backend path - EventHistoryRpcBackend.fetch_price_path
built exactly as aero_bot.strategy.PolicyEngine builds it, through the corrected
RANGING_READ_LOG_WINDOW_BLOCKS constant - against the public Tenderly Base
gateway with public pool inputs (the MSTRc pool from the failed preflight).

Also replays the boundary pair that pinned the defect: the pre-fix 2,000-block
eth_getLogs shape (rejected, RPC error -32602) and the repaired 1,000-block
shape (accepted), so one artifact carries both the production failure and the
successful counterfactual.

Read-only chain proof: no signer, no credentials, no sealed values, no state
changes. Everything printed is public chain data: block numbers, addresses,
topics, counts.
"""

import json
import sys
from datetime import timedelta
from typing import Any

import httpx

from aero_bot.history import (
    RANGING_READ_LOOKBACK,
    EventHistoryRpcBackend,
    HistoryUnavailableError,
)
from aero_bot.ranging_reads import RANGING_READ_LOG_WINDOW_BLOCKS

POOL = "0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0"  # MSTRc/USDC, from the failed cycle report
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SWAP_TOPIC0 = "0xc42079f94a6350d7e6235f29174924f928cc2ac81886456d53743539d80b6603"
HOST = "https://gateway.tenderly.co/public/base"

TOKEN0_SELECTOR = "0x0dfe1681"
TOKEN1_SELECTOR = "0xd21220a7"
DECIMALS_SELECTOR = "0x313ce567"


class SpanRecordingTransport(httpx.BaseTransport):
    """Delegate to the real network while recording every eth_getLogs span."""

    def __init__(self) -> None:
        self._inner = httpx.HTTPTransport()
        self.spans: list[tuple[int, int]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        if body.get("method") == "eth_getLogs":
            query = body["params"][0]
            self.spans.append(
                (int(query["fromBlock"], 16), int(query["toBlock"], 16))
            )
        response = self._inner.handle_request(request)
        return response


def eth_call(client: httpx.Client, to: str, data: str) -> Any:
    payload = {
        "jsonrpc": "2.0",
        "id": 900,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    resp = client.post(HOST, json=payload)
    return resp.json()["result"]


def getlogs(host: str, from_block: int, span: int) -> tuple[int, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": 901,
        "method": "eth_getLogs",
        "params": [
            {
                "fromBlock": hex(from_block),
                "toBlock": hex(from_block + span - 1),
                "address": POOL,
                "topics": [SWAP_TOPIC0],
            }
        ],
    }
    resp = httpx.post(host, json=payload, timeout=30)
    body = resp.json()
    if "error" in body:
        return body["error"]["code"], body["error"].get("message")
    return 0, len(body["result"])


def main() -> int:
    print(f"host {HOST}")
    print(f"pool {POOL} (MSTRc) window constant {RANGING_READ_LOG_WINDOW_BLOCKS}")

    # Boundary pair at a recent anchor: the pre-fix 2000-block shape fails,
    # the repaired 1000-block shape succeeds - the exact diagnostic matrix.
    anchor = int(
        httpx.post(
            HOST,
            json={"jsonrpc": "2.0", "id": 902, "method": "eth_blockNumber", "params": []},
            timeout=30,
        ).json()["result"],
        16,
    )
    print(f"head {anchor}")
    from_block = anchor - 2_100
    code_2000, detail_2000 = getlogs(HOST, from_block, 2_000)
    code_1000, detail_1000 = getlogs(HOST, from_block, 1_000)
    print(f"span=2000 addr+topics -> code {code_2000} {detail_2000}")
    print(f"span=1000 addr+topics -> code {code_1000} {detail_1000}")
    if code_1000 != 0:
        print("FAIL: the 1000-block span was rejected; repair premise broken")
        return 1
    if code_2000 == 0:
        print("NOTE: the 2000-block span was accepted at probe time (gateway cap moved)")

    # The real production path, built exactly as the policy engine builds it.
    transport = SpanRecordingTransport()
    backend = EventHistoryRpcBackend(
        rpc_url=HOST,
        log_window_blocks=RANGING_READ_LOG_WINDOW_BLOCKS,
        transport=transport,
    )
    with httpx.Client(timeout=30, transport=transport) as client:
        token0 = "0x" + eth_call(client, POOL, TOKEN0_SELECTOR)[-40:]
        token1 = "0x" + eth_call(client, POOL, TOKEN1_SELECTOR)[-40:]
        d0 = int(eth_call(client, token0, DECIMALS_SELECTOR), 16)
        d1 = int(eth_call(client, token1, DECIMALS_SELECTOR), 16)
    stock = token0 if token0.lower() != USDC.lower() else token1
    stock_dec = d0 if token0.lower() != USDC.lower() else d1
    print(f"token0 {token0} d{d0}; token1 {token1} d{d1}")
    try:
        path = backend.fetch_price_path(
            pool_address=POOL,
            token_address=stock,
            token0_address=token0,
            token1_address=token1,
            token_decimals=stock_dec,
            quote_decimals=6,
            lookback=RANGING_READ_LOOKBACK,
        )
    except HistoryUnavailableError as failure:
        print(f"FAIL: fetch_price_path failed: {failure}")
        return 1
    spans = [to_block - from_block + 1 for from_block, to_block in transport.spans]
    widest = max(spans) if spans else 0
    ordered = all(
        later[0] == earlier[1] + 1
        for earlier, later in zip(transport.spans, transport.spans[1:])
    )
    print(
        f"fetch_price_path OK: {len(path.points)} points over "
        f"{path.from_block}..{path.to_block} "
        f"({path.to_block - path.from_block + 1} blocks, "
        f"lookback {RANGING_READ_LOOKBACK / timedelta(hours=1):.1f}h)"
    )
    print(
        f"getlogs windows {len(transport.spans)}; widest span {widest}; "
        f"gap-free/overlap-free consecutive pages: {ordered}"
    )
    if widest > RANGING_READ_LOG_WINDOW_BLOCKS:
        print("FAIL: a window exceeded the gateway cap")
        return 1
    if not ordered or len(path.points) < 2:
        print("FAIL: coverage or ordering is incomplete")
        return 1
    blocks = [point.block_number for point in path.points]
    keys = [(point.block_number, point.log_index) for point in path.points]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        print("FAIL: points are unordered or duplicated")
        return 1
    print(f"ordered unique points {len(keys)}; first block {blocks[0]}; last block {blocks[-1]}")
    print("VERDICT: production ranging read completes at the capped window")
    return 0


if __name__ == "__main__":
    sys.exit(main())
