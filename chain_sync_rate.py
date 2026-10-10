#!/usr/bin/env python3
"""How fast a Bitcoin-family daemon is catching up, and WHAT is holding it back.

Role: diagnostics (read-only)
Reads: getblockchaininfo and getnettotals on the SERVICE's configured daemon for
       each chain named, sampled several times over a window
Writes: nothing
Can send orders: no
Live-safe: yes. Every call is a read, it opens no wallet, and it is safe against a
       daemon that is mid-sync -- which is the only state it is interesting in.

=============================================================================
WHY THIS FILE EXISTS: I ASSERTED A CAUSE I HAD NOT MEASURED
=============================================================================

chains/daemon_network.sync_verdict() already answers "is this node caught up".
The operator's next two questions are "how long" and "can I do anything", and on
2026-10-10 I answered the second one wrong, in writing, twice.

A diagnostic I handed them printed:

    peers now  1   <- want 8+; one peer is why the sync is slow

"one peer is why" was an inference in the register of a measurement -- rule 17's
exact failure -- and they pasted that line back twice and acted on it. What the
measurements then showed, on LTC testnet at height ~3.1M of 4.9M:

    the conf             no connect=, no maxconnections=, no peer settings at all
    the command line     the same: three flags total, none of them peer-related
    known addresses      8219, from getnodeaddresses
    blocks validated     2153 in 30.0s   (71.7/s)
    bytes received       1,646,379       (0.055 MB/s)
    average block        0.8 kB on the wire

So one peer was what the node had ESTABLISHED, not what it was told to establish,
and that one peer was feeding it FASTER THAN IT COULD VALIDATE -- 0.055 MB/s is a
factor of 36 below the 2 MB/s floor. Adding peers would have changed little. The
lever is dbcache, which needs a restart and is therefore the operator's (rule 16).

Nothing in this tree measured any of that. Surveyed the same day: `getnettotals`
appeared in no file, and every reader of `blocks`/`headers`/`verificationprogress`
read a POSITION rather than a RATE, so no surface could tell a node that was
crawling from one that had stopped.

THE DECISION IS NOT IN THIS FILE. chains/daemon_network.sync_bottleneck() takes
samples and returns a verdict, so it can be driven with seeded numbers and no
sleeping; this file does the sampling, the printing, and nothing else (rule 10).

=============================================================================
WHY IT RESOLVES THE SERVICE'S DAEMON AND NOT A CONF
=============================================================================

SERVICE SIDE, registered in tests/test_daemon_conf.py. The question this answers
is "when will a deposit to this chain become visible", and the node that decides
that is the one the payout worker and deposit watcher talk to --
build_adapters(Config.RPC), the same construction the service makes. A conf
fallback resolving some other daemon would report a sync that has nothing to do
with whether the terminal can see money, which is the quietest kind of wrong
answer: a correct-looking ETA for the wrong node.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from chains.daemon_network import (
    SYNC_NOT_ADVANCING,
    SYNC_RATE_NOT_ESTABLISHED,
    SYNC_SYNCED,
    sync_bottleneck,
    sync_verdict,
)
from chains.registry import build_adapters, why_unconfigured
from config import Config
from microfortnights import format_duration

#: The chains this can measure. Bitcoin-family only, because getnettotals and
#: getblockchaininfo are theirs -- XRP, SOL and ICP have no equivalent pair and a
#: row that printed "(none)" for them would imply the question had been asked.
CHAINS = ("BTC", "LTC")

#: Sampling defaults. Five samples across a minute is enough to separate a rate
#: from jitter, and short enough that an operator will actually wait for it.
DEFAULT_SAMPLES = 5
DEFAULT_GAP_SECONDS = 15.0

#: The two reads, named once. getnettotals is the one no other file in this tree
#: calls, and it is the one that makes the verdict possible: without bytes there is
#: no way to tell a slow link from a busy CPU.
CHAIN_INFO = "getblockchaininfo"
NET_TOTALS = "getnettotals"


def sample(node) -> tuple[tuple | None, str]:
    """One (elapsed, blocks, headers, bytes_received) reading, or (None, why).

    BOTH CALLS OR NEITHER. A reading with a block height and no byte count would
    silently become a sample that cannot answer the question -- sync_bottleneck()
    would refuse it for the wrong reason ("bad tuple shape") and the operator would
    read that as a bug in this file rather than as a daemon that stopped answering.
    """
    try:
        chain = node.call(CHAIN_INFO)
        net = node.call(NET_TOTALS)
    except Exception as exc:  # noqa: BLE001 -- checked: the reason is RETURNED, not swallowed; the caller prints it and the sample is dropped
        return None, f"{type(exc).__name__}: {exc}"
    try:
        return (time.monotonic(), int(chain["blocks"]), int(chain["headers"]),
                int(net["totalbytesrecv"])), ""
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"a field was missing or not a number: {type(exc).__name__}: {exc}"


def collect_samples(node, samples: int, gap: float) -> list[tuple]:
    """Take `samples` readings `gap` seconds apart, printing each as it arrives.

    PRINTS AS IT GOES rather than at the end, which is the whole of rule 14 for this
    file: the default window is a minute, and a minute of blank terminal is
    indistinguishable from a hang. The operator's own words on that, 2026-08-10: "i
    cannot stand to wait who knows how the fuck long on a blinking cursor. how do i
    know it's not hung or broken?"

    A FAILED SAMPLE IS ANNOUNCED, NOT SKIPPED. One that vanished silently would
    shorten the window without saying so, and the rate would then be computed over a
    span the reader believes was continuous.

    SEPARATE FROM report() BECAUSE ONLY THIS HALF NEEDS A DAEMON AND A CLOCK. ruff
    C901 flagged the combined version at 11, and rule 12's answer is to extract the
    decision rather than raise the ceiling -- so report() takes readings and nothing
    else, and its tests seed them instead of sleeping.
    """
    readings: list[tuple] = []
    for index in range(samples):
        if index:
            time.sleep(gap)
        reading, why = sample(node)
        if reading is None:
            print(f"    sample {index + 1}/{samples}  UNREADABLE  {why}", flush=True)
            continue
        readings.append(reading)
        _elapsed, blocks, headers, got = reading
        print(f"    sample {index + 1}/{samples}  blocks {blocks}  headers {headers}  "
              f"{headers - blocks} behind  recv {got:,}", flush=True)
    return readings


def report(asset: str, readings: list[tuple], attempted: int) -> int:
    """Render the verdict for one chain from its readings. No I/O but printing.

    `attempted` is how many samples were TRIED, so "answered none of 5" can be said
    rather than "no readings" -- the denominator rule 3 asks for, and the difference
    between a daemon that is down and a tool that never asked.
    """
    if not readings:
        print(f"    NOTHING WAS READ. {asset} answered none of {attempted} attempts, so there is")
        print("    no rate and no position -- the daemon is down or the credentials differ.")
        return 1

    # The static verdict too, because "already synced" makes the rate uninteresting
    # and an operator should not have to infer that from a tiny `behind`.
    last = readings[-1]
    state = sync_verdict({"blocks": last[1], "headers": last[2],
                          "initialblockdownload": last[2] - last[1] > 0})
    verdict = sync_bottleneck(readings)
    print()
    if verdict["state"] == SYNC_RATE_NOT_ESTABLISHED:
        print(f"    RATE NOT ESTABLISHED: {verdict['why']}")
        return 0

    window = readings[-1][0] - readings[0][0]
    print(f"    OVER {format_duration(window)}:")
    print(f"      blocks validated   {last[1] - readings[0][1]}  "
          f"({verdict['blocks_per_second']:.1f}/s)   out of {verdict['behind']} behind")
    print(f"      bytes received     {last[3] - readings[0][3]:,}  "
          f"({verdict['bytes_per_second'] / 1_000_000:.3f} MB/s)")
    if verdict["bytes_per_block"] is not None:
        print(f"      average block      {verdict['bytes_per_block'] / 1000:.1f} kB on the wire")
    print(f"\n    {verdict['state'].upper()}: {verdict['why']}")

    if verdict["state"] == SYNC_NOT_ADVANCING:
        return 0
    if state["state"] == SYNC_SYNCED:
        print("\n    AND IT IS ALREADY CAUGHT UP, so the rate above is this node keeping pace")
        print("    with new blocks rather than catching up. There is nothing to wait for.")
        return 0
    if verdict["eta_seconds"] is not None:
        print(f"\n    ETA at this rate   {format_duration(verdict['eta_seconds'])}"
              f"  = {verdict['eta_seconds'] / 3600:.1f} hours")
        print("    AN EXTRAPOLATION, NOT A FINISH TIME. Measured twenty minutes apart on")
        print("    2026-10-10 the rate fell 79.8 -> 71.7 blocks/s (6.3h -> 7.0h) because block")
        print("    density rises with height. Re-run it rather than trusting one reading.")
    return 0




def measure(asset: str, samples: int, gap: float) -> int:
    """One chain, end to end: resolve, sample, report. Orchestration and no decision."""
    node = build_adapters(Config.RPC).get(asset)
    print(f"\n=== {asset}")
    if node is None:
        print(f"    REFUSED    {why_unconfigured(asset, Config.RPC)}")
        print("    This reads the SERVICE's daemon with no conf fallback, so it is the node")
        print("    that decides whether a deposit is visible. Export the variables above.")
        return 1
    print(f"    rpc        {node.url}   (no credential is printed by this tool)")
    return report(asset, collect_samples(node, samples, gap), samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure a chain's sync rate and say what is limiting it.")
    parser.add_argument("--chain", action="append", default=[], metavar="ASSET",
                        help=f"a chain to measure (repeatable). Default: all of {', '.join(CHAINS)}")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help=f"readings per chain (default {DEFAULT_SAMPLES})")
    parser.add_argument("--gap-seconds", type=float, default=DEFAULT_GAP_SECONDS,
                        help=f"seconds between readings (default {DEFAULT_GAP_SECONDS}). "
                             f"SECONDS, not microfortnights: it is an interface, not a report "
                             f"(rule 6)")
    args = parser.parse_args(argv)

    chosen = [c.upper() for c in args.chain] or list(CHAINS)
    unknown = [c for c in chosen if c not in CHAINS]
    if unknown:
        print(f"chain_sync_rate.py: {', '.join(unknown)} is not measurable here. "
              f"getblockchaininfo and getnettotals are Bitcoin-family RPCs, so this tool "
              f"covers {', '.join(CHAINS)} only.")
        return 2

    per_chain = args.gap_seconds * max(0, args.samples - 1)
    print(f"chain_sync_rate.py: measuring {', '.join(chosen)}")
    print(f"  samples    {args.samples} per chain, {format_duration(args.gap_seconds)} apart")
    print(f"  wall time  {format_duration(per_chain)} per chain, "
          f"{format_duration(per_chain * len(chosen))} in total")
    print("  every call is a READ. This tool changes nothing, opens no wallet, and is safe")
    print("  to run against a syncing daemon.")

    worst = 0
    for asset in chosen:
        worst = max(worst, measure(asset, args.samples, args.gap_seconds))
    print()
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
