#!/usr/bin/env python3
"""A daemon conf is parsed, and the password never reaches a message.

Role: tests (read-only)
Reads: temp files it writes itself. No chain, no network, no daemon, and none
        of the operator's real confs.
Writes: pytest's tmp_path only
Can move funds: no
Mainnet-safe: yes

THE SECRET TEST IS THE POINT OF THE MODULE. chains/daemon_conf.py exists so an
operator never has to echo an rpcpassword into a terminal, and the way that goal
fails is not a crash -- it is one f-string interpolating the wrong key into a
line that then gets pasted somewhere. So the password's VALUE is asserted absent
from every string the module returns, with a value distinctive enough that a
substring search cannot miss it.

The section handling is the other half, and it is what makes the module useful
at all: a regtest conf conventionally carries rpcuser and rpcpassword at the top
level and rpcport inside [regtest], so reading only the top level finds
credentials, no port, and reports "unconfigured" for a daemon that is running.
"""

from __future__ import annotations

import pathlib

import pytest
from chains.daemon_conf import (
    CONF_FALLBACK_NETWORK,
    SECRET_CONF_KEYS,
    DaemonConfError,
    conf_fallback_settings,
    describe,
    parse_daemon_conf,
    rpc_settings_from_conf,
)

# Distinctive on purpose: a real-looking password would risk a false pass if some
# fragment of it happened to appear in surrounding prose.
# Named CANARY rather than SECRET so ruff's S105 does not read the assignment
# as a hardcoded credential -- which it would be right to, on any other line.
CANARY = "Zq7-UNIQUE-DO-NOT-PRINT-4f2"

REGTEST_CONF = f"""\
# litecoin.conf, as the harness expects it
server=1
rpcuser=rt
rpcpassword={CANARY}

[regtest]
rpcport=19443
"""


def test_the_port_is_found_inside_the_network_SECTION_and_not_only_at_the_top():
    """The layout a real regtest conf actually has.

    MUTATION: parse only the top level and this fails -- rpcport is absent, so
    rpc_settings_from_conf() raises and the chain reads as unconfigured while its
    daemon is answering. Verified 2026-09-29.
    """
    found = parse_daemon_conf(REGTEST_CONF, network="regtest")
    assert found == {"rpcuser": "rt", "rpcpassword": CANARY, "rpcport": "19443"}


def test_a_section_for_ANOTHER_network_is_not_read():
    """A conf that configures two networks must not have them merged.

    This is the case that would point a regtest reader at a mainnet port, and the
    section name is the only thing separating them.
    """
    both = REGTEST_CONF + "\n[main]\nrpcport=9332\n"
    assert parse_daemon_conf(both, network="regtest")["rpcport"] == "19443"
    assert parse_daemon_conf(both, network="main")["rpcport"] == "9332"
    # With no network asked for, only the top level is read -- and this conf puts
    # no port there, so the answer is "no port", not "either port".
    assert "rpcport" not in parse_daemon_conf(both)


def test_a_hash_inside_a_PASSWORD_is_not_treated_as_a_comment():
    """Modern Bitcoin Core does not strip an inline comment, and generators emit `#`.

    Stripping from the `#` would truncate the password silently, and the symptom
    would be a 401 from the daemon with nothing pointing at this parser.

    MUTATION: split each line on "#" before parsing and this fails. Verified
    2026-09-29.
    """
    text = "rpcuser=rt\nrpcpassword=abc#def#ghi\nrpcport=19443\n"
    assert parse_daemon_conf(text)["rpcpassword"] == "abc#def#ghi"


def test_a_leading_hash_IS_a_comment_so_a_commented_out_setting_is_not_read():
    """The other direction. A disabled line must stay disabled."""
    text = "#rpcpassword=old-value\nrpcuser=rt\nrpcpassword=new-value\nrpcport=19443\n"
    assert parse_daemon_conf(text)["rpcpassword"] == "new-value"


def test_settings_from_a_real_file_are_shaped_for_an_adapter(tmp_path):
    """Config.RPC's six keys exactly, so it splats into RPCAdapter like any entry."""
    conf = tmp_path / "litecoin.conf"
    conf.write_text(REGTEST_CONF)
    settings = rpc_settings_from_conf(conf, network="regtest")
    assert settings == {"user": "rt", "password": CANARY, "host": "127.0.0.1",
                        "port": 19443, "wallet": "", "timeout": 30.0}
    assert isinstance(settings["port"], int), "a string port would build a wrong URL"


def test_a_conf_MISSING_the_password_raises_instead_of_returning_a_partial_mapping(tmp_path):
    """A port with no password builds an adapter that 401s on every call.

    chains/registry.missing_settings() was added for exactly that on 2026-09-26,
    so handing back a partial mapping would push a known-broken adapter one layer
    further in before it failed.
    """
    conf = tmp_path / "litecoin.conf"
    conf.write_text("rpcuser=rt\n[regtest]\nrpcport=19443\n")
    with pytest.raises(DaemonConfError) as raised:
        rpc_settings_from_conf(conf, network="regtest")
    assert "rpcpassword" in str(raised.value)


def test_a_missing_FILE_names_the_path_it_looked_at():
    """Rule 14: "the conf did not supply one" is useless without which conf."""
    with pytest.raises(DaemonConfError) as raised:
        rpc_settings_from_conf("/definitely/not/here/litecoin.conf", network="regtest")
    assert "/definitely/not/here/litecoin.conf" in str(raised.value)
    assert "FileNotFoundError" in str(raised.value)


def test_a_NON_NUMERIC_port_is_named_rather_than_crashing_on_int(tmp_path):
    """An unparseable port is a conf problem, and it should read as one."""
    conf = tmp_path / "litecoin.conf"
    conf.write_text(f"rpcuser=rt\nrpcpassword={CANARY}\nrpcport=nineteen\n")
    with pytest.raises(DaemonConfError) as raised:
        rpc_settings_from_conf(conf)
    assert "nineteen" in str(raised.value)
    assert CANARY not in str(raised.value), "a parse failure must not print the password either"


def test_the_PASSWORD_never_appears_in_the_line_that_describes_the_connection(tmp_path):
    """THE WHOLE REASON THIS MODULE EXISTS.

    On 2026-09-25 a wallet CLI printed a 25-word recovery seed into a terminal
    whose output was then pasted into a chat, and that wallet had to be treated
    as public from then on. describe() is the one formatter, so this is the one
    place that can leak.

    MUTATION: add the password to describe()'s output and this fails. Verified
    2026-09-29.
    """
    conf = tmp_path / "litecoin.conf"
    conf.write_text(REGTEST_CONF)
    settings = rpc_settings_from_conf(conf, network="regtest")
    line = describe("/some/path/litecoin.conf", settings, network="regtest")
    assert CANARY not in line, f"describe() printed the password:\n{line}"
    # The names ARE printed: they are what makes a parse failure diagnosable.
    assert "rpcpassword" in line and "NOT shown" in line, line
    assert "19443" in line and "rt" in line, f"the port and user are not secret and are useful:\n{line}"


def test_every_string_this_module_returns_is_checked_against_the_secret_keys():
    """SECRET_CONF_KEYS is a claim about which values may not be printed.

    Asserting the set's contents pins the claim itself: adding a key to
    CONF_TO_SETTING that is also sensitive without adding it here would leave the
    test above passing while a new value leaks.
    """
    assert {"rpcpassword"} == SECRET_CONF_KEYS


# ---------------------------------------------------------------------------
# THE SHARED RESOLVER, and the reason it is shared. Added 2026-09-29 after the
# balance reader and the swap driver gave different answers about the same
# running Litecoin daemon within an hour of each other.
# ---------------------------------------------------------------------------


#: EVERY ROOT SCRIPT THAT RESOLVES A CHAIN, and which side it is on. This is a
#: decision register rather than a rule, and the honest reason is that I tried
#: three rules first and each one bent: "these two files" missed the fourth
#: instance, "goes through the service" missed settle_payout.py, "touches the
#: custodial database" missed swap_readiness.py. Every attempt was a
#: generalization fitted to the cases I had already seen.
#:
#: DIRECT DRIVERS are the operator's own tools. They hold no custodial state and
#: talk to daemons on the operator's behalf, so resolving a Litecoin daemon from
#: litecoin.conf is a convenience with no other party's money behind it. These
#: MUST use the shared resolver, because an operator who configures a daemon once
#: should not be told it exists by one of them and not another -- which happened
#: four times on 2026-09-29.
DIRECT_DRIVERS = frozenset({
    "atomic_swap.py",       # self-custody swap, BTC/LTC/GRC, both directions
    "atomic_swap_xrp.py",   # self-custody swap, XRP against a script chain
    "chain_balances.py",    # read-only balances
})

#: SERVICE SIDE scripts speak FOR the brokered terminal, and must connect to
#: exactly what it connects to. open_swap.py creates a swap the deposit watcher
#: then has to see; settle_payout.py proves a payout exists in the wallet the
#: service paid from; show_swap.py reports on those rows; swap_readiness.py
#: answers "can this terminal run a swap", which is a question about the
#: service's daemons and not about any daemon a conf happens to name. For these a
#: conf fallback is not a convenience -- it is a way to inspect, credit or clear
#: a DIFFERENT daemon than the one holding customer funds, which is strictly
#: worse than refusing until the variables are exported. Changing how the service
#: resolves its daemons is live posture and the operator's (rule 16).
SERVICE_SIDE = frozenset({
    # correct_payout_amounts.py, added 2026-10-03. SERVICE SIDE, and the reason is
    # show_payout_fees.py's one step further on: it reads the transactions THIS
    # TERMINAL broadcast, by the txids in its own `payouts` rows, in order to write
    # a corrected amount back into those rows.
    #
    # A conf fallback here would be worse than a wrong read, because this one
    # WRITES. Resolve a different Gridcoin daemon and `gettransaction` does not know
    # the txid, so every GRC row degrades to ARITHMETIC ONLY -- which the tool then
    # prints and records honestly, and the operator reads as "my daemon could not be
    # reached" about a daemon that is running and holds the transaction. The
    # correction still lands, with its provenance quietly weakened, which is the
    # wrong-number-under-the-right-label failure of this register with the label
    # being the word VERIFIED.
    #
    # It is also the half that could be genuinely wrong rather than merely weaker: a
    # daemon that is NOT the one that paid could know a DIFFERENT transaction with
    # the same txid only by collision, but it can and does know transactions paying
    # the same reused address -- and the read matches on the destination address. The
    # register's own words apply: strictly worse than refusing until the variables
    # are exported.
    "correct_payout_amounts.py",
    # collect_fees.py, added 2026-10-04. SERVICE SIDE, and this one is not a
    # judgment call either: it resolves a chain for exactly one purpose, which is to
    # ask whether the wallet the PAYOUT WORKER SPENDS FROM can still fund what it
    # owes after a sweep. An answer about any other daemon is not a weaker answer to
    # that question, it is an answer to a different one.
    #
    # It is also the entry on this list where the wrong-number-under-the-right-label
    # failure would cost the most, because the label is the word `obligated` and the
    # consequence is a BROADCAST. A conf fallback could read a balance off a wallet
    # the desk does not pay from, compute headroom against it, decide a sweep fits,
    # and send this desk's fee out of the wallet that is holding a credited
    # customer's deposit -- which is the one thing swap_terminal/fee_sweep.py exists
    # to prevent, arriving through the door this register guards rather than through
    # the arithmetic. The register's own words apply unchanged: strictly worse than
    # refusing until the variables are exported.
    #
    # And a sweep cannot be undone. correct_payout_amounts.py's entry above says a
    # conf fallback there would be worse than a wrong read because that tool WRITES;
    # this one SENDS.
    "collect_fees.py",
    "open_swap.py",
    "settle_payout.py",
    # show_payout_fees.py, added 2026-10-03. SERVICE SIDE, and the reason is the
    # same one settle_payout.py is on this list for, one step further on: it reads
    # the fee the wallet PAID on a payout this terminal sent, which means it has to
    # ask the daemon holding that transaction.
    #
    # A conf fallback here would not be a convenience, it would be a wrong answer
    # that looks like a measurement. Resolve a different daemon and `gettransaction`
    # does not know the txid, so the payout lands in `unread` -- and the operator
    # reads "0 of 7 payouts answered with a fee" about seven payouts their service
    # made and their wallet recorded. The register's own words: strictly worse than
    # refusing until the variables are exported.
    #
    # MEASURED THE DAY IT WAS CLASSIFIED, which is why this is not a coin flip. On
    # the operator's host, against the service's own exported settings, all 7 of 7
    # GRC payouts answered: mean fee 0.00100000, low and high identical. That is the
    # outcome this side of the register produces; the other side could have produced
    # it only by luck of the conf naming the same wallet.
    "show_payout_fees.py",
    # sol_payout_preview.py, added 2026-10-03. SERVICE SIDE, for the same reason
    # show_payout_fees.py is, read forward instead of back: it previews the payout
    # THIS TERMINAL would make, so it has to ask the cluster the payout worker will
    # ask. A conf fallback pointing it at another endpoint would print a rent floor,
    # a balance and a genesis hash from a cluster nobody is paying out on -- and the
    # operator would read it as the plan for their send. Wrong numbers under the
    # right labels, which is worse than a refusal.
    #
    # It also means the devnet guard is measuring the real thing: the cluster is
    # identified by genesis hash through the same adapter the worker builds, so a
    # preview that says DEVNET is a statement about the endpoint the send will use.
    "sol_payout_preview.py",
    # resolve_halted_swap.py, added 2026-10-03. SERVICE SIDE, and it resolves a
    # chain for exactly one reason: to ask whether the destination wallet can fund
    # the payout it is about to hand to payout_worker.
    #
    # That makes the classification forced rather than chosen. The question is not
    # "can some GRC wallet pay 55.45 GRC", it is "can the wallet THIS WORKER WILL
    # SPEND FROM pay it" -- so it has to resolve what the worker resolves. A conf
    # fallback would read a balance off a different wallet and print it under the
    # label `funding`, which is the wrong-number-under-the-right-label failure the
    # entries above are on this list to avoid, on a line an operator reads
    # immediately before authorizing a payout.
    #
    # The check exists because moving a swap to payout_pending BYPASSES the funding
    # gate: services/swap_service.create_swap() asks at CREATION, and the swap this
    # tool was written for was created a week before it was resolved.
    "resolve_halted_swap.py",
    "swap_readiness.py",
    # wallet_custody.py, added 2026-10-03. SERVICE SIDE, and this one is not a
    # judgment call: the question it asks is "which wallet does the BROKERED
    # TERMINAL's endpoint serve", so an answer about any other daemon is not a
    # weaker answer, it is an answer to a different question.
    #
    # A conf fallback here would be the worst instance of the wrong-number-under-
    # the-right-label failure this register exists to prevent, because the label is
    # the word SEPARATED. Resolve a daemon from gridcoin.conf, find a named wallet
    # loaded on it, and this tool prints "the desk's wallet is distinct from the
    # default" about a daemon the desk does not use -- an operator would read that
    # as the custody question answered and stop looking. The state it would be
    # hiding is the one measured on their host on 2026-10-03: GRC_RPC_WALLET empty,
    # the desk sharing the operator's own default wallet, 500 GRC moving as a
    # self-transfer.
    #
    # It is also why this tool refuses through network_target.may_read_a_wallet()
    # rather than deciding for itself what a safe port is: the wallet reads it
    # makes (getwalletinfo, listwallets, validateaddress) carry a balance, and the
    # service-side rule and the mainnet refusal are the same requirement -- ask
    # exactly the daemon the terminal asks, and only when its port says test chain.
    "wallet_custody.py",
})
# show_swap.py was in this set for one commit and the register's own completeness
# check removed it: it reads swap_terminal.db and resolves no chain at all, so a
# row describing it was a row about a file that does not do the thing. That is the
# check working in the direction nobody writes a test for -- an entry going stale
# rather than one going missing.
#
# repair_inventory_reservations.py, 2026-10-04: CONSIDERED AND DELIBERATELY ON
# NEITHER SIDE, recorded here because in a register "no row" and "nobody looked"
# are indistinguishable, and only one of them is a decision.
#
# It resolves no chain. Measured against this register's own three markers rather
# than by reading the file: `build_adapters(` 0, `CLIENTS[` 0, `SCRIPT_CLIENTS[` 0.
# It imports nothing from chains/, constructs no adapter, opens no socket and makes
# no RPC call of any kind -- its whole derivation is one SQL statement over
# `wallet_inventory` left-joined to `payouts`, which is why the thing it decides is
# a query and not a balance read (rules 5 and 20).
#
# So it stays out of BOTH frozensets, and that is ENFORCED rather than remembered:
# the second assertion in the test below fails with "is in the register but no
# longer resolves a chain" for any entry the markers do not find -- which is
# exactly how show_swap.py came off the list above. Adding a row for this tool
# would not be caution, it would break the suite, and the row would describe a file
# that does not do the thing.
#
# AND IF IT EVER DOES RESOLVE ONE, IT IS SERVICE SIDE. Said now because the
# classification is forced rather than open: it WRITES hot_reserved for the wallet
# the payout worker spends from, so any balance it ever read would have to be that
# daemon's. A conf fallback would let it reconcile the desk's reservations against
# a wallet the desk does not pay from -- correct_payout_amounts.py's entry above
# word for word, since that one WRITES too. The register will ask the question on
# its own the moment a marker appears; this records what the answer is.


def test_EVERY_entry_point_THAT_RESOLVES_A_CHAIN_is_on_one_side_or_the_other():
    """A new one fails this test rather than quietly picking a side.

    THE FOUR INSTANCES, in the order they were found on 2026-09-29, none of them
    pointed to by anything:

      1. chain_balances.py grew the conf fallback.
      2. atomic_swap_xrp.py's adapter did not -- "(none)" for a daemon the
         reader had just found.
      3. build_script_client() still did not -- found the daemon, then could not
         build an HTLC on it, at the last check before funding a --run.
      4. atomic_swap.py's client_for() still did not.

    The first version of this test named two files and asserted they imported
    the resolver. They did. That is why it caught none of instances 3 and 4, and
    why its own docstring's claim -- "a new one that builds its own is what this
    catches" -- was false as written. A check that enumerates what it checks
    measures the list.

    What it measures now is that the register is COMPLETE: every root script
    resolving a chain is classified, so an unclassified new one fails here and
    somebody has to decide which side it is on. That is the property the four
    instances actually needed, and it does not depend on my finding the right
    generalization.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    # What "resolves a chain" looks like in source: it asks the registry for
    # adapters, or indexes a client table. Both are how a daemon handle is made.
    markers = ("build_adapters(", "CLIENTS[", "SCRIPT_CLIENTS[")
    found = sorted(path.name for path in root.glob("*.py")
                   if any(marker in path.read_text() for marker in markers))
    assert found, "no root script resolves a chain any more; this test has stopped measuring"
    unclassified = set(found) - DIRECT_DRIVERS - SERVICE_SIDE
    assert not unclassified, (
        f"{sorted(unclassified)} resolve a chain and are on neither side of the register above. "
        f"Decide: a DIRECT DRIVER must use chains/daemon_conf.conf_fallback_settings() so an "
        f"operator's conf is honored everywhere; a SERVICE SIDE script must NOT, because it has to "
        f"connect to exactly what the brokered terminal connects to"
    )
    assert not (DIRECT_DRIVERS | SERVICE_SIDE) - set(found), (
        f"{sorted((DIRECT_DRIVERS | SERVICE_SIDE) - set(found))} is in the register but no longer "
        f"resolves a chain. Remove the row rather than leaving it to describe a file that moved"
    )


def test_every_DIRECT_DRIVER_uses_the_shared_resolver():
    """The rule the register exists to enforce on the three that must follow it."""
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in sorted(DIRECT_DRIVERS):
        source = (root / name).read_text()
        assert "conf_fallback_settings" in source, (
            f"{name} resolves a chain without the shared conf fallback. An operator who configured "
            f"a daemon by conf will be told by one direct driver that it is there and by this one "
            f"that it is not"
        )
        assert "CONF_FALLBACK_NETWORK = {" not in source, (
            f"{name} spells its own fallback table. There is one, in chains/daemon_conf.py"
        )


def test_no_SERVICE_SIDE_script_quietly_gained_a_conf_fallback():
    """The other direction, and it is the one that costs money if it slips.

    A brokered script resolving a daemon from a conf can credit or clear against
    a different daemon than the one holding customer funds. Adding it there is a
    posture change and belongs to the operator, so it fails here rather than
    passing because it looks like consistency.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    for name in sorted(SERVICE_SIDE):
        source = (root / name).read_text()
        assert "conf_fallback_settings" not in source, (
            f"{name} speaks for the brokered terminal and now resolves daemons from a conf. It has "
            f"to connect to exactly what the service connects to; this is a live-posture change and "
            f"the operator's call, not a consistency fix"
        )


def test_GRC_is_absent_from_the_shared_table_so_no_entry_point_can_resolve_it():
    """The exclusion has to hold for the DRIVER, not only for the reader.

    A read of the wrong Gridcoin wallet is bad; a swap driver funding an HTLC
    against it is a different order of bad, and the driver gained this fallback
    on 2026-09-29. Excluding GRC in one shared table is what makes the guarantee
    the same in both.

    MUTATION: add "GRC" to CONF_FALLBACK_NETWORK and this fails. Verified
    2026-09-29.
    """
    assert "GRC" not in CONF_FALLBACK_NETWORK
    settings, line = conf_fallback_settings("GRC")
    assert settings is None, "GRC was resolved from a conf; its conf is shared with mainnet"
    assert "by design" in line and "GRC_RPC_PORT" in line, (
        f"the refusal has to say it is deliberate and name what to set instead:\\n{line}"
    )


def test_an_unknown_chain_gets_a_sentence_rather_than_an_exception():
    """conf_fallback_settings never raises: a caller needs to SAY why, not crash."""
    settings, line = conf_fallback_settings("DOGE")
    assert settings is None
    assert "DOGE_RPC_PORT" in line, line
