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

## Safe compatibility and canonical encoding

The canary proxy reports `VERSION()` as `1.4.1`, but its deployed master copy exposes an older ABI subset.
Only entry points verified read-only against this exact deployment are used.
In particular, `getGuard()` and `approvedHashes(address)` revert, despite the reported version.

Owner signatures are proven with `checkSignatures(bytes32,bytes,bytes)`, selector `0x934f3a11`.
The similarly named `checkNSignatures(bytes32,bytes,bytes)`, selector `0x9c546ffd`, does not exist on this master copy and reverts as an unknown selector.
The validation calldata uses the stored Safe threshold and canonically encodes the Safe transaction hash, empty data bytes, and the 65-byte owner signature.

The outer call uses `execTransaction(address,uint256,bytes,uint8,uint256,uint256,uint256,address,address,bytes)`, selector `0x6a761202`.
Its ten-word ABI head follows parameter order exactly: `to`, `value`, the dynamic `data` offset, `operation`, `safeTxGas`, `baseGas`, `gasPrice`, `gasToken`, `refundReceiver`, and the dynamic `signatures` offset.
Golden tests bind both complete encodings byte for byte to canonical `cast calldata` vectors.

Delivery transactions are type-2 EOA transactions whose both fee parameters sit at the observed gas price, which preflight has already bounded at or below the one-gwei cap.
The relaying EOA, not the Safe, pays the delivery gas, and the attempt refuses if the EOA cannot afford the buffered gas cost.
If the base fee rises above the capped price after signing, the transaction simply cannot be included and the bounded receipt wait fails closed instead of overpaying.

## Keychain setup

The signing key is the bot EOA owner of the Safe (`0x0c49cc4D53423CCd6be2Bcf115a25F418649C5C9`).
Store it once in the macOS Keychain:

```bash
security add-generic-password -s bot-signing-key -a aero-bot -w
```

Enter the 64-character hex private key at the interactive prompt (the leading `0x` is optional) so the secret never lands in shell history.
The existing canary item uses service `bot-signing-key` and account `aero-bot`.
The code defaults remain `aero-bot` and `bot-key`, so canary commands must set `AERO_BOT_KEYCHAIN_SERVICE=bot-signing-key` and `AERO_BOT_KEYCHAIN_ACCOUNT=aero-bot` as shown below.
The keychain module reads the secret at runtime through the absolute `/usr/bin/security` tool path, never logs or caches it, and reports only the derived public address.
A missing item, an empty secret, a non-hex secret, and the all-zero placeholder each fail closed with distinct diagnostics.

The relayer EOA `0x0c49cc4D53423CCd6be2Bcf115a25F418649C5C9` pays delivery gas.
Its live balance was verified as 0.001 ETH on 2026-09-07, which is ample for the first capped canary at the observed gas price.
The Safe separately held 0.0001 ETH, above the executor's fixed 0.00005-ETH Safe-balance floor.
Recheck both public balances before a later broadcast if funds may have moved.

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

   The ephemeral key is not a Safe owner, so `checkSignatures` honestly reports the signature as rejected and the on-chain gas estimates revert for the same reason.
   That is the expected proof that validation runs against the live contract, not a local simulation of it.

3. Dry-run with the real Keychain key.
   Both signatures must verify, and the nonce-4 approval must have a gas estimate:

   ```bash
   AERO_BOT_KEYCHAIN_SERVICE=bot-signing-key AERO_BOT_KEYCHAIN_ACCOUNT=aero-bot uv run aero-bot-swap dry-run --symbol AAPLc --amount 1
   ```

   When the standing allowance is zero and the live Safe nonce is 4, the dry run also builds the swap for nonce 5 without first executing the approval.
   `checkSignatures` can still accept that nonce-5 signature because it validates the supplied hash, but `execTransaction` gas estimation must hash with the current on-chain nonce and therefore honestly reverts with `GS026`.
   This nonce-5 estimate failure is expected until the nonce-4 approval is executed.
   The `execute` flow broadcasts and confirms the approval first, re-reads the advanced nonce, and only then builds and estimates the swap.

4. Execute, with the explicit broadcast confirmation:

   ```bash
   AERO_BOT_KEYCHAIN_SERVICE=bot-signing-key AERO_BOT_KEYCHAIN_ACCOUNT=aero-bot uv run aero-bot-swap execute --symbol AAPLc --amount 1 --confirm-broadcast
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

The proofs below ran against `https://mainnet.base.org`, and neither broadcast anything.

The live quote:

```
$ uv run aero-bot-swap quote --symbol AAPLc --amount 1
AAPLc pool 0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0 (snapshot block 50993968)
1.000000 USDC -> ~0.00311062 AAPLc at 321.478341268125192304907480293773237985140162017882942076052 USDC per AAPLc
amountOutMinimum 310750 raw units, conservative impact bound 0.000001, quote age 0s
```

The live real-key dry run after the canonical Safe ABI fixes:

```
AERO_BOT_KEYCHAIN_SERVICE=bot-signing-key AERO_BOT_KEYCHAIN_ACCOUNT=aero-bot uv run aero-bot-swap dry-run --symbol AAPLc --amount 1
AAPLc pool 0xa3b1e3f9747065e2073722ff4c9027d3ea4994f0 (snapshot block 50996276)
1.000000 USDC -> ~0.00311781 AAPLc at 320.736995921186044375877103304100615852327524247391926709792 USDC per AAPLc
amountOutMinimum 311469 raw units, conservative impact bound 0.000001, quote age 2s
safe 0xb69ab6c7e73f711d5f2d10fed8f0d09b1d028c28, relayer 0x0c49cc4d53423ccd6be2bcf115a25f418649c5c9 (Keychain key, nothing broadcast)
gas price 6000000 wei, Safe ETH 100000000000000 wei, standing allowance 0 raw USDC
approval: safeTxHash 0x1b77bce2f17a8000c12becece21770c71ead4c94fab106a50f6ac44034e1a712 (nonce 4)
approval: target 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913, calldata digest 0xdc3cf422d864f693704b3d8c1a9bf53592b3a197c4c58fffe774fe16a36d7922
approval: signature accepted by live checkSignatures, 96880 gas estimated
swap: safeTxHash 0xadfb38e1ff9ec622f2e9c5f2c5aadc0b748eb2f069ad30c9360fb1392a1d35b3 (nonce 5)
swap: target 0xcaf22ce31298cf2bf1d152862f80216478ad7c67, calldata digest 0x2f2076a6212ff51ea40429d04b248c5f5a3f12bf49bc2e4e45a41d16894c6e27
swap: signature accepted by live checkSignatures, no estimate: the on-chain estimate reverted: RPC call reverted: execution reverted: GS026
build took 183701.170 ms
```

What the dry run proved against the live chain:

- Live Sugar discovery pinned a fresh snapshot (block 50,996,276) and priced the swap through the real AAPLc pool.
- The Safe nonce read live as 4, so the approval was built for nonce 4 and the swap for nonce 5.
- The standing USDC allowance read as exactly zero, so the bounded 20-USDC approval was sequenced before the swap.
- The observed gas price was 6,000,000 wei (0.006 gwei), far below the one-gwei cap.
- The Safe's live ETH balance read as exactly 1e14 wei (0.0001 ETH), above the 5e13-wei floor.
- The live `checkSignatures` accepted both genuine owner signatures.
- The corrected nonce-4 approval calldata succeeded under read-only `execTransaction` and estimated at 96,880 gas with and without an explicit funded `from` address.
- The nonce-5 swap estimate honestly reverted with `GS026` while the contract nonce remained 4, as expected before approval inclusion.
- The relayer's public balance read as exactly 1e15 wei (0.001 ETH).
- Nothing was broadcast.

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
