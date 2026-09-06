# Wallet-free transaction simulation

## First-release boundary

The application can create immutable unsigned transaction plans and submit them only to an injected read-only `eth_call` simulation backend.
It has no private-key input, keystore, seed phrase, signer, raw-transaction signing, or transaction broadcast path.
Wallet onboarding is visibly disabled in the local dashboard.
Successful simulation means only that the unsigned calls completed against one pinned Base block.
It is never permission or an instruction to execute them.

The default application is emergency-halted with empty transaction target allowlists and no simulation backend.
It therefore returns explicit blocked or unavailable diagnostics without making an RPC call.

## Exact allowance policy

Only the standard ERC-20 `approve(address,uint256)` function is supported by the current planner.
The token and encoded spender must both appear in immutable contract allowlists.
The planner never creates unlimited approval calldata.
It approves exactly the requested raw token quantity.
If the existing allowance is non-zero and differs from the request, the plan first resets it to zero before setting the replacement amount.
If the existing allowance already equals the request, the result is `no_action` and contains no transaction.

Every transaction has Base chain ID 8453 and zero native value.
Each plan is pinned to the block used for the public allowance evidence.
A deterministic integrity identifier covers the public sender, block, transaction order, targets, actions, values, and calldata.

## Simulation validation

Caller-supplied plans are never trusted because they cross an HTTP boundary.
Before any backend call, the application recalculates the plan identifier, verifies the public sender, checks the token allowlist, decodes the complete ABI payload, checks the spender allowlist, and enforces exact allowance semantics again.
The backend must return one observation for every transaction in order and at the plan's exact block.
A revert blocks the complete plan, and incomplete or mismatched evidence is rejected.
Backend unavailability never falls back to signing or broadcasting.

## Immutable planning evidence

Every exact-allowance planning response is appended to the local immutable audit chain before the API returns it.
The record includes the validated public request, complete active transaction policy, and exact blocked, no-action, or ready result.
Ready records contain only unsigned transaction plans and retain explicit false signing and broadcasting capabilities.
Audit integrity failure returns an evidence-backed service-unavailable response instead of an unaudited plan.
Every read-only simulation response is also appended before it is returned.
The simulation record binds the exact submitted unsigned plan and active revalidation policy to the complete result, including ordered backend observations when available.
Blocked, unavailable, rejected, reverted, and passed statuses share the same required durable boundary.
