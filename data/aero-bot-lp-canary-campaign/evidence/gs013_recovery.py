"""Recover attempt 2's exact inner revert reason by simulating its mint calldata.

Attempt 2 (audit seq 116-118, refused 2026-09-08T04:00:52Z): snapshot block
51025253, range [-11630,-11610), desired 1426633 USDC + 1744817 AAPLc, 1 percent
minima. This script rebuilds that exact NFPM mint calldata and eth_calls it
from the Safe at the live state, then prints the required-vs-minimum amounts at
both the snapshot price and the measured post-refusal price.
"""

from decimal import Decimal, localcontext

from aero_bot.config import Settings
from aero_bot.executor import ExecutorRpcBackend
from aero_bot.lp_calldata import LpMintParams, build_lp_mint_calldata
from aero_bot.lp_plan import position_amounts_for_liquidity

SAFE = "0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28"
NFPM = "0xe1f8cd9ac4e4a65f54f38a5cdafca44f6dd68b53"
POOL = "0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0"
RPC = ExecutorRpcBackend(rpc_url=Settings().base_rpc_url)
SLOT0 = "0x3850c7bd"

A0, A1 = 1426633, 1744817
LO, HI = -11630, -11610
M0 = int(Decimal(A0) * Decimal("0.99"))
M1 = int(Decimal(A1) * Decimal("0.99"))
DEADLINE = 1788840000  # past attempt-2 era; only the string matters

params = LpMintParams(
    token0_address="0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC (lower address)
    token1_address="0xb200000000000000000000c2e324d24d7eecd1fb",  # AAPLc
    tick_spacing=10,
    tick_lower=LO,
    tick_upper=HI,
    amount0_desired_units=A0,
    amount1_desired_units=A1,
    amount0_min_units=M0,
    amount1_min_units=M1,
    recipient_address=SAFE,
    deadline=DEADLINE,
)
calldata = build_lp_mint_calldata(params)
print("minima:", M0, M1)
try:
    RPC._rpc_call(
        "eth_call",
        [{"to": NFPM, "from": SAFE, "data": calldata}, "latest"],
    )
    print("live eth_call SUCCEEDED (minima pass at the current price)")
except Exception as error:  # noqa: BLE001
    print("live eth_call REVERTED:", str(error)[:300])


def sqrt_ratio_at_tick(tick: Decimal) -> int:
    with localcontext() as ctx:
        ctx.prec = 50
        return int(
            (Decimal("1.0001") ** (tick / 2) * Decimal(2) ** 96).to_integral_value()
        )


# Liquidity pinned at the SNAPSHOT price. The snapshot's continuous tick is
# recovered exactly from the plan's own amount ratio: A0/A1 must equal
# unit0/unit1 at the snapshot, so solve for that tick first.
with localcontext() as ctx:
    ctx.prec = 50

    def ratio_at(tick: Decimal) -> Decimal:
        u0, u1 = position_amounts_for_liquidity(
            sqrt_ratio_at_tick(tick), LO, HI, Decimal(1)
        )
        return u0 / u1

    target = Decimal(A0) / Decimal(A1)
    low, high = Decimal("-11620"), Decimal("-11610")
    for _ in range(80):
        mid = (low + high) / 2
        if ratio_at(mid) < target:
            high = mid
        else:
            low = mid
    SNAPSHOT_TICK = (low + high) / 2
    _u0, _u1 = position_amounts_for_liquidity(
        sqrt_ratio_at_tick(SNAPSHOT_TICK), LO, HI, Decimal(1)
    )
    LIQUIDITY = Decimal(A1) / _u1
    print(
        f"snapshot tick solved from the amount ratio: {SNAPSHOT_TICK.quantize(Decimal('0.001'))};"
        f" implied desired0 {int((LIQUIDITY * _u0).quantize(Decimal('1')))} vs plan {A0}"
    )


def amounts_at(tick: Decimal) -> tuple[Decimal, Decimal]:
    ratio = sqrt_ratio_at_tick(tick)
    with localcontext() as ctx:
        ctx.prec = 50
        unit0, unit1 = position_amounts_for_liquidity(ratio, LO, HI, Decimal(1))
        return LIQUIDITY * unit0, LIQUIDITY * unit1


for label, tick in (
    (f"snapshot {SNAPSHOT_TICK.quantize(Decimal('0.001'))} (04:00:35)", SNAPSHOT_TICK),
    ("measured -11614.2 (04:01:34)", Decimal("-11614.2")),
):
    r0, r1 = amounts_at(tick)
    print(
        f"{label}: required {r0.quantize(Decimal('1'))} USDC / {r1.quantize(Decimal('1'))} AAPLc"
        f" vs minima {M0} / {M1}"
    )

raw = RPC.eth_call(POOL, SLOT0)
sx = int(raw[2:66], 16)
with localcontext() as ctx:
    ctx.prec = 50
    price = (Decimal(sx) / Decimal(2) ** 96) ** 2
    tick_now = price.ln() / Decimal("1.0001").ln()
r0, r1 = amounts_at(tick_now)
print(
    f"now {tick_now.quantize(Decimal('0.001'))}: required {r0.quantize(Decimal('1'))} USDC /"
    f" {r1.quantize(Decimal('1'))} AAPLc vs minima {M0} / {M1}"
)

# Try public endpoints that may surface the revert data string.
import json as _json
import urllib.request

for endpoint in ("https://base.publicnode.com", "https://base.drpc.org"):
    try:
        request = urllib.request.Request(
            endpoint,
            data=_json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [{"to": NFPM, "from": SAFE, "data": calldata}, "latest"],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = _json.loads(response.read())
        print(endpoint, "->", str(body.get("error"))[:220])
    except Exception as error:  # noqa: BLE001
        print(endpoint, "request failed:", str(error)[:160])
