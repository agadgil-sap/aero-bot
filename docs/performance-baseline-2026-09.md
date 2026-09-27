# Measured-edge baseline, 2026-09

Provenance: EXTRACTED DATA (firstmate, read-only from the VM audit chain, 2026-09-17 to 2026-09-27).
Every number in this document is restated verbatim from that extraction; nothing here was re-verified, re-derived, or extended beyond it.

## 1. The headline finding: the yield engine ran at roughly half throttle

Headline time-in-range across the names is 39-72 percent, and MSTRc - the dominant holding - sat in range 55 percent of its cycles.
Income in this programme is qualifying APR multiplied by time in range, so the in-range discipline and the allocator are not niceties: they are the largest measured lever in the programme.
Every in-range point left on the table is captured APR that the position's qualifying rate promised and the clock never collected.
The single-name book also held one position throughout the window, so this lever was exercised on exactly one pool at a time.

## 2. Daily marks

Last cycle per day, restated verbatim from the extraction.

| Day | Equity (USDC) | PnL (USDC) |
| --- | --- | --- |
| 09-23 | 99.11 | +18.38 |
| 09-24 | 93.82 | -3.00 (day-start anchored 80.73) |
| 09-25 | 103.68 | +10.27 |
| 09-26 | 102.90 | -0.61 |
| 09-27 | 102.75 | -0.75 |

Position value ran ~57-82 USDC throughout the window.
The book ran from ~90 at the start of the window to ~103 at the end.
One ~18 percent economic swing crossed the 09-23/24 anchor boundary; the 09-24 PnL line is day-start anchored at 80.73.

## 3. Time-in-range per symbol

Cycles, percent in range, and non-hold actions per name, restated verbatim from the extraction.

| Symbol | Cycles | % in range | Non-hold actions |
| --- | --- | --- | --- |
| MSTRc | 1459 | 55% | 223 |
| SNDKc | 378 | 65% | 64 |
| AAPLc | 239 | 72% | 10 |
| METAc | 172 | 42% | 28 |
| AMZNc | 70 | 50% | 22 |
| MSFTc | 33 | 39% | 9 |
| SPCXc | 14 | 93% | 1 |

SPCXc's 93 percent sits on only 14 cycles and is not a headline number; the materially-sampled names define the 39-72 percent range above.
A single position was live throughout the window - these rows are sequential, never concurrent.

## 4. What this implies for the allocator

The measured lever is in-range time, so the allocator's design should treat in-range percentage as the primary output it exists to move.
Tiered allocation follows directly: names that hold range deserve the deeper tier, and the tier boundary should be drawn on measured in-range discipline rather than APR alone.
Count should be treated as an output of qualification, not an input - the captain's 5-10 positions at the proven ~100-per-position scale imply a book built from names that have individually proven they can hold range.
Concentration bounds follow the same evidence: MSTRc took the dominant share of a single-name window at 55 percent in-range, which argues for bounds that stop one name from dominating both the book and the error budget.
Cash is dry powder for APR spikes, and the half-throttle finding says idle cash is only justified when it is waiting for a qualifying APR that outruns the in-range time it costs.

## 5. Known measurement caveats

Claimable-fees readings are checkpoint-stale over this window; gnhf 32 fixes that measurement, so pre-fix fee numbers must not be read as live income.
Days 09-17 through 09-22 are pre-schema, so the schema-era records - the daily marks above - begin 09-23.
This was a single-name era: one position throughout, so name diversification is entirely unmeasured by this window.

## 6. What the attribution window must confirm

The 2-3 day attribution window starting at the gnhf 32 deploy must confirm that fee readings are now live rather than checkpoint-stale, so income can be attributed at all.
It must confirm the income identity on fresh measurements: that realized income tracks qualifying APR multiplied by time in range, name by name.
And it must confirm whether in-range time under the allocator matches, beats, or falls short of the 39-72 percent single-name baseline this document records.
