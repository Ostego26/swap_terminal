#!/usr/bin/env python3
"""Does OP_CHECKLOCKTIMEVERIFY actually EXECUTE on Gridcoin? Measured, on the real testnet.

Role: file (the entry point; the decisions live in swap_terminal/regtest/adaptor_steps.py,
      modules/atomic_htlc_scripts.py and modules/htlc_rpc.py, and it holds none of its own)
Reads: the operator's Gridcoin TESTNET daemon over JSON-RPC, and ST_ADAPTOR_FUNDING_SEED
Writes: nothing on disk. It BROADCASTS on a test network.
Can move funds: YES -- on Gridcoin testnet, and structurally nowhere else.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED. The daemon must SAY it is on a test network
      before a byte is built, three ways, and an absence of evidence is treated as mainnet.
Live-safe: yes. It starts and stops NOTHING, and since 2026-09-28 the refund path no longer
      touches the wallet's lock state, so a staking-only wallet is left exactly as found.

THE GAP THIS CLOSES, and it is the one docs/branch_coverage.md graded NONE.

Three GRC swaps completed on 2026-09-27 and every one took the HASHLOCK branch.
`OP_CHECKLOCKTIMEVERIFY` sits in the `OP_ELSE`, which a hashlock spend never executes. So
what those swaps established is that Gridcoin ACCEPTS A SCRIPT CONTAINING the opcode in a
branch that does not run -- a real fact, and not the fact anybody needed. The refund branch
of a Gridcoin HTLC, the one an operator needs when a counterparty vanishes, had never been
executed at all.

WHY regtest_htlc_verify.py CANNOT DO THIS, measured rather than assumed. Its `--chain` offers
`btc`, `ltc`, `both`. Adding `grc` would not help: that harness MINES to the locktime, and

    contract_locktime("GRC", ROLE_INITIATOR, tip) == tip + 1920

is 1920 blocks at Gridcoin's 90s target -- **48 hours** of real waiting, on a chain with no
regtest mode and no `generateblock`, `generatetoaddress` or `submitblock` to shorten it.

SO THIS USES A SHORT TEST LOCKTIME, AND SAYS SO. `LOCKTIME_BLOCKS_AHEAD` below is six blocks,
not the production value. That is honest because of what is being measured: whether the
OPCODE runs and enforces. `contract_locktime()` decides HOW LONG to wait and is a separate
decision with its own unit tests; the number it returns does not change what CLTV does with
it. What this run must never be read as is a test of the production timelock -- it is not,
and the verdict says so.

HOW CLTV IS ISOLATED WITHOUT `generateblock`, which is the only interesting design decision
in this file. Gridcoin's refusal message is `-22 TX rejected` and names nothing, so "it was
refused" proves only "refused for some reason". Two different rules can refuse an early
refund and only one of them is CLTV:

    non-finality   a transaction whose nLockTime is in the future is not final, and the
                   mempool refuses it before any script runs. This says NOTHING about CLTV.
    CLTV           the script compares its own locktime against the transaction's nLockTime
                   and fails if the transaction's is lower.

The isolation is a refund whose nLockTime is the CURRENT TIP. That transaction is FINAL --
nLockTime is below the next block's height -- so the mempool's finality check passes and the
only thing left that can refuse it is the script. Its scriptSig, its input, its output and its
fee are the same as the accepted one's; they differ in the four bytes of nLockTime, and
`OP_CHECKLOCKTIMEVERIFY` is the only opcode in the script that reads them.

    8a  nLockTime = L,   height < L   REFUSED   non-final. Proves nothing about CLTV.
    8b  nLockTime = tip, height < L   REFUSED   FINAL, so only the script can refuse it.
    9   nLockTime = L,   height >= L  ACCEPTED  through the REAL GRCClient.refund_contract()

8b is the measurement. 8a and 9 are its controls: 8a shows the non-final refusal looks
identical on this chain, and 9 shows every other part of the spend is good.

THE REFUND IS DRIVEN THROUGH THE REAL CLIENT, not a harness copy. That is the point of a
verification harness (the behavioral-verification principle: run the real script, assert on
what the chain did) and it is newly possible: `GRCClient.refund_contract()` stopped calling
`ensure_fully_unlocked()` on 2026-09-28, so it no longer locks a staking wallet to do work
that never used the wallet.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from decimal import Decimal
from pathlib import Path

# The sys.path line must run before these imports: this repository's modules import each other
# rootlessly, which is CLAUDE.md rule 10's layout gap. E402 is ignored repo-wide for this idiom.
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from modules import adaptor_swap_chain as chain
from modules.atomic_grc_client import GRCClient
from modules.atomic_htlc_scripts import build_htlc_redeem_script, p2sh_script_for
from modules.htlc_rpc import build_refund_spend
from modules.htlc_spend import satoshis_to_coins
from regtest import adaptor_steps
from regtest.adaptor_steps import Run
from regtest.console import FAIL, OK, SKIP, Console
from regtest.daemons import RegtestSetupError
from regtest.keys import generate_key, key_from_seed

TOTAL_STEPS = 9

# SIX BLOCKS, NOT contract_locktime()'s 1920. The module docstring carries the argument; the
# short version is that CLTV does the same thing with any locktime, and 1920 is 48 hours.
#: The role string that separates the contract's refund key from the funding key. Both are
#: derived from ST_ADAPTOR_FUNDING_SEED, so a run that dies after funding leaves its coins at an
#: address the next run -- or reclaim_funding.py -- can still spend. See build_contract().
REFUND_ROLE = "grc_htlc_refund"

LOCKTIME_BLOCKS_AHEAD = 6

# What to put in the contract. Well clear of Gridcoin's 0.01 fee floor, which the refund pays
# out of the contract itself -- there is no second input to draw on.
CONTRACT_COIN = Decimal("1.0")

PREIMAGE_BYTES = 32


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="grc_htlc_verify.py",
        description=(
            "Fund a real HTLC on Gridcoin testnet and prove OP_CHECKLOCKTIMEVERIFY runs: the "
            "refund is refused before the locktime EVEN WHEN THE TRANSACTION IS FINAL -- which "
            "is what isolates the opcode from mere non-finality -- and accepted after it, "
            "through the real GRCClient.refund_contract(). Refuses any network the daemon does "
            "not itself say is a test network."
        ),
    )
    parser.add_argument(
        "--funding-txid", default="",
        help="a payment to the seed-derived funding address, when the wallet does not remember it",
    )
    return parser.parse_args(argv)


def _seed_or_refuse() -> str:
    """The funding seed, or a refusal that says why this file cannot run without one.

    SEPARATE FROM operator_funding_key() because that one returns None for "no seed", which is
    the ordinary state on BTC and LTC where the wallet funds the harness itself. Here there is
    no such state: the refund key is derived from the seed, so a run with no seed would mint a
    fresh one, pay it, and strand the coin if anything failed -- which is precisely what this
    constant exists to stop.
    """
    seed = os.environ.get(adaptor_steps.FUNDING_SEED_VARIABLE, "")
    if not seed.strip():
        raise RegtestSetupError(
            f"{adaptor_steps.FUNDING_SEED_VARIABLE} is not set. This harness derives BOTH the "
            f"address you fund AND the contract's refund key from it, so that a run which dies "
            f"after funding leaves its coins somewhere recoverable rather than at a key that "
            f"existed only in a process that has exited. Nothing was built or broadcast."
        )
    return seed


def build_contract(run: Run, tip: int) -> dict:
    """The HTLC, from the REAL builder, with a SHORT test locktime that is named as one."""
    run.step(3, "build a real HTLC with the REAL script builder and a SHORT test locktime")
    # THE REFUND KEY IS DERIVED FROM THE SEED, NOT MINTED FRESH, AND 1.50 GRC PAID FOR THAT.
    #
    # It was `generate_key()` until 2026-09-28. That key lives in this process and is never
    # written anywhere, so when the run below died at step 4 the funding output it owned --
    # e1f8ae8f961d8591:0, 1.50 GRC -- became unspendable the moment python exited. Not lost to a
    # bug in what was being tested; lost because the harness minted a key, paid it, and threw it
    # away. docs/branch_coverage.md gap (c) says atomic_swap.py does exactly this and has
    # already cost 310.72 GRC, and this file reproduced it on its first real run.
    #
    # A seed-derived key has a stable address across runs, so a failed run leaves coins at an
    # address the NEXT run can spend -- and reclaim_funding.py can sweep it in between. The role
    # string is what separates it from the funding address; both come from the same seed.
    #
    # The PARTICIPANT key stays random on purpose, and the asymmetry is the point: it is the
    # hashlock side, this harness never spends through it, and nothing is ever paid to it. A key
    # that never receives cannot strand anything. Only keys that RECEIVE need to be recoverable.
    participant = generate_key()
    refund = key_from_seed(_seed_or_refuse(), REFUND_ROLE)
    preimage = secrets.token_bytes(PREIMAGE_BYTES)
    secret_hash = hashlib.sha256(preimage).digest()
    locktime = tip + LOCKTIME_BLOCKS_AHEAD
    redeem_script = build_htlc_redeem_script(
        secret_hash=secret_hash.hex(),
        participant_address=participant.address,
        refund_address=refund.address,
        locktime=locktime,
    )
    run.say(f"secret_hash={secret_hash.hex()} (public by construction; it goes in the script). "
            f"The PREIMAGE is never printed and this run never uses it -- the hashlock branch is "
            f"already established by three live swaps; the REFUND branch is what is not")
    run.say(f"locktime={locktime} = tip {tip} + {LOCKTIME_BLOCKS_AHEAD} blocks. THIS IS A TEST "
            f"VALUE, not contract_locktime()'s: that returns tip+1920 on GRC, which is 48 hours "
            f"of real waiting. CLTV does the same thing with either number")
    run.say(f"redeem script {len(redeem_script)} bytes; P2SH scriptPubKey "
            f"{p2sh_script_for(redeem_script).hex()}")
    return {
        "participant": participant, "refund": refund, "locktime": locktime,
        "redeem_script": redeem_script, "secret_hash": secret_hash,
    }


def fund_contract(run: Run, contract: dict, funding: chain.Outpoint) -> chain.Outpoint:
    """Pay the P2SH from the operator's funding output, signed IN THIS PROCESS.

    The wallet is not asked to create or sign anything, exactly as in adaptor_regtest_verify --
    which is what lets this run at all against a wallet unlocked for staking only.
    """
    run.step(4, "fund the contract's P2SH -- signed in THIS process, the wallet is not asked")
    # THE KEY THAT OWNS THE OUTPUT, NOT THE ONE THAT PAID FOR IT. This read
    # `operator_funding_key(run)` until 2026-09-28 and the chain refused every transaction it
    # built -- `39c099481d VerifySignature failed`, in the operator's debug.log, after four runs
    # in which `-22 TX rejected` named nothing.
    #
    # The funding key's output is consumed one step earlier: `prepare_operator_funding` splits
    # it into an output paying `contract["refund"]`, and `fund_and_prepare` hands that outpoint
    # back. So by the time this runs, the coin belongs to the REFUND key and the funding key has
    # no claim on it. Signing with the wrong one of two keys the same process is holding
    # produces a perfectly well-formed transaction that only a chain can reject.
    key = contract["refund"]
    raw, predicted, value = adaptor_steps.reclaim_p2pkh_to_script(
        run, key, funding, p2sh_script_for(contract["redeem_script"])
    )
    run.say(f"paying {satoshis_to_coins(value)} into the contract; predicted txid {predicted}")
    txid, message = adaptor_steps.broadcast_and_report(run, raw, "the contract funding transaction")
    if txid is None:
        raise RegtestSetupError(f"{run.asset}: the contract could not be funded: {message}")
    run.check("the funding txid PREDICTED before broadcast equals the daemon's",
              f"predicted={predicted} daemon={txid}", "the same txid", OK if txid == predicted else FAIL)
    adaptor_steps.wait_or_mine_to(run, adaptor_steps.current_height(run) + 1)
    return chain.Outpoint(txid=txid, vout=0, value_satoshis=value)


def _refund_bytes(run: Run, contract: dict, outpoint: chain.Outpoint, nlocktime: int) -> str:
    """A signed refund spend with a CHOSEN nLockTime, built by the REAL builder.

    `build_refund_spend` is the same function `GRCClient.refund_contract()` reaches through
    `broadcast_refund`, so 8a and 8b are not a harness paraphrase of the refund -- they are the
    refund, with one field varied. That is what makes step 9's acceptance a control on them.
    """
    spend = build_refund_spend(
        asset="GRC",
        rpc_call=lambda method, params=None: run.node(wallet=False).call(method, *(params or [])),
        contract_txid=outpoint.txid,
        contract_vout=outpoint.vout,
        contract_value=Decimal(satoshis_to_coins(outpoint.value_satoshis)),
        redeem_script=contract["redeem_script"],
        wif=contract["refund"].wif,
        destination_address=contract["refund"].address,
        locktime=nlocktime,
    )
    return spend.raw_hex


def step_5_non_final(run: Run, contract: dict, outpoint: chain.Outpoint, outcome: dict) -> None:
    """8a: nLockTime = the script's locktime, before it. REFUSED, and it proves nothing."""
    run.step(5, "the refund with nLockTime = the SCRIPT's locktime, before it -- a NON-FINAL refusal")
    raw = _refund_bytes(run, contract, outpoint, contract["locktime"])
    reason = adaptor_steps.mempool_reject_reason(run, raw)
    txid, message = adaptor_steps.broadcast_and_report(run, raw, f"refund, nLockTime {contract['locktime']} (not yet final)")
    refused = txid is None
    run.check("5 a non-final refund is REFUSED",
              f"{message}{f' [reject-reason={reason}]' if reason else ''}" if refused else f"ACCEPTED as {txid}",
              "a refusal", OK if refused else FAIL)
    run.say("THIS PROVES NOTHING ABOUT CLTV and is here as a control: a transaction whose "
            "nLockTime is in the future is refused by the mempool BEFORE any script runs. Step 6 "
            "is the one that isolates the opcode")
    outcome["non_final_refused"] = OK if refused else FAIL


def step_6_cltv(run: Run, contract: dict, outpoint: chain.Outpoint, outcome: dict) -> None:
    """8b: THE MEASUREMENT. nLockTime = the tip, so the transaction is FINAL."""
    tip = adaptor_steps.current_height(run)
    run.step(6, "THE CLTV MEASUREMENT: a FINAL refund, before the locktime. Only the script can refuse it")
    run.check("we are genuinely before the script's locktime", tip < contract["locktime"], True,
              OK if tip < contract["locktime"] else FAIL)
    raw = _refund_bytes(run, contract, outpoint, tip)
    run.say(f"nLockTime={tip} (the current tip), script locktime={contract['locktime']}. "
            f"nLockTime below the next block's height makes this transaction FINAL, so the "
            f"mempool's finality check PASSES and the only rule left that can refuse it is the "
            f"script's OP_CHECKLOCKTIMEVERIFY")
    reason = adaptor_steps.mempool_reject_reason(run, raw)
    txid, message = adaptor_steps.broadcast_and_report(run, raw, f"refund, nLockTime {tip} -- FINAL, and CLTV must refuse it")
    refused = txid is None
    run.check("6 CLTV REFUSES A FINAL REFUND BEFORE THE LOCKTIME",
              f"{message}{f' [reject-reason={reason}]' if reason else ''}" if refused else f"ACCEPTED as {txid}",
              "a refusal -- and this one can only have come from the script", OK if refused else FAIL)
    outcome["cltv_refused_final"] = OK if refused else FAIL
    if not refused:
        outcome["notes"].append(
            "GRIDCOIN ACCEPTED A FINAL REFUND WHOSE nLockTime IS BELOW THE SCRIPT'S LOCKTIME. "
            "That means OP_CHECKLOCKTIMEVERIFY did not enforce, and every HTLC this repository "
            "funds on Gridcoin can be refunded by its funder AT ANY TIME -- including while the "
            "counterparty is still able to claim the hashlock branch. Nothing here should fund a "
            "Gridcoin HTLC until that is explained."
        )


def step_8_accepted(run: Run, contract: dict, outpoint: chain.Outpoint, destination: str, outcome: dict) -> None:
    """9: the REAL client's refund, at the locktime. The control that says the rest is good."""
    run.step(8, "the REAL GRCClient.refund_contract() at the locktime -- it must SPEND")
    client = GRCClient(run.config.base_url, run.config.rpc_user, run.config.rpc_password)
    try:
        txid = client.refund_contract(
            contract_txid=outpoint.txid,
            contract_vout=outpoint.vout,
            redeem_script=contract["redeem_script"],
            locktime=contract["locktime"],
            refund_privkey=contract["refund"].wif,
            refund_address=destination,
        )
    except Exception as exc:  # noqa: BLE001 -- checked: the real client raises a bare Exception for every RPC failure (htlc_rpc.rpc_result), so narrowing here would let a refusal escape as a traceback. It is scored FAIL with its message, never swallowed.
        run.check("8 the REAL refund_contract() SPENDS the timelock branch", f"{type(exc).__name__}: {exc}",
                  "a txid", FAIL)
        outcome["refund_accepted"] = FAIL
        outcome["notes"].append(f"the real client could not refund: {exc}")
        return
    run.check("8 the REAL refund_contract() SPENDS the timelock branch", txid,
              "a txid -- mempool acceptance IS a full script verification", OK)
    outcome["refund_accepted"] = OK
    run.say(f"the refund paid {destination}; the SAME script that refused step 6 accepted this, "
            f"and the only thing that changed is the height and this transaction's nLockTime")


def established(outcome: dict) -> bool:
    """Both decisive outcomes OK, and nothing else counts.

    THE EXIT CODE KEYS ON THIS, and it is a function rather than an expression inline in main()
    so a test can drive it -- the first version of that test recomputed the condition beside the
    one it was testing, which ruff caught as an unused `entry` and which would have stayed green
    for any behavior at all.

    A SKIP is not a pass. A harness that exited 0 having measured nothing is rule 13's twelve
    cycles printing exit_code=0 beside "skipping this cycle".
    """
    return outcome["cltv_refused_final"] == OK and outcome["refund_accepted"] == OK


def verdict(outcome: dict) -> str:
    """The one sentence, and it never claims the production timelock was tested."""
    if established(outcome):
        return (
            "OP_CHECKLOCKTIMEVERIFY EXECUTES AND ENFORCES ON GRIDCOIN: a FINAL refund -- one the "
            "mempool's own finality check passes -- was REFUSED while the tip was below the "
            "script's locktime, and the same spend was ACCEPTED after it through the real "
            "client. The refusal can only have come from the script. This is a spend, not a "
            "source reading. It does NOT test contract_locktime()'s production value, which is "
            "tip+1920 on GRC and was not used here."
        )
    if FAIL in (outcome["cltv_refused_final"], outcome["refund_accepted"]):
        return (
            "OP_CHECKLOCKTIMEVERIFY DID NOT BEHAVE AS REQUIRED ON GRIDCOIN. See the FAIL lines; "
            "nothing is softened to make the run green, and the notes say what it would cost."
        )
    return (
        "NOT ESTABLISHED: the decisive check was never attempted. A SKIP is not a pass -- whether "
        "the opcode runs is the one thing only a chain can answer."
    )


def main(argv: list[str], console: Console | None = None) -> int:
    console = Console(TOTAL_STEPS) if console is None else console
    args = parse_args(argv)
    outcome = {"non_final_refused": SKIP, "cltv_refused_final": SKIP, "refund_accepted": SKIP, "notes": []}
    console.banner("DOES OP_CHECKLOCKTIMEVERIFY EXECUTE ON GRIDCOIN? The refund branch, on the real testnet")
    try:
        config = adaptor_steps.resolve_config("GRC")
        run = Run(console=console, config=config, wallet="")
        console.say(f"GRC: endpoint {config.base_url}  datadir {config.datadir}")
        console.say("GRC: this harness starts and stops NOTHING, and since 2026-09-28 the refund "
                    "path does not touch the wallet's lock -- your staking wallet is left as found")
        adaptor_steps.step_1_reachable(run)
        adaptor_steps.assert_test_network(run)

        tip = adaptor_steps.current_height(run)
        contract = build_contract(run, tip)
        adaptor_steps.prepare_operator_funding(run, args.funding_txid, [contract["refund"]])
        funding = adaptor_steps.fund_and_prepare(run, "the HTLC contract", contract["refund"])
        outpoint = fund_contract(run, contract, funding)

        step_5_non_final(run, contract, outpoint, outcome)
        step_6_cltv(run, contract, outpoint, outcome)

        console.step(7, "GRC", "advance to the script's locktime -- real blocks, nothing can mine them")
        adaptor_steps.wait_or_mine_to(run, contract["locktime"])
        step_8_accepted(run, contract, outpoint, adaptor_steps.wallet_owned_address(run), outcome)
    except RegtestSetupError as exc:
        console.banner("REFUSED AT A PRECONDITION")
        console.say(str(exc))
        console.say("Nothing here is a defect in the code under test, and nothing is a pass.")
        return 1

    console.step(9, "GRC", "the verdict")
    console.banner("DID CHECKLOCKTIMEVERIFY RUN")
    console.say(f"GRC: {verdict(outcome)}")
    console.say(f"GRC:   5 non-final refund REFUSED={outcome['non_final_refused']} (a control)  "
                f"6 CLTV refuses a FINAL refund={outcome['cltv_refused_final']} (THE MEASUREMENT)  "
                f"8 the real refund SPENDS at the locktime={outcome['refund_accepted']} (a control)")
    for note in outcome["notes"] or ["(none)"]:
        console.say(f"GRC:   note: {note}")
    console.summary()
    return 0 if established(outcome) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
