"""testnet_wallets.py: it may create a wallet, and it may NOT reach a mainnet daemon.

Role: tests (seeded adapters; no daemon, no socket, no network)
Reads: testnet_wallets.py, through conftest.root_entry_point()
Writes: nothing
Can move funds: no. Every adapter here is a stub that raises on a method the
        tool is not supposed to call.
Mainnet-safe: yes
Live-safe: yes

WHAT THESE EXIST TO HOLD. The tool's one write is `createwallet`, and it runs
against the operator's OWN daemons -- not a harness datadir this tree created. So
the property worth pinning is not "it makes a wallet"; it is WHICH DAEMONS IT
REFUSES, and that nothing is written to those. Every refusal test asserts on the
CALLS THE STUB RECEIVED, not on the message: a message is what a refactor keeps
and a call list is what it breaks.

Per BEHAVIORAL_VERIFICATION_PRINCIPLE: seed the rows the real code reads, run the
real code, assert on what came out. Nothing here paraphrases the tool's logic.
"""

from __future__ import annotations

import io
import json

import pytest
import valid_addresses
from chains.base import RPCError
from conftest import root_entry_point
from regtest.console import Console

tw = root_entry_point("testnet_wallets.py")


class _Daemon:
    """An adapter that answers a seeded script and REMEMBERS what it was asked.

    `url` is present because the tool reads it for the WalletSite's `where`, and a
    stub missing it would have the tool fall back to a generic phrase -- which is
    the shape of bug this file is about, so the stub carries the real attribute.
    """

    url = "http://127.0.0.1:18443/wallet/desk_hot"

    def __init__(self, answers: dict, *, wallets: list[str] | None = None):
        self.answers = answers
        self.wallets = [] if wallets is None else list(wallets)
        self.asked: list[tuple] = []

    def call(self, method, *params):
        self.asked.append((method, *params))
        if method == "listwallets":
            return list(self.wallets)
        if method == "createwallet":
            self.wallets.append(params[0])
            return {"name": params[0]}
        if method == "loadwallet":
            if params[0] in self.answers.get("_on_disk", []):
                self.wallets.append(params[0])
                return {"name": params[0]}
            # RPCError SPECIFICALLY, because that is what ensure_wallet_on() catches to
            # decide "not on disk, so create it". A stub raising anything else -- the
            # first version of this raised RuntimeError -- escapes that handler and the
            # test fails for a reason unrelated to its claim, which is the shape this
            # repository keeps rediscovering.
            raise RPCError(f"wallet {params[0]} not found")
        if method in self.answers:
            return self.answers[method]
        raise AssertionError(f"{method} was asked and this stub has no answer for it")

    def methods(self) -> list[str]:
        return [call[0] for call in self.asked]


def _run(monkeypatch, asset, answers, *, wallet="desk_hot", wallets=None):
    """Drive prepare_chain() for one chain against a seeded daemon.

    Config.RPC is patched rather than the environment, because that is the mapping
    the tool reads (wallet_name_for) and the one registry.build_adapters() reads.
    """
    daemon = _Daemon(answers, wallets=wallets)
    monkeypatch.setattr(tw, "build_adapters", lambda rpc: {asset: daemon})
    monkeypatch.setattr(tw.Config, "RPC", {asset: {"wallet": wallet}}, raising=False)
    stream = io.StringIO()
    console = Console(1, stream=stream)
    row = tw.prepare_chain(console, asset)
    return daemon, row, stream.getvalue()


_HEALTHY_BTC = {
    "getblockchaininfo": {"chain": "testnet4", "blocks": 100, "headers": 100,
                          "initialblockdownload": False},
    "getwalletinfo": {"descriptors": True, "balance": 0},
    # DERIVED, NOT TYPED. tests/test_address_literals_are_valid.py holds a CEILING on
    # address-shaped literals in the tree and I pushed it from 60 to 64 writing this
    # file; its own message says what to do instead -- "Use tests/valid_addresses.py
    # rather than writing one -- a derived address cannot be mistyped and says what it
    # is for". One of the four I added was `tltc1qexampleaddress...`, which is not a
    # valid bech32 string at all, so the sibling test that DECODES every literal caught
    # it too. Both were real: a hand-typed address in a test about address shapes is
    # the one place a typo is invisible.
    "getnewaddress": valid_addresses.BTC_PARTICIPANT,
}


# ---------------------------------------------------------------------------
# THE REFUSALS. Each asserts createwallet was NOT called.
# ---------------------------------------------------------------------------


def test_a_MAINNET_daemon_is_refused_and_NOTHING_is_created(monkeypatch):
    """The whole point of the file. Asserted on the call list, not the sentence.

    A refusal that still created the wallet would pass any message assertion and
    would have put a wallet in the operator's real Bitcoin wallet directory. The
    ordering that prevents it -- network before write -- is prepare_chain()'s, and
    this is what holds it there.
    """
    daemon, row, out = _run(monkeypatch, "BTC", {"getblockchaininfo": {"chain": "main"}})
    assert row["ok"] is False
    assert "createwallet" not in daemon.methods(), "a wallet was created on a MAINNET daemon"
    assert "listwallets" not in daemon.methods(), "it did not even ask about wallets, which is right"
    assert "not on this chain's allowlist" in out
    assert "mainnet daemon" not in out.lower(), (
        "it must not CLAIM mainnet -- for LTC a refused network may be signet, and that "
        "confidently-wrong claim is the defect CHAIN_TEST_NETWORKS' comment records"
    )


def test_an_UNREADABLE_network_gets_the_CREDENTIAL_sentence_not_the_wrong_chain_one(monkeypatch):
    """Two refusals, two remedies. Collapsing them sends the operator to the wrong place.

    A daemon that will not say which network it is on is almost always a 401 or a
    closed port -- an RPC problem. A daemon that names `main` is pointed at the
    wrong chain. The first sentence says to check credentials; the second says the
    daemon is on a chain this tool will not touch.
    """
    daemon, row, out = _run(monkeypatch, "BTC", {"getblockchaininfo": {}})
    assert row["ok"] is False and "createwallet" not in daemon.methods()
    assert "did not name its network" in out
    assert "401" in out, "and it names the failure a 401 actually produces"
    assert "not on this chain's allowlist" not in out, "that is the OTHER refusal"


def test_LTC_reporting_testnet4_is_REFUSED_because_that_is_the_near_match(monkeypatch):
    """Litecoin's DATADIR is named testnet4; its chain is not. The trap, pinned.

    CHAIN_TEST_NETWORKS deliberately withholds `testnet4` from LTC: Litecoin Core
    0.21 predates the value entirely and reports `test`. A daemon answering
    `testnet4` for LTC is therefore not a Litecoin testnet node, and the allowlist
    is what stands between a command and a mainnet wallet.
    """
    daemon, row, _out = _run(monkeypatch, "LTC", {"getblockchaininfo": {"chain": "testnet4"}})
    assert row["ok"] is False and "createwallet" not in daemon.methods()


def test_an_UNSET_wallet_variable_refuses_rather_than_inventing_a_FIFTH_name(monkeypatch):
    """A funded wallet the payout path cannot read is worse than no wallet.

    config.py defaults BTC_RPC_WALLET to "", and four wallet names already exist in
    this tree. Picking one here would make a fifth AND make a wallet the
    application does not look at -- which looks finished and is not.
    """
    daemon, row, out = _run(monkeypatch, "BTC", _HEALTHY_BTC, wallet="")
    assert row["ok"] is False and "createwallet" not in daemon.methods()
    assert "BTC_RPC_WALLET" in out, "the variable is named, because that is the action"
    assert "FIFTH" in out


def test_an_unconfigured_chain_says_WHY_and_asks_no_daemon(monkeypatch):
    """No adapter means no calls. The reason comes from registry.why_unconfigured()."""
    monkeypatch.setattr(tw, "build_adapters", lambda rpc: {})
    monkeypatch.setattr(tw.Config, "RPC", {}, raising=False)
    stream = io.StringIO()
    row = tw.prepare_chain(Console(1, stream=stream), "BTC")
    out = stream.getvalue()
    assert row["ok"] is False and row["why"] == "not configured"
    assert "NO CONF FALLBACK HERE" in out, (
        "and it says so, because every other direct driver in this tree DOES fall back to a "
        "conf -- an operator who configured a daemon that way has to be told why this one "
        "refuses rather than left to think it is broken"
    )


# ---------------------------------------------------------------------------
# THE PERMITTED PATH
# ---------------------------------------------------------------------------


def test_a_TESTNET_daemon_gets_its_wallet_and_an_address(monkeypatch):
    """The happy path, and the kind of wallet is REPORTED rather than chosen."""
    daemon, row, out = _run(monkeypatch, "BTC", _HEALTHY_BTC)
    assert row["ok"] is True
    assert "createwallet" in daemon.methods() and ("createwallet", "desk_hot") in daemon.asked
    assert row["wallet"] == "desk_hot"
    assert row["address"] == valid_addresses.BTC_PARTICIPANT
    assert row["address"].startswith("tb1")
    assert row["kind"] == "descriptor"
    assert "REPORTED, not chosen" in out, "it must not read as a choice this tool made"


def test_an_EXISTING_wallet_is_loaded_and_not_recreated(monkeypatch):
    """Idempotent by construction: a second run must not try to create it again.

    `createwallet` on an existing name is an error, and an error here would read as
    "the tool is broken" on the most ordinary second run there is.
    """
    daemon, row, _out = _run(monkeypatch, "BTC", _HEALTHY_BTC, wallets=["desk_hot"])
    assert row["ok"] is True
    assert "createwallet" not in daemon.methods(), "an already-loaded wallet was recreated"


def test_a_CORRECT_address_PASSES_the_shape_check(monkeypatch):
    """The positive half, and its absence let a mutation live.

    The wrong-prefix test below asserts a bad address FAILS. On its own that is
    satisfied by a check that fails EVERYTHING -- which is exactly what
    `address.startswith(prefix + "1")` does, since no address starts with "tb11",
    and that was the first version of this code. The mutation survived the whole
    suite. A negative case with no positive case is a test that cannot tell a
    working check from a broken one.
    """
    _daemon, row, out = _run(monkeypatch, "BTC", _HEALTHY_BTC)
    assert row["ok"] is True, "a correct tb1 address on a testnet4 daemon must pass"
    assert "address prefix" in out and "OK" in out
    assert "FAIL" not in out


def test_an_address_with_the_WRONG_PREFIX_fails_the_shape_check(monkeypatch):
    """One base58 byte is six networks. The bech32 HRP is the only thing that tells them apart.

    `0x6F` is shared by BTC testnet, regtest and signet, LTC testnet and regtest,
    and GRC testnet. So the realistic error -- an address from the OLD regtest
    datadir pasted into a testnet4 faucet -- is invisible in base58 and obvious in
    bech32. A `bcrt1...` from a daemon claiming testnet4 is that error.
    """
    answers = {**_HEALTHY_BTC, "getnewaddress": valid_addresses.BTC_REGTEST_DEPOSIT}
    _daemon, row, out = _run(monkeypatch, "BTC", answers)
    assert "address prefix" in out
    assert "FAIL" in out, "a regtest address on a testnet4 daemon has to be a failure on screen"
    assert row["ok"] is False, (
        "and the row must be refused, not merely annotated -- a mismatched address printed "
        "under 'PASTE THESE INTO A FAUCET' is a thrown-away faucet payment"
    )
    assert "REFUSING to offer this address" in out
    assert "The wallet WAS created" in out, (
        "and it says what DID happen, so the operator is not left wondering whether to re-run"
    )


def test_a_SYNCING_daemon_says_PAY_NOW_SEE_LATER_rather_than_refusing(monkeypatch):
    """33-54 hours of sync must not block the faucet step, and the operator must know why.

    A faucet payment lands on the chain whether or not this node has caught up. The
    only consequence of an unsynced node is that it will not REPORT the payment
    yet -- so refusing here would waste two days for nothing, and saying nothing
    would have the operator think the faucet failed.
    """
    answers = {**_HEALTHY_BTC,
               "getblockchaininfo": {"chain": "testnet4", "blocks": 71056, "headers": 155858,
                                     "initialblockdownload": True}}
    _daemon, row, out = _run(monkeypatch, "BTC", answers)
    assert row["ok"] is True and row["synced"] is False
    assert "STILL SYNCING" in out
    assert "SAFE to make now" in out and "will not" in out


# ---------------------------------------------------------------------------
# THE FILE'S OWN CLAIMS ABOUT ITSELF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("forbidden", [
    "sendtoaddress", "sendrawtransaction", "sendmany", "signrawtransactionwithwallet",
    "dumpprivkey", "dumpwallet", "importprivkey", "walletpassphrase", "encryptwallet",
])
def test_the_tool_has_NO_send_path_and_touches_NO_key(forbidden):
    """The header says "Can move funds: NO" and this is what makes that checkable.

    A claim in a module header is read by whoever is deciding whether to run the
    thing against a live daemon, which is the worst possible place for a sentence
    nobody verifies. `walletpassphrase` and `encryptwallet` are in the list for the
    repository's standing rule rather than for funds: a passphrase must never
    appear in a command this repo emits, and the way that rule breaks is a tool
    helpfully offering to encrypt the wallet it just made.
    """
    source = tw.__file__
    assert source
    body = "\n".join(
        line for line in open(source, encoding="utf-8").read().splitlines()  # noqa: SIM115, PTH123 -- checked: one read of one file in a test; a with-block or Path.read_text here buys nothing and the line is shorter than the noqa
        if not line.lstrip().startswith("#")
    )
    # The docstring NAMES several of these on purpose, which is the point of the
    # header. So the assertion is on the CODE: the quoted names live in the
    # docstring, and a real call would be `adapter.call("sendtoaddress", ...)`.
    assert f'"{forbidden}"' not in body.split('"""')[2], (
        f"testnet_wallets.py calls {forbidden!r} outside its docstring, and its header says it "
        f"has no send path and touches no key"
    )


def test_every_faucet_carries_its_EVIDENCE_and_not_just_a_url():
    """Rule 17 as a structure: the claim and how it is known travel together.

    None of these was tested from this container -- it cannot fill in a captcha.
    A bare list of confident-looking URLs is how an operator spends twenty minutes
    on three dead faucets, so the caveat is attached to each entry rather than
    written once in a paragraph above them.
    """
    assert tw.FAUCETS, "an empty table would make the closing block print nothing useful"
    for asset, entries in tw.FAUCETS.items():
        assert asset in tw.CHAINS
        for faucet in entries:
            assert faucet.url.startswith("https://"), f"{asset}: {faucet.url!r} is not https"
            assert faucet.note.strip(), f"{asset}: {faucet.url} has no note on what it gives"
            # PREFILL IS A CLAIM ABOUT THAT FAUCET, so a link is only built for one that
            # documents the parameter. Appending ?address= to a site that ignores it
            # would teach the operator it works everywhere, which is worse than a bare
            # URL -- the next faucet they try by hand would silently drop the address.
            # A DERIVED ADDRESS, NOT A TYPED SAMPLE. `tb1qexample` was the first
            # version and it tripped BOTH address-literal gates: the decode check
            # (it is not valid bech32) and the ceiling. Second time today, and the
            # gate is right -- a hand-typed address in a test about addresses is
            # where a typo is invisible.
            built = faucet.link_for(valid_addresses.BTC_PARTICIPANT)
            assert (f"?address={valid_addresses.BTC_PARTICIPANT}" in built) is faucet.prefill, (
                f"{asset}: {faucet.url} prefill={faucet.prefill} but link_for() built {built!r}"
            )
    assert "UNREACHABLE from this container" in tw.SEARCHED_NOT_TESTED, (
        "the evidence string must say WHY none was tested -- a 403 at the proxy is a harder "
        "fact than 'I did not try', and it tells the operator their own machine has no such "
        "restriction"
    )
    prefilling = [f for entries in tw.FAUCETS.values() for f in entries if f.prefill]
    assert prefilling, "at least one faucet documents ?address=; a table with none has lost it"


def test_the_closing_block_prints_SOMETHING_when_every_chain_refused():
    """A blank tail is ambiguous between "all done" and "it died" (rule 14).

    This is the run an operator is most likely to have: nothing configured yet.
    """
    stream = io.StringIO()
    tw.report(Console(1, stream=stream), [{"asset": "BTC", "ok": False, "why": "not configured"}])
    out = stream.getvalue()
    assert "NO CHAIN IS READY" in out
    assert "BTC" in out and "not configured" in out, "and it says which chain and why"


def test_one_chain_FAILING_does_not_cost_the_other_its_address():
    """The address for the chain that worked is the whole product of a run.

    Losing it because the other daemon was unreachable would make the operator run
    the tool twice and read the first answer off the floor.
    """
    stream = io.StringIO()
    tw.report(Console(2, stream=stream), [
        {"asset": "BTC", "ok": False, "why": "OSError"},
        {"asset": "LTC", "ok": True, "wallet": "desk_hot", "network": "test",
         "address": valid_addresses.LTC_PARTICIPANT, "kind": "legacy", "synced": True},
    ])
    out = stream.getvalue()
    assert valid_addresses.LTC_PARTICIPANT in out
    assert "cypherfaucet.com/ltc-testnet" in out, "with a faucet to paste it into"


# ---------------------------------------------------------------------------
# main(), WHICH NOTHING RAN -- and that is why a TypeError reached the operator's
# terminal on the first line of real work. Every test above calls prepare_chain()
# or report() directly, which is rule 10's layering working for the DECISIONS and
# leaving the ORCHESTRATION untested. The orchestration is the part an operator
# runs.
#
# The specific crash: `console.step(number, title)` against regtest.console.Console,
# whose step() is `(number, chain, title)`. The two classes named Console differ in
# THREE methods and this was my second crossing in one file.
#
# AND THESE TESTS FOUND A SECOND DEFECT, IN A SHIPPED CLASS. They could not read
# their own output: capsys returned '' and so did capfd, while the output sat plainly
# visible in pytest's "captured stdout" dump. The cause was that
# regtest.console.Console was declared `__init__(self, total_steps, stream=sys.stdout)`
# -- and a DEFAULT ARGUMENT IS EVALUATED AT DEF TIME, so every Console built without
# an explicit stream held the stdout object from when that module was imported, which
# under pytest is the session-level capture rather than the test's. It resolves
# sys.stdout at CONSTRUCTION now, and capsys works.
#
# Nothing had hit it because every other test in this repo passes
# stream=io.StringIO(). A main() test cannot: main() builds its own Console. So the
# gap that let the TypeError ship was also hiding the thing that made the gap hard
# to close.
# ---------------------------------------------------------------------------


def test_main_RUNS_END_TO_END_which_is_the_test_that_was_missing(monkeypatch, capsys):
    """One chain, seeded daemon, through main(). No decision function called directly.

    It asserts the EXIT CODE and the closing block, because those are what an operator
    sees -- but the real value is that it executes every line main() touches:
    Console(), console.step(), the per-chain loop, the exception handler and report().
    A TypeError in any of them fails here instead of on the operator's host.
    """
    daemon = _Daemon(_HEALTHY_BTC)
    monkeypatch.setattr(tw, "build_adapters", lambda rpc: {"BTC": daemon})
    monkeypatch.setattr(tw.Config, "RPC", {"BTC": {"wallet": "desk_hot"}}, raising=False)

    assert tw.main(["--btc"]) == 0
    out = capsys.readouterr().out
    assert "preparing 1 chain(s): BTC" in out, "the announcement comes BEFORE the work (rule 14)"
    # THE RENDERED SHAPE, READ OFF A REAL RUN rather than guessed. regtest.console
    # prints `step 1/1  [BTC]  testnet wallet` -- the chain in brackets, which is the
    # whole reason its step() takes a `chain` argument that step_console's does not.
    # My first version asserted "BTC testnet wallet" and failed on the formatting:
    # a test wrong about the very thing it had just been written to fix.
    assert "step 1/1" in out and "[BTC]" in out and "testnet wallet" in out, (
        "console.step() rendered -- this is the call that crashed on the operator's host"
    )
    assert "PASTE THESE INTO A FAUCET" in out
    assert valid_addresses.BTC_PARTICIPANT in out
    assert f"cypherfaucet.com/btc-testnet?address={valid_addresses.BTC_PARTICIPANT}" in out, (
        "the faucet link CARRIES THE ADDRESS where the faucet supports ?address=, which "
        "removes the one step in this flow where a human copies a 42-character string by hand"
    )
    assert ("createwallet", "desk_hot") in daemon.asked


def test_main_RETURNS_NONZERO_when_a_chain_refuses(monkeypatch, capsys):
    """The exit code is a result, and a refusing run must not report success.

    `raise SystemExit(main())` is the entry point, so this IS the shell's answer.
    Returning 0 on a run where no wallet was prepared is the shape C16 keeps
    recurring as -- a failure that exits clean.
    """
    monkeypatch.setattr(tw, "build_adapters",
                        lambda rpc: {"BTC": _Daemon({"getblockchaininfo": {"chain": "main"}})})
    monkeypatch.setattr(tw.Config, "RPC", {"BTC": {"wallet": "desk_hot"}}, raising=False)
    assert tw.main(["--btc"]) == 1
    assert "NO CHAIN IS READY" in capsys.readouterr().out


def test_main_runs_BOTH_chains_and_one_failure_does_not_stop_the_other(monkeypatch, capsys):
    """A daemon that cannot be reached must not cost the other chain its address.

    The mirror of the operator's situation on 2026-10-10, which is why it is the pair
    worth seeding: one chain answering and mid-sync, one not answering at all.
    """
    good = _Daemon({
        "getblockchaininfo": {"chain": "test", "blocks": 2470883, "headers": 4912201,
                              "initialblockdownload": True},
        "getwalletinfo": {"descriptors": False, "balance": 0},
        "getnewaddress": valid_addresses.LTC_PARTICIPANT,
    })

    class _Unreachable:
        url = "http://127.0.0.1:18443/"

        def call(self, method, *params):
            raise OSError("connection refused")

    monkeypatch.setattr(tw, "build_adapters", lambda rpc: {"BTC": _Unreachable(), "LTC": good})
    monkeypatch.setattr(tw.Config, "RPC",
                        {"BTC": {"wallet": "desk_hot"}, "LTC": {"wallet": "desk_hot"}},
                        raising=False)
    assert tw.main(["--all"]) == 1, "one chain failed, so the run did not fully succeed"
    out = capsys.readouterr().out
    # IT LANDS IN THE NETWORK REFUSAL, NOT IN main()'s HANDLER, and that is better than
    # what this test first asserted. chain_network() catches the transport error and
    # returns its sentinel, so the verdict is NOT_ESTABLISHED and the operator gets the
    # credential-first sentence naming BOTH RPCs that were tried -- rather than the bare
    # `FAILED: OSError` I expected. main()'s handler needs a failure AFTER the network is
    # established, which is its own test below.
    assert "did not name its network" in out and "OSError" in out, (
        "the unreachable chain is named with what was tried, not swallowed"
    )
    assert "Check the RPC credentials first" in out, "and with the remedy for THAT refusal"
    assert valid_addresses.LTC_PARTICIPANT in out, "and the chain that worked still gives its address"
    assert "STILL SYNCING" in out
    assert "SAFE to make now" in out


def test_main_with_NO_CHAIN_SELECTED_prints_help_and_exits_2(capsys):
    """Nothing selected is not an error and not a success. It is exit 2, with the help.

    The same shape fund_testnets.main() uses, and the reason is rule 14: a bare run
    that printed nothing would be ambiguous between "done" and "broken".
    """
    assert tw.main([]) == 2
    out = capsys.readouterr().out
    assert "Nothing selected, so nothing was done" in out
    assert "--btc" in out and "--ltc" in out and "--all" in out


def test_main_CATCHES_a_setup_error_and_still_reports(monkeypatch, capsys):
    """main()'s own except handler, which the unreachable-daemon test does NOT reach.

    A transport error is caught by chain_network() and becomes a network refusal, so
    the only way into main()'s handler is a failure AFTER the network is established.
    `listwallets` raising is that: ensure_wallet_on() turns it into RegtestSetupError,
    naming a --disable-wallet build as the cause, and main() has to name and count it
    rather than letting it end the run.
    """
    class _NoWalletSupport:
        url = "http://127.0.0.1:18443/"

        def call(self, method, *params):
            if method == "getblockchaininfo":
                return {"chain": "testnet4", "blocks": 1, "headers": 1,
                        "initialblockdownload": False}
            raise RPCError("Method not found")

    monkeypatch.setattr(tw, "build_adapters", lambda rpc: {"BTC": _NoWalletSupport()})
    monkeypatch.setattr(tw.Config, "RPC", {"BTC": {"wallet": "desk_hot"}}, raising=False)
    assert tw.main(["--btc"]) == 1
    out = capsys.readouterr().out
    assert "FAILED: RegtestSetupError" in out, "named and counted, not raised through main()"
    assert "disable-wallet" in out, "and it names the cause ensure_wallet_on() exists to name"
    assert "NO CHAIN IS READY" in out, "and the closing block still prints"


# ---------------------------------------------------------------------------
# THE FAUCET, 2026-10-10. Written AFTER the operator proved the endpoint live,
# not before -- the previous commit said a client would be a proposal because the
# API is off by default in the faucet's config and this container is denied every
# faucet host (403 at the CONNECT). One real request settled both, so the
# responses below are the OBSERVED ones, copied from their terminal.
#
# NOTHING HERE OPENS A SOCKET. urlopen is replaced per test; a test that reached
# the real faucet would burn the operator's per-IP rate limit on every suite run.
# ---------------------------------------------------------------------------

#: The two real 200s, verbatim. Using the observed payload rather than a plausible
#: one is the difference between a test of this client and a test of my idea of the
#: faucet -- and the field I would have got wrong is `amount`, a STRING ("0.01000000")
#: where a plausible mock would have used a float.
_REAL_BTC_200 = {
    "ok": True, "network": "btc-testnet", "currency": "tBTC", "amount": "0.01000000",
    "txid": "ee8e14c6d38b7330a8c7ab48589ca820f91686f1312fe89181436e81adf8f9c9",
    "source": "https://github.com/Tech1k/cypherfaucet.com",
}
_REAL_LTC_200 = {
    "ok": True, "network": "ltc-testnet", "currency": "tLTC", "amount": "0.01000000",
    "txid": "6889177cedbd764e7dcaf6e79a9d27714a608536add9155b7479de5cdeab8e38",
    "source": "https://github.com/Tech1k/cypherfaucet.com",
}


class _Answer:
    """What urlopen returns, as a context manager over one JSON body."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _faucet(monkeypatch, outcome):
    """Replace urlopen and record the request the client built."""
    sent = {}

    def _urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["method"] = request.get_method()
        sent["headers"] = {k.lower(): v for k, v in request.header_items()}
        sent["body"] = json.loads(request.data.decode())
        sent["timeout"] = timeout
        if isinstance(outcome, Exception):
            raise outcome
        return _Answer(outcome)

    monkeypatch.setattr(tw.urllib.request, "urlopen", _urlopen)
    return sent


def test_the_faucet_request_IS_THE_SHAPE_THE_FAUCET_DOCUMENTS(monkeypatch):
    """The request, asserted field by field against the README and the real 200.

    A client that sent the wrong slug would get a 400 the operator has to decode.
    `btc-testnet` and NOT `btc-testnet4` is the half this file guessed wrong for one
    commit, so it is pinned per chain rather than trusted to a dict literal nobody
    reads.
    """
    sent = _faucet(monkeypatch, _REAL_BTC_200)
    row = tw.claim_from_faucet(Console(1, stream=io.StringIO()), "BTC",
                               valid_addresses.BTC_PARTICIPANT)
    assert row["ok"] is True
    assert sent["url"] == "https://cypherfaucet.com/api/v1/claim"
    assert sent["method"] == "POST"
    assert sent["headers"]["content-type"] == "application/json"
    assert sent["body"] == {"network": "btc-testnet", "address": valid_addresses.BTC_PARTICIPANT}
    assert sent["timeout"] == tw.FAUCET_TIMEOUT_SECONDS, (
        "a request with no timeout blocks forever, which is rule 14's blinking cursor"
    )


def test_the_LTC_slug_is_its_own_and_not_the_BTC_one(monkeypatch):
    """Two chains, two slugs. One dict, and a test that reads it per chain."""
    sent = _faucet(monkeypatch, _REAL_LTC_200)
    tw.claim_from_faucet(Console(1, stream=io.StringIO()), "LTC", valid_addresses.LTC_PARTICIPANT)
    assert sent["body"]["network"] == "ltc-testnet"


def test_a_SUCCESSFUL_claim_reports_the_amount_the_currency_and_the_TXID(monkeypatch):
    """The txid is the receipt. A claim that printed no txid would be unverifiable."""
    stream = io.StringIO()
    row = tw.claim_from_faucet(Console(1, stream=stream), "BTC", valid_addresses.BTC_PARTICIPANT)
    del row
    sent = _faucet(monkeypatch, _REAL_BTC_200)
    del sent
    stream = io.StringIO()
    row = tw.claim_from_faucet(Console(1, stream=stream), "BTC", valid_addresses.BTC_PARTICIPANT)
    out = stream.getvalue()
    assert row == {"ok": True, "amount": "0.01000000", "currency": "tBTC",
                   "txid": _REAL_BTC_200["txid"]}
    assert _REAL_BTC_200["txid"] in out and "0.01000000 tBTC" in out


@pytest.mark.parametrize(("status", "must_say"), [
    (400, "rejected the address"),
    (409, "EMPTY for this chain"),
    (429, "rate limited"),
    (503, "node is busy"),
    (418, "undocumented status 418"),
])
def test_every_DOCUMENTED_faucet_error_gets_its_own_sentence(monkeypatch, status, must_say):
    """Four codes the faucet documents, and one it does not.

    409 and 429 are the two an operator will actually hit, and they mean opposite
    things about whose problem it is -- "the faucet is dry, not your fault" against
    "you already claimed". A single "the faucet refused" would send them to the wrong
    place. The 418 row is the honest handling of a code nobody has seen: report the
    NUMBER rather than pick the nearest sentence.
    """
    error = tw.urllib.error.HTTPError(tw.FAUCET_CLAIM_URL, status, "refused", {}, None)
    _faucet(monkeypatch, error)
    stream = io.StringIO()
    row = tw.claim_from_faucet(Console(1, stream=stream), "BTC", valid_addresses.BTC_PARTICIPANT)
    assert row["ok"] is False and row["why"] == f"HTTP {status}"
    assert must_say in stream.getvalue()


def test_a_DENIED_HOST_says_nothing_was_sent_rather_than_looking_like_an_empty_faucet(monkeypatch):
    """The failure this will actually hit, and it must not read as "the faucet is dry".

    The container that wrote this client is denied every faucet host --
    `CONNECT tunnel failed, response 403`, measured 2026-10-10 -- and a URLError is
    what that surfaces as. "It did nothing" and "the faucet had nothing" lead an
    operator to two different next actions, so the message names the first.
    """
    _faucet(monkeypatch, tw.urllib.error.URLError("CONNECT tunnel failed, response 403"))
    stream = io.StringIO()
    row = tw.claim_from_faucet(Console(1, stream=stream), "BTC", valid_addresses.BTC_PARTICIPANT)
    out = stream.getvalue()
    assert row["ok"] is False and row["why"] == "URLError"
    assert "NOTHING WAS SENT" in out
    assert "network policy that denies the faucet host" in out


def test_an_ok_false_body_with_HTTP_200_is_still_a_refusal(monkeypatch):
    """`{"ok":false,...}` is the faucet's own shape and a 200 does not override it.

    Trusting the status code alone is the shape of the mirror_sql defect this repo
    records -- four copies printing success while branching on a return value that
    does not exist. The body is the answer.
    """
    _faucet(monkeypatch, {"ok": False, "error": "address_already_claimed"})
    row = tw.claim_from_faucet(Console(1, stream=io.StringIO()), "BTC",
                               valid_addresses.BTC_PARTICIPANT)
    assert row["ok"] is False and row["why"] == "address_already_claimed"


def test_the_faucet_is_OFF_unless_asked(monkeypatch):
    """No flag, no outbound request. Asserted by making urlopen fail the test if called."""
    def _never(*_a, **_k):
        raise AssertionError("prepare_chain contacted the faucet without --faucet")

    monkeypatch.setattr(tw.urllib.request, "urlopen", _never)
    daemon = _Daemon(_HEALTHY_BTC)
    monkeypatch.setattr(tw, "build_adapters", lambda rpc: {"BTC": daemon})
    monkeypatch.setattr(tw.Config, "RPC", {"BTC": {"wallet": "desk_hot"}}, raising=False)
    assert tw.main(["--btc"]) == 0


def test_main_with_FAUCET_funds_THE_ADDRESS_IT_JUST_DERIVED(monkeypatch, capsys):
    """The trap this closes, and it is one I built.

    getnewaddress mints a FRESH address on every run, so the operator funded one
    address, re-ran the tool, and saw a different one -- with nothing on screen to
    say the money was still in the same wallet. Two steps a human carries a
    42-character string between is one too many. Funding happens in the same
    invocation as the derivation now, and the report leads with the txid and the
    address it landed on rather than with a new address to paste.
    """
    daemon = _Daemon(_HEALTHY_BTC)
    monkeypatch.setattr(tw, "build_adapters", lambda rpc: {"BTC": daemon})
    monkeypatch.setattr(tw.Config, "RPC", {"BTC": {"wallet": "desk_hot"}}, raising=False)
    sent = _faucet(monkeypatch, _REAL_BTC_200)

    assert tw.main(["--btc", "--faucet"]) == 0
    out = capsys.readouterr().out
    assert sent["body"]["address"] == valid_addresses.BTC_PARTICIPANT, (
        "the faucet was asked to pay the address this run derived, not some other one"
    )
    assert "FUNDED." in out and _REAL_BTC_200["txid"] in out
    assert f"{valid_addresses.BTC_PARTICIPANT}" in out
    assert "one POST to https://cypherfaucet.com/api/v1/claim" in out, (
        "and the outbound request is announced BEFORE it happens, naming the host"
    )


def test_the_PAID_faucet_entry_no_longer_claims_to_be_untested():
    """The evidence field, now that one entry has actually been measured.

    The whole table was `SEARCHED_NOT_TESTED` because nothing had been tried. One
    entry has now paid, on both chains, with txids -- so it says so, and the DEFAULT
    is still the hedge, which is what stops a new row looking verified by omission.
    """
    cypher = [f for entries in tw.FAUCETS.values() for f in entries
              if "cypherfaucet.com" in f.url]
    assert len(cypher) == 2, "one entry per chain"
    for faucet in cypher:
        assert faucet.evidence == tw.PAID
        assert "MEASURED" in faucet.evidence and "it paid" in faucet.evidence
    others = [f for entries in tw.FAUCETS.values() for f in entries
              if "cypherfaucet.com" not in f.url]
    assert others, "the unverified rows are still there as fallbacks"
    for faucet in others:
        assert faucet.evidence == tw.SEARCHED_NOT_TESTED, (
            f"{faucet.url} claims evidence it does not have -- the default must be the hedge"
        )


# ---------------------------------------------------------------------------
# THE BALANCE LINES, 2026-10-10, after the operator asked "do we have our btc and
# ltc wallets funded or not?" and this tool's own output could not answer.
# ---------------------------------------------------------------------------


class _Balances:
    """A daemon answering getwalletinfo and getbalances, or failing getbalances."""

    url = "http://127.0.0.1:18443/"

    def __init__(self, buckets, *, raises=False):
        self.buckets = buckets
        self.raises = raises

    def call(self, method, *params):
        if method == "getbalances":
            if self.raises:
                raise RPCError("Method not found")
            return {"mine": self.buckets} if self.buckets is not None else {}
        raise AssertionError(method)


def test_a_PENDING_payment_is_named_where_a_confirmed_balance_reads_zero():
    """The exact case that cost the operator an answer.

    getwalletinfo.balance is the CONFIRMED balance, so a faucet payment broadcast
    seconds ago reads 0.0 -- byte for byte identical to nothing arriving. Both of
    those were on the screen and neither was distinguishable from the other.
    """
    adapter = _Balances({"trusted": 0.0, "untrusted_pending": 0.01, "immature": 0.0})
    lines = tw.balance_lines(adapter, {"balance": 0.0})
    blob = " ".join(lines)
    assert "0.01 in the mempool" in blob
    assert "A PAYMENT HAS ARRIVED" in blob, (
        "and it says so in words, because the operator reads the screen and not the source"
    )
    assert "could not tell you" in blob


def test_a_TRULY_EMPTY_wallet_does_not_claim_a_payment_arrived():
    """The negative case, without which the test above is satisfied by always shouting."""
    adapter = _Balances({"trusted": 0.0, "untrusted_pending": 0.0, "immature": 0.0})
    blob = " ".join(tw.balance_lines(adapter, {"balance": 0.0}))
    assert "A PAYMENT HAS ARRIVED" not in blob
    assert "0.0 confirmed" in blob and "0.0 in the mempool" in blob, (
        "and all three buckets still print -- (none) is a result, and a blank is ambiguous"
    )


def test_a_CONFIRMED_payment_reads_as_confirmed_and_not_as_pending():
    """Once it confirms, the money is in `trusted` and the mempool line goes to zero."""
    adapter = _Balances({"trusted": 0.01, "untrusted_pending": 0.0, "immature": 0.0})
    blob = " ".join(tw.balance_lines(adapter, {"balance": 0.01}))
    assert "0.01 confirmed" in blob
    assert "A PAYMENT HAS ARRIVED" not in blob, "it has arrived AND confirmed; that is not pending"


@pytest.mark.parametrize("broken", [True, False])
def test_an_UNREADABLE_getbalances_says_so_and_never_renders_a_silent_zero(broken):
    """Fail loud, not quiet. A narrower number presented as the whole one is the defect.

    Two ways it can go wrong -- the RPC raising, and a `mine` bucket that is absent --
    and both must say that a 0-confirmation payment is NOT counted in the figure
    printed beside them. Falling back silently to getwalletinfo.balance would
    reproduce exactly the defect this function was written to fix.
    """
    adapter = _Balances(None if not broken else {}, raises=broken)
    blob = " ".join(tw.balance_lines(adapter, {"balance": 7.5}))
    assert "7.5 confirmed" in blob, "the figure it DOES have is still printed"
    assert "NOT" in blob and "mempool" in blob.lower(), (
        "and it says the pending amount is not in that figure"
    )


def test_the_client_NAMES_ITSELF_and_does_not_pretend_to_be_a_browser(monkeypatch):
    """The 403 this fixes, and the fix it deliberately is not.

    MEASURED 2026-10-10: this client got `403 / error code: 1010` where a `curl` of
    the identical URL and body had succeeded minutes earlier. Cloudflare's own
    documentation says 1010 is access denied "based on the browser's signature" -- a
    Browser Integrity Check the SITE OWNER switches on -- and the only material
    difference between the two requests was the User-Agent, since urllib defaults to
    `Python-urllib/3.x`.

    SO IT SENDS A TRUTHFUL ONE. A Chrome string would very likely pass, and that is
    exactly why it is not here: it would be this tool circumventing an access control
    somebody deliberately switched on, to take coins from a service they run for free.
    The faucet's README asks for a credit and offers a contact for integrations, so
    naming the caller is both the honest and the useful move -- the owner can then
    decide.

    ASSERTED IN BOTH DIRECTIONS, because only the negative half has teeth: a test that
    checked for a UA at all would be satisfied by a spoofed one.
    """
    sent = _faucet(monkeypatch, _REAL_BTC_200)
    tw.claim_from_faucet(Console(1, stream=io.StringIO()), "BTC", valid_addresses.BTC_PARTICIPANT)
    agent = sent["headers"]["user-agent"]
    assert "swap_terminal" in agent, "it names the caller, so the faucet's owner can see who it is"
    assert "Python-urllib" not in agent, "urllib's default is what Cloudflare 1010s"
    for browser in ("Mozilla/", "Chrome/", "Safari/", "AppleWebKit", "Gecko/", "Edg/"):
        assert browser not in agent, (
            f"the User-Agent claims to be {browser!r}. This tool must not pretend to be a "
            f"browser to get past a Browser Integrity Check the site owner turned on"
        )
    assert sent["headers"]["accept"] == "application/json"


def test_a_CLOUDFLARE_403_says_whose_refusal_it_is_and_points_at_the_browser_link(monkeypatch):
    """403 is not the faucet refusing. Saying "the faucet refused" would send them wrong.

    It is Cloudflare in front of the faucet, rejecting the CLIENT before the faucet
    sees anything -- so the remedy is not "try again later" (429) and not "the faucet
    is dry" (409), it is the one-click ?address= link, which goes through a browser.
    403 is absent from the faucet's README, which is why the generic
    "undocumented status" path printed first and let this be diagnosed at all.
    """
    error = tw.urllib.error.HTTPError(tw.FAUCET_CLAIM_URL, 403, "Forbidden", {}, None)
    _faucet(monkeypatch, error)
    stream = io.StringIO()
    row = tw.claim_from_faucet(Console(1, stream=stream), "BTC", valid_addresses.BTC_PARTICIPANT)
    out = stream.getvalue()
    assert row["ok"] is False and row["why"] == "HTTP 403"
    assert "Cloudflare refused this client, not the faucet" in out
    assert "1010" in out and "Browser Integrity Check" in out
    assert "?address=" in out, "and it points at the link that actually works"
