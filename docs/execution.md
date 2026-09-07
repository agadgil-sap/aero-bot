# Capped manual swap execution

## Purpose and containment posture

The `aero-bot-swap` command is the application's only signing and broadcast path, and it exists solely for the canary phase of the emissions-farming program: manually triggered, hard-capped USDC-to-B20 stock swaps executed through the Safe smart account on Base.
Containment is the design center, not a property that emerges from responsible use.

- There is no loop, scheduler, watcher, or policy-driven trigger anywhere in the execution module.
  Every swap begins with one human-typed CLI command.
- The policy engine is never wired to execution.
  Its decisions remain pure HTTP-exposed decision code.
- Broadcast code is reachable only through the explicit `execute` subcommand, and even then only with the additional `--confirm-broadcast` flag.
- Every hard cap below is enforced in code before anything is signed.
- The default mode is `dry-run`: build, validate, and report without ever broadcasting.
- The bot private key is read from the macOS Keychain at runtime and never from the repository, environment files, arguments, or logs.
  Key bytes sign exactly one SafeTx hash per transaction plus the outer delivery transaction and are never stored, logged, or persisted.
- The API and dashboard remain structurally unable to sign or broadcast anything.

## Hard caps

| Cap | Default | Ceiling | Enforcement |
| --- | --- | --- | --- |
| Per-swap amount | 1.00 USDC | 5.00 USDC (validator rejects above) | Before quoting |
| Standing USDC approval | 20 USDC | 20 USDC (validator rejects above; never infinite) | Bounded `approve` calldata |
| Router whitelist | `0xcAF22ce31298CF2BF1D152862F80216478ad7c67` | Exactly this one address | Model validator |
| Token whitelist | Native USDC plus the official B20 registry | No other token | Quote gate |
| Pool source | Live Sugar discovery only | No configured address | Quote gate |
| Gas price | refuse above 1 gwei effective | Fixed in code | Preflight, before signing |
| Safe ETH floor | 5e13 wei (0.00005 ETH) | Fixed in code | Preflight, before signing |
| Quote staleness | 120 seconds | Fixed in code | Quote gate |
| Slippage tolerance | 0.1 percent | 1 percent (validator rejects above) | `amountOutMinimum` floor |
| Modeled impact | refuse when the conservative reserve-based bound reaches the tolerance | - | Quote gate |
| Receipt wait | 120 seconds, 2-second polls | Fixed in code | Fail-closed timeout |

The router is Aerodrome's universal router, and the swap calldata is modeled byte for byte on the Safe's already-executed reference transaction: one `execute` call carrying a single `V3_SWAP_EXACT_IN` command with the 43-byte concentrated-liquidity path `USDC || 0x08 || tickSpacing(uint16) || stock` and a deadline eight minutes past build time.
The offline test suite reproduces the reference calldata byte for byte as a golden vector.
The approval the reference transaction preceded its swap with was exact, so the standing allowance here is the bounded 20-USDC number and never the infinite maximum approval.

Delivery transactions are type-2 EOA transactions whose both fee parameters sit at the observed gas price, which preflight has already bounded at or below the one-gwei cap.
The relaying EOA, not the Safe, pays the delivery gas, and the attempt refuses if the EOA cannot afford the buffered gas cost.
If the base fee rises above the capped price after signing, the transaction simply cannot be included and the bounded receipt wait fails closed instead of overpaying.

## Keychain setup

The signing key is the bot EOA owner of the Safe (`0x0c49cc4D53423CCd6be2Bcf115a25F418649C5C9`).
Store it once in the macOS Keychain:

```bash
security add-generic-password -s aero-bot -a bot-key -w
```

Enter the 64-character hex private key at the interactive prompt (the leading `0x` is optional) so the secret never lands in shell history.
The service and account names default to `aero-bot` and `bot-key` and can be overridden with `AERO_BOT_KEYCHAIN_SERVICE` and `AERO_BOT_KEYCHAIN_ACCOUNT`.
The keychain module reads the secret at runtime through the absolute `/usr/bin/security` tool path, never logs or caches it, and reports only the derived public address.
A missing item, an empty secret, a non-hex secret, and the all-zero placeholder each fail closed with distinct diagnostics.

## Canary procedure

The Safe address defaults to the canary deployment `0xB69ab6C7E73F711D5f2d10feD8f0d09B1D028C28`; set `AERO_BOT_SAFE_ADDRESS` to override it for a different deployment.

1. Quote without building anything:

   ```bash
   uv run aero-bot-swap quote --symbol AAPLc --amount 1
   ```

2. Dry-run with a throwaway key.
   This exercises discovery, preflight, both Safe-transaction builds, and the live read-only signature check without reading the Keychain and without broadcasting anything:

   ```bash
   uv run aero-bot-swap dry-run --symbol AAPLc --amount 1 --ephemeral-key
   ```

   The ephemeral key is not a Safe owner, so `checkNSignatures` honestly reports the signature as rejected and the on-chain gas estimates revert for the same reason.
   That is the expected proof that validation runs against the live contract, not a local simulation of it.

3. Dry-run with the real Keychain key.
   The signature must verify and both gas estimates must succeed before the next step:

   ```bash
   uv run aero-bot-swap dry-run --symbol AAPLc --amount 1
   ```

4. Execute, with the explicit broadcast confirmation:

   ```bash
   uv run aero-bot-swap execute --symbol AAPLc --amount 1 --confirm-broadcast
   ```

Exit codes are zero on success, two on any pre-sign refusal (caps, whitelists, staleness, gas, floors, signature rejection), and one on other failures.
Every command accepts `--json` to print the complete typed report instead of the human summary.

## What each attempt audits

Every attempt appends its events to the local immutable audit chain before the CLI prints anything.
No event carries key material; the audit payloads are validated models whose field names are rejected outright if they could hold credentials or signed transactions.

- `execution_quote`: mode, symbol, token and pool addresses, snapshot block, USDC input, expected output, spot price, minimum output, modeled impact, quote age.
- `execution_built`: per built transaction (approval, swap), the SafeTx hash, target, keccak digest of the complete execTransaction calldata, Safe nonce, USDC amount, deadline, signature verdict, gas estimate, build duration.
- `execution_sent`: per broadcast, the role, SafeTx hash, delivery transaction hash, relayer public address, Safe address.
- `execution_confirmed` / `execution_failed`: the receipt's status, block, gas used, effective gas price, realized stock units decoded from the pool's Swap log, quote-versus-realized slippage fraction, inclusion duration, and a diagnostic on failure.

## Live read-only verification, 2026-09-07

Both proofs below ran against `https://mainnet.base.org` with no key beyond the dry run's throwaway key, and neither broadcast anything.

The live quote:

```
$ uv run aero-bot-swap quote --symbol AAPLc --amount 1
AAPLc pool 0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0 (snapshot block 50993968)
1.000000 USDC -> ~0.00311062 AAPLc at 321.478341268125192304907480293773237985140162017882942076052 USDC per AAPLc
amountOutMinimum 310750 raw units, conservative impact bound 0.000001, quote age 0s
```

The live ephemeral-key dry run:

```
$ uv run aero-bot-swap dry-run --symbol AAPLc --amount 1 --ephemeral-key
AAPLc pool 0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0 (snapshot block 50994064)
1.000000 USDC -> ~0.00311061 AAPLc at 321.480021594103644848504403222038906477704607707956905855623 USDC per AAPLc
amountOutMinimum 310749 raw units, conservative impact bound 0.000001, quote age 0s
safe 0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28, relayer 0x1d4ecfcaffe29981d1b55e4533cef2400ffe17a5 (ephemeral key, nothing broadcast)
gas price 6000000 wei, Safe ETH 100000000000000 wei, standing allowance 0 raw USDC
approval: safeTxHash 0x1b77bce2f17a8000c12becece21770c71ead4c94fab106a50f6ac44034e1a712 (nonce 4)
approval: target 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913, calldata digest 0x68e5b99d2f11a9235c2a2f9971b31454efa5156a887ae42088553bab853965fe
approval: signature REJECTED by live checkNSignatures, no estimate: the on-chain estimate reverted: RPC call reverted: execution reverted
swap: safeTxHash 0xe57bbc39126b2840138ffb448afd66cda3f8ae5e2a176637f640fc05d698055f (nonce 5)
swap: target 0xcaf22ce31298cf2bf1d152862f80216478ad7c67, calldata digest 0xc82ad37b423cd31cab9a051706e9364adc8eb44a4aefa5c2f348fde5001d7a39
swap: signature REJECTED by live checkNSignatures, no estimate: the on-chain estimate reverted: RPC call reverted: execution reverted
build took 187771.523 ms
```

What the dry run proved against the live chain:

- Live Sugar discovery pinned a fresh snapshot (block 50,994,064) and priced the swap through the real AAPLc pool.
- The Safe nonce read live as 4, so the approval was built for nonce 4 and the swap for nonce 5.
- The standing USDC allowance read as exactly zero, so the bounded 20-USDC approval was sequenced before the swap.
- The observed gas price was 6,000,000 wei (0.006 gwei), far below the one-gwei cap.
- The Safe's live ETH balance read as exactly 1e14 wei (0.0001 ETH), above the 5e13-wei floor.
- The live `checkNSignatures` honestly rejected the ephemeral signature, and the on-chain gas estimates reverted for the same owner check, proving both validations run against the real contract.
- Nothing was broadcast.

The two attempts appended four events to the local audit chain (`execution_quote`, `execution_quote`, `execution_built` for the approval, `execution_built` for the swap), after which the complete chain verified.

## Refusal catalog

Every refusal below exits with code two and prints an actionable explanation on stderr, and none of them sign or broadcast anything.

- Non-positive or above-cap amounts, and amounts finer than USDC's six decimals.
- Symbols outside the official B20 registry, an unverified registry, no Sugar-verified pool for the symbol, or a snapshot without observation evidence.
- Snapshots older than the staleness bound.
- Swaps whose conservative impact bound reaches the slippage tolerance, or that would deliver less than one whole stock unit.
- Gas price above the cap, Safe ETH below the floor, a relayer that cannot afford the buffered gas cost, or a missing gas estimate.
- Any router outside the single-address whitelist, rejected at policy construction.
- A Safe signature the live contract rejects, or a Safe nonce that advanced mid-attempt.
- `execute` without `--confirm-broadcast`.

## Approximations and honest limits

- The quote prices at the snapshot spot price; the realized output depends on the pool state at inclusion, which the `amountOutMinimum` floor and the receipt's decoded realized amount and slippage fraction bound and report.
- The modeled impact bound is the conservative constant-product bound against the pool's whole USDC reserve; a concentrated active range concentrates liquidity, so the true impact of these sizes is lower.
- The realized stock amount is decoded from the pool's own `Swap` event in the delivery receipt; a confirmed swap whose log is absent reports no realized amount rather than a guess.
