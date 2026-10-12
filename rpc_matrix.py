#!/usr/bin/env python3
"""Which RPC methods each chain actually has, and what they are called there. Read-only.

Role: file (entry point; the operator runs this)
Reads: chains/rpc_matrix.py and the three per-chain authorities it joins. NO network, no
      daemon, no database, no .env -- every cell comes from a release tag's own source.
Writes: nothing
Can move funds: no. It prints a table of method names.
Mainnet-safe: yes. It contacts nothing, so it cannot touch a wallet of any kind.

WHY THIS EXISTS. Operator, 2026-10-12: "map all commands for any chain we have and
create a cross-reference matrix of equivalence too" / "combine and extend them", after
the fourth time this session reasoned about Gridcoin as though it were Bitcoin Core and
had to be corrected by its source.

IT PRINTS THE DISAGREEMENTS FIRST. A row where BTC, LTC and GRC all say PRESENT needs no
attention and there are dozens of them; a row where they differ is a method somebody will
call on the wrong chain and find out from a broad `except`. Rule 14's "make 'did nothing'
look different from 'did work'", applied to a table: the useful half is the short half.

THE ROWS ARE DERIVED, NOT TYPED. `--called` greps this tree for every `.call("...")` an
adapter can reach, which is the denominator that matters and the one that cannot go
stale. `--all` prints every method any authority knows, which is bigger and is for
reading rather than for acting.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from chains import gridcoin_rpc_commands, rpc_matrix  # noqa: E402
from chains.rpc_matrix import ASSETS, cell, disagreements  # noqa: E402

#: Where to look for `.call("method")`. The adapters and everything that reaches them --
#: NOT regtest/, which drives daemons the harness starts itself and whose versions it
#: controls. A regtest-only method is a different risk and saying so keeps the live list
#: short enough to read.
LIVE_DIRS = ("chains", "modules", "services", "workers", "routes")

#: A Bitcoin-style method name is lowercase; Solana's are camelCase and are the NATIVE
#: names chains/solana.py sends, not Bitcoin-style names to be mapped. Filtering them out
#: here rather than listing them keeps the rule in one place: if it has a capital, it is
#: already a native name and this matrix has nothing to say about it.
#: WHERE A METHOD NAME CAN APPEAR, and there are TWO shapes because making this matrix
#: load-bearing created the second one.
#:
#: MEASURED THE MOMENT IT HAPPENED, 2026-10-12. chains/base._any_wallet_address() used to
#: read `.call("getaddressesbylabel")`; it now asks rpc_matrix.cell() for the name THIS
#: chain has and calls `.call(fallback.equivalent, ...)`. So the literal vanished from the
#: scan, `getaddressesbylabel` dropped out of the row set, and
#: tests/test_rpc_matrix.py::test_the_disagreement_list_is_SHORT_and_holds_the_real_hazards
#: failed -- because FIXING a hazard had removed it from the hazard list.
#:
#: That is the worst available direction for a report to move in, and it is the shape
#: rule 19's "does it stop the symptom being reported, or stop the cause existing" is
#: about: the symptom had stopped being reported and the method is still absent from
#: Gridcoin. So a `cell(asset, "method")` lookup IS a call site in the sense that matters
#: -- it is where the code names the method -- and the scan reads both.
_CALL = re.compile(
    r'\.call\(\s*"([a-z][a-z0-9_]*)"'
    r'|\bcell\(\s*[^,]+,\s*"([a-z][a-z0-9_]*)"'
)


def called_methods(dirs: tuple[str, ...] = LIVE_DIRS) -> list[str]:
    """Every Bitcoin-style method name `.call()`ed under `dirs`. Sorted, deduplicated."""
    found: set[str] = set()
    for name in dirs:
        root = REPO_ROOT / "swap_terminal" / name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            # findall returns a TUPLE per match because the pattern has two groups, one
            # per shape, and only one of them is ever set. Filtering the empties is what
            # turns that into method names.
            for groups in _CALL.findall(path.read_text(encoding="utf-8")):
                found.update(name for name in groups if name)
    return sorted(found)


def all_known_methods() -> list[str]:
    """Every method any authority in the join knows about. For reading, not for acting."""
    known = set(gridcoin_rpc_commands.COMMANDS) | set(gridcoin_rpc_commands.CORE_ONLY)
    known |= set(rpc_matrix.ARRIVED_IN) | set(rpc_matrix.REMOVED_IN) | set(rpc_matrix.ANCIENT)
    return sorted(known)


def table_lines(methods: list[str]) -> list[str]:
    """The matrix, one row per method, as report lines."""
    width = max([len("method")] + [len(m) for m in methods]) + 2
    lines = [f"{'method':<{width}}" + "".join(f"{asset:<10}" for asset in ASSETS)]
    lines.append("-" * (width + 10 * len(ASSETS)))
    lines.extend(
        f"{method:<{width}}" + "".join(f"{cell(asset, method).state:<10}" for asset in ASSETS)
        for method in methods
    )
    return lines


def disagreement_lines(methods: list[str]) -> list[str]:
    """Only the rows where the three Bitcoin-style chains do NOT agree, with the reason.

    THE REASON IS THE AUTHORITY'S OWN SENTENCE, never a paraphrase -- the same rule
    chains/daemon_capabilities.refusal_remedy() follows, and for the same reason: a second
    copy of "Gridcoin has no fundrawtransaction" is a second thing to keep true.
    """
    lines = []
    for method in methods:
        summary = disagreements(method)
        if not summary:
            continue
        lines.append(f"  {method}")
        lines.append(f"      {summary}")
        for asset in ("BTC", "LTC", "GRC"):
            entry = cell(asset, method)
            if entry.state in (rpc_matrix.ABSENT, rpc_matrix.RENAMED, rpc_matrix.UNKNOWN):
                where = f" -> call `{entry.equivalent}`" if entry.equivalent else ""
                lines.append(f"      {asset}: {entry.why}{where}")
    return lines


def run(args) -> int:
    """Orchestration only: choose the rows, print (rule 10)."""
    methods = all_known_methods() if args.all else called_methods()
    scope = (
        "every method any authority in the join knows"
        if args.all
        else f"every Bitcoin-style method this tree .call()s under {', '.join(LIVE_DIRS)}"
    )
    print("rpc matrix -- what each chain actually has, from release sources. Contacts nothing.")
    print(f"  rows            {len(methods)}: {scope}")
    print(f"  GRC source      {gridcoin_rpc_commands.SOURCE_URL}")
    print(f"                  {len(gridcoin_rpc_commands.COMMANDS)} commands at tag "
          f"{gridcoin_rpc_commands.SOURCE_TAG}, read off its own dispatch table")
    print("  BTC / LTC       Bitcoin Core release history. "
          f"BTC=Core {rpc_matrix.CORE_BASE['BTC'][0]}.{rpc_matrix.CORE_BASE['BTC'][1]}, "
          f"LTC=Core {rpc_matrix.CORE_BASE['LTC'][0]}.{rpc_matrix.CORE_BASE['LTC'][1]} "
          "(Litecoin 0.21.4 is forked from it)")
    print("  SOL / XRP       chains/solana_rpc_map.py and chains/xrp_rpc_map.py, unchanged")
    print(f"  ICP             {rpc_matrix.NOT_RPC} -- {rpc_matrix.ICP_WHY[:68]}...")
    print("  NOT a claim     about any BINARY. Every cell is a release tag's source or Core's")
    print("                  release history, and a daemon can be built from anywhere.")
    print()

    disagreeing = disagreement_lines(methods)
    print("  WHERE THE THREE BITCOIN-STYLE CHAINS DISAGREE -- the half worth reading:")
    for line in disagreeing or ["  (none) -- BTC, LTC and GRC agree on every row above"]:
        print(line)
    print()
    for line in table_lines(methods):
        print(f"  {line}")
    # EXIT 0 ALWAYS. This reports; it does not judge. A disagreement is a FACT about the
    # chains, not a failure of this host -- unlike pair_verdicts.py, whose exit code is a
    # verdict about a desk. Making this non-zero would make `rpc_matrix.py && something`
    # unusable for the thing it is for.
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Which RPC methods each chain has, and what they are called there. "
                    "Read-only; contacts nothing. Always exits 0 -- it reports facts about "
                    "the chains, not a verdict about this host.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="every method any authority knows, rather than only the ones this tree calls. "
             "Bigger, and for reading rather than for acting.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
