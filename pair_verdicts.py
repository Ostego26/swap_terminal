#!/usr/bin/env python3
"""Which of the six conditions is refusing each allowed pair, and how many pairs each one costs.

Role: file (entry point; the operator runs this, or `swap_stack.py pairs` runs it
      inside the container)
Reads: the process environment through config.Config, chains/registry.build_adapters(),
      and the `chain_reachability` table in Config.DB_PATH (read-only, one SELECT,
      through services/chain_reachability.reachability_from_path() -- which lived here
      as reachability_or_empty() for six hours on 2026-10-12 and moved to the module
      that owns the table the moment open_swap.py needed the same read).
      ONE NETWORK READ IS POSSIBLE and it is the page's own: a pair paying out in SOL
      that has passed every other condition reaches
      services/quote_service.why_cannot_establish_payout_floor(), which asks the
      cluster for its rent-exempt minimum and caches it for 600 seconds.
Writes: nothing. No row, no file, no chain.
Can move funds: no. It creates no quote and no swap, signs nothing, broadcasts
      nothing, and unlocks no wallet.
Mainnet-safe: yes

WHY THIS EXISTS, AND IT IS A QUESTION THAT COST THE OPERATOR A DAY.

2026-10-11, their screen, every coin:

    BTC  NONE  0/5 out      LTC  NONE  0/5 out      GRC  NONE  0/5 out
    SOL  NONE  0/5 out      XRP  NONE  0/5 out      ICP  NONE  0/5 out

    "nothing works in the UI"

Thirty directions, thirty refusals, and SIX DARK LAMPS IS ONE NUMBER. The customer
page is built to withhold the operator-facing remedy by design -- see
services/wizard.unavailable_note(), which exists because screen 2 was printing
"SOL_RPC_URL is unset (or 0) in the environment this process was started with" to an
unauthenticated reader on the public port -- so the surface that shows the problem is
deliberately the surface that cannot explain it. /admin can, one row at a time,
across thirty rows of a matrix. Neither answers the question an operator actually has
when every lamp is dark, which is "is this ONE cause or thirty".

It is almost always one, and which one decides what they go and do:

    missing           export settings and restart the container
    unreachable       start a daemon, or open a firewall rule
    cannot_pay        a signing decision, which is theirs (rule 16)
    cannot_take       set one shared-account variable
    cannot_quote      record a payout cost for that chain
    cannot_establish  a cluster that could not be asked

So this prints the thirty verdicts GROUPED BY CAUSE, with a count per condition out
of the number of pairs evaluated (rule 3: a count without its denominator has caused
real errors here more than once), and the sentence each authority gave.

IT HOLDS NO DECISION OF ITS OWN, which is the only reason it is allowed to exist
beside the page (rule 8). services/pair_view.allowed_pair_rows() is the single
evaluation -- the same call routes/atm.py makes for the page -- and
services/pair_view.refusals_by_cause() is the single list of causes. A second
evaluation here is the exact defect pair_view.py's own header records:
services/admin_view.pair_rows() once computed its own verdict and told the operator a
pair was ENABLED that the customer page was refusing in the same process.

AND IT PASSES THE REACHABILITY ROWS, which is the half that makes it match the page.
The page passes them (routes/atm.py:183). open_swap.py does not, so its catalog can
still call a pair available whose chain has not answered for ten minutes -- surfaced
to the operator rather than changed here, because "whether a swap may proceed at all"
is rule 16's fund-movement line and the change belongs to them.

WHY NOT A FLAG ON swap_readiness.py, which is the neighbouring tool. That one asks
the DAEMONS -- it polls the Gridcoin wallet, the XRP endpoint and the Solana cluster,
and it refuses outright to poll a mainnet Gridcoin wallet because looking is itself
the hazard there. This asks the SERVING PROCESS'S OWN VERDICT and makes at most the
one cached read the page makes. They answer different questions with different costs,
which is the distinction tests/test_two_readiness_verdicts_answer_different_questions.py
already exists to keep.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))
# .env BEFORE config, because config.Config is a class body evaluated once at import:
# a load afterwards changes nothing and looks like it worked. Measured 2026-10-12 --
# 2 of 34 root tools did this and 32 did not, so the same .env produced different
# adapters depending on which tool you ran. env_preamble's header has the measurement,
# why it is an import rather than a call, and the posture it states.
import env_preamble  # noqa: F401  # isort: skip  -- MUST stay above `from config import Config`

from config import Config
from microfortnights import format_duration
from network_target import startup_lines
from report_block import CONTINUATION, labeled, wrapped
from services.chain_reachability import reachability_from_path, row_age_seconds
from services.pair_view import REFUSAL_KEYS, allowed_pair_rows, refusals_by_cause
from stack_authority import pair_exit_code
from workers.common import build_adapters_from_config, get_config_dict

#: What each condition means for the PERSON READING, in one clause. Keyed by the same
#: constant pair_view evaluates, so a seventh condition shows up here as a KeyError in
#: a test rather than as a silently unexplained group on an operator's screen --
#: tests/test_pair_verdicts.py asserts the coverage is exhaustive.
#: How wide a cause sentence is wrapped to. The report's own width minus the
#: continuation indent, so a wrapped cause lines up under the first line of itself
#: rather than under the label column -- report_block.wrapped() does the same thing for
#: a LABELED value, and these are not labeled.
_CAUSE_WIDTH = 96 - len(CONTINUATION)

WHAT_TO_DO = {
    "missing": "export that chain's settings and restart the container",
    "unreachable": "start that daemon, or open the firewall between the container and it",
    "cannot_pay": "arming that chain's payout leg is a signing decision, and it is yours",
    "cannot_take": "set that chain's shared deposit-account variable",
    "cannot_quote": "record what a payout on that chain costs this desk",
    "cannot_establish": "that chain could not be asked for its own payout minimum",
}


def reachability_lines(rows: dict) -> list[str]:
    """One line per recorded chain: did it answer, how long ago, and what it said.

    AGE IS REPORTED BECAUSE STALENESS IS NOT A REFUSAL.
    services/chain_reachability.reachability_refusal() goes SILENT on a row older than
    STALE_AFTER_SECONDS, so a chain that is genuinely down but whose last reading has
    aged out does not appear as a cause anywhere in this report -- and a reader who
    cannot see the age has no way to notice that. Rule 14: echo the parameter that
    decides the answer.

    IN MICROFORTNIGHTS WITH SECONDS BESIDE (rule 6), because the number is read
    against ST_* settings an operator may want to edit, which are in seconds.
    """
    lines = []
    for asset in sorted(rows):
        row = rows[asset]
        age = row_age_seconds(row["checked_at"])
        when = (
            "age UNKNOWN -- checked_at did not parse, so this row refuses nothing"
            if age is None
            else f"{format_duration(age)} ago"
        )
        answered = "ANSWERED" if row["reachable"] else "did NOT answer"
        detail = f" -- {row['detail']}" if row["detail"] else ""
        lines.append(f"{asset}: {answered}, {when}, asked {row['asked']}{detail}")
    return lines


def condition_counts(rows: list[dict]) -> list[tuple[str, int]]:
    """How many pairs each condition is the refusing one for, in evaluation order.

    THE REFUSING ONE AND NOT "a failing one". pair_serviceability() short-circuits,
    so at most one key on a row is non-empty and the count is a partition of the
    unavailable pairs rather than six overlapping tallies that sum past the total.
    That is what makes the numbers addable, and a reader adding them is the whole
    point of printing them.

    EVERY CONDITION IS LISTED INCLUDING THE ZEROES, because "no pair is blocked by
    this" is a result and a missing row is not (rule 14). On the operator's 2026-10-11
    reading, five zeroes and one thirty is the entire answer to "nothing works in the
    UI", and the five zeroes are what makes the one number trustworthy.
    """
    return [
        (key, sum(1 for row in rows if row.get(key)))
        for key in REFUSAL_KEYS
    ]


def say_pair_table(rows: list[dict]) -> None:
    """Every evaluated pair, with the condition that refused it and nothing else.

    ONE LINE PER PAIR AND THE CONDITION'S NAME, not its sentence: the sentences are
    deduplicated into the GROUPED BY CAUSE block below, where one shared cause reads
    as one problem. Printing the full sentence thirty times is the output defect this
    file was written to replace.
    """
    for row in rows:
        refusing = next((key for key in REFUSAL_KEYS if row.get(key)), "")
        verdict = "AVAILABLE" if row["serviceable"] else f"blocked by {refusing or 'NOTHING NAMED'}"
        print(f"{CONTINUATION}{row['from_asset']:>4} -> {row['to_asset']:<4}  {verdict}", flush=True)


def run(args) -> int:
    """Orchestration only: read, evaluate once, print (rule 10)."""
    started = time.monotonic()
    db_path = args.db or Config.DB_PATH
    config = get_config_dict()

    # ANNOUNCE BEFORE, NOT ONLY AFTER (rule 14). build_adapters_from_config() opens no
    # socket, but the verdict below can make one cached cluster read per *->SOL pair,
    # and a reader watching a blinking cursor cannot tell working from hung.
    print(
        "pair verdicts -- services/pair_view.allowed_pair_rows(), the same call the "
        "customer page makes. Read-only.",
        flush=True,
    )
    print(labeled("database", db_path), flush=True)
    # ALWAYS PRINT THE NETWORK (rule 14: "a line that does not say mainnet or testnet
    # is a line that will eventually be read as the wrong one"). There is no single
    # Config.NETWORK to print -- a first draft of this file invented one and it would
    # have rendered "(not stated)" forever -- because each chain is pointed
    # separately. network_target.startup_lines() is the one place that classifies a
    # port into a network and is what app.py's own banner uses, so this is the same
    # sentence an operator already knows how to read.
    print(labeled("network", "per chain, classified by network_target.startup_lines():"), flush=True)
    for line in startup_lines(config.get("RPC") or {}):
        print(f"{CONTINUATION}{line}", flush=True)

    adapters = build_adapters_from_config()
    built = ", ".join(sorted(adapters)) or "(none) -- this process built no adapter at all"
    for line in wrapped("adapters here", built):
        print(line, flush=True)

    reachability, headline, detail = reachability_from_path(db_path)
    print(labeled("reachability", headline), flush=True)
    # THE DETAIL IS NOT FOLDED INTO THE LABEL LINE. db.connect_db()'s refusal is a
    # whole remedy block -- five `check` clauses and the 2026-10-10 measurement behind
    # them -- and wrapping it after "reachability" buried the five pair-verdict lines
    # that follow under a paragraph about a database path. It is kept in full, because
    # a mistyped SWAP_DB_PATH is exactly the thing that remedy is for, and printed
    # where a reader can skip it.
    for line in textwrap.wrap(detail, width=_CAUSE_WIDTH) if detail else []:
        print(f"{CONTINUATION}{line}", flush=True)
    for line in reachability_lines(reachability) or [
        "(none) -- no chain has a recorded reading, so the sixth condition refuses nothing"
    ]:
        print(f"{CONTINUATION}{line}", flush=True)

    rows = allowed_pair_rows(config, adapters, reachability)
    if not rows:
        print(
            labeled("verdict", "(none) -- Config.ALLOWED_PAIRS is empty, so there is no pair to "
                               "evaluate and six dark lamps would be correct"),
            flush=True,
        )
        return pair_exit_code("no_pairs")

    available = [row for row in rows if row["serviceable"]]
    unavailable = [row for row in rows if not row["serviceable"]]
    print(flush=True)
    print(
        labeled("available", f"{len(available)} of {len(rows)} allowed pairs can be quoted right now"),
        flush=True,
    )
    say_pair_table(rows)

    print(flush=True)
    print(labeled("blocked by", f"{len(unavailable)} of {len(rows)} pairs, by condition:"), flush=True)
    for key, count in condition_counts(rows):
        # THE DENOMINATOR BESIDE EVERY NUMERATOR (rule 3), and the clause saying what
        # to do about it, because a condition name alone sends the reader to grep.
        print(f"{CONTINUATION}{key:<18} {count:>3} of {len(rows)}  -- {WHAT_TO_DO[key]}", flush=True)

    print(flush=True)
    causes = refusals_by_cause(unavailable)
    print(labeled("causes", f"{len(causes)} distinct cause(s) behind those {len(unavailable)} pairs:"), flush=True)
    # THE AUTHORITIES' OWN SENTENCES, deduplicated by refusals_by_cause() -- so six
    # pairs sharing one down daemon read as one problem rather than six. `(none)` is a
    # result and is printed rather than left as a gap (rule 14); here it means the
    # pairs are unavailable and no condition named why, which is a defect upstream and
    # says so rather than reading as good news.
    for cause in causes or [
        "(none) -- pairs above are unavailable and no condition named one, which is a "
        "defect in services/pair_view.pair_serviceability()"
    ]:
        for line in textwrap.wrap(cause, width=_CAUSE_WIDTH) or [cause]:
            print(f"{CONTINUATION}{line}", flush=True)

    elapsed = time.monotonic() - started
    print(flush=True)
    print(labeled("took", format_duration(elapsed)), flush=True)
    # EXIT CODE IS THE VERDICT, not whether the report printed. 0 only when every
    # allowed pair can be quoted, so `swap_stack.py pairs` can be read by a script
    # and a report that printed thirty refusals cannot exit 0 (rule 13: "skipped"
    # plus "success" in one output is a defect in the output).
    #
    # AND THE FAILURE CODE IS 3 RATHER THAN 1, which is not cosmetic: this file died at
    # IMPORT on the operator's host (the system interpreter had no `bech32`), CPython
    # exited 1, and swap_stack's cmd_pairs() reported "at least one allowed pair cannot
    # be quoted" about thirty pairs nothing had evaluated. stack_authority.
    # PAIR_EXIT_CODES has the measurement and the reason 1 and 2 are left to CPython
    # and argparse.
    return pair_exit_code("all_quotable" if not unavailable else "some_unavailable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Why each allowed pair is unavailable, grouped by cause. Read-only; "
                    "exits 0 only when every allowed pair can be quoted.",
    )
    parser.add_argument(
        "--db", default="",
        help=f"database holding the chain_reachability rows (default: Config.DB_PATH, "
             f"currently {Config.DB_PATH}). It must be the file the workers write, or the "
             f"sixth condition is not considered and the report says so.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
