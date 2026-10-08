"""fund_desk.py: the arithmetic, the custody direction, and what reaches a daemon.

Role: test module (verification only)
Reads: fund_desk.py and the real adapters it builds
Writes: nothing
Can move funds: no -- every adapter here is tests/recording_rpc_adapter.py, which is
      the REAL RPCAdapter with its socket replaced by a list, so `sendtoaddress`
      runs the production quantization and appends to that list instead of
      connecting. The ICP side is a closure that records.
Mainnet-safe: yes; no socket is opened, and the two tests that are ABOUT a mainnet
      port assert that no adapter is constructed at all.

WHAT THESE ARE FOR. The tool moves real coins between two wallets the operator
owns, so the hazards are not in the arithmetic -- they are in the ordering. Three
of them, each with a test that fails if the order changes:

  a mainnet port must be refused BEFORE a socket    the GridcoinAdapter constructor
                                                    is replaced with one that
                                                    raises, so "it was built" is
                                                    the failure
  a self-transfer must be refused BEFORE an unlock  the recording adapter's `sent`
                                                    list must stay empty
  the unlock must bracket the send                  the recorded method order is
                                                    asserted as a whole sequence,
                                                    not as a membership test

The last one is the one that would otherwise rot quietly: a send that happens
outside the unlock window fails against a locked wallet and succeeds against an
unlocked one, so a test asserting only that `sendtoaddress` happened would pass on
a host where the wallet was already open.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

APP_ROOT = Path(__file__).resolve().parent.parent
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from gridcoin_credentials import OperatorEndpoint  # noqa: E402

import fund_desk  # noqa: E402
from fund_desk import (  # noqa: E402
    MECHANISMS,
    MINT_FEE_E8S,
    OPERATOR_UNLOCK_ENV_VAR,
    _port_gate,
    amount_to_move,
    build_parser,
    direction_verdict,
    grc_plan,
    grc_send,
    icp_mint,
)
from tests.recording_rpc_adapter import RecordingRPCAdapter  # noqa: E402
from tests.valid_addresses import GRC_DESK_DEPOSIT, GRC_PAYOUT  # noqa: E402

#: Fixture credentials for the operator endpoint. NOT SECRETS: `wire()` below
#: replaces the GridcoinAdapter constructor entirely, so these never reach an
#: adapter, a socket or a daemon.
#:
#: NAMED CONSTANTS RATHER THAN LITERALS AT THE CALL SITE, which is what answers
#: ruff's S106 here instead of a `noqa`. S106 fires on a string LITERAL passed to a
#: `password=` argument, and it was right to: a literal there is indistinguishable
#: from a real one to a checker, to a grep, and to a reader skimming. A reference to
#: a constant whose name says what it is removes the cause rather than quieting the
#: finding (rule 19), and says more than the suppression would have.
FIXTURE_RPC_USER = "operator-rpc-user"
FIXTURE_RPC_AUTH = "operator-rpc-auth-value-that-reaches-no-daemon"

DESK_PORT = 25779
OPERATOR_PORT = 25715
MAINNET_PORT = 15715


# ------------------------------------------------------------- the arithmetic


@pytest.mark.parametrize(("held", "target", "available", "expected"), [
    (11.0, 100.0, 1000.0, 89.0),      # the ordinary case: move the difference
    (100.0, 100.0, 1000.0, 0.0),      # already there
    (200.0, 100.0, 1000.0, 0.0),      # above it
    (11.0, 100.0, 50.0, 50.0),        # capped at the source
    (11.0, 100.0, 0.0, 0.0),          # source empty
    (11.0, 0.0, 1000.0, 0.0),         # no target
    (11.0, -5.0, 1000.0, 0.0),        # negative target
])
def test_the_amount_is_the_shortfall_capped_at_the_source(held, target, available, expected):
    amount, why = amount_to_move(held, target, available)
    assert amount == pytest.approx(expected)
    assert why, "every outcome carries a sentence, including the ones that move nothing"


def test_a_capped_amount_says_it_is_capped_and_says_what_is_still_short():
    """Rule 14: a number that silently means something else is the defect.

    MUTATION: `return source_available, ...` -> `return shortfall, ...` in the
    capped branch. This test still passes on the amount (it asserts 50.0 against
    `source_available`) -- no: it fails, because the mutation returns 89.0. Verified
    2026-10-07. What it ALSO pins is the word CAPPED, without which an operator
    reading `amount 50.0` against `target 100` has to do the subtraction to find out
    they are still short.
    """
    amount, why = amount_to_move(11.0, 100.0, 50.0)
    assert amount == 50.0
    assert "CAPPED AT THE SOURCE" in why
    assert "still short by 39.0" in why


#: The measured GRC chain fee, from config.GRC_NETWORK_FEE_RESERVE. Named here so
#: the two tests below read against a figure rather than a literal.
GRC_CHAIN_FEE = 0.001


def test_the_chain_fee_stays_with_the_source_and_never_shrinks_the_amount():
    """The defect the operator's first real dry run exposed, 2026-10-07.

    It printed `amount 3687.32154338` against an operator balance of exactly
    3687.32154338 -- the whole wallet -- and `sendtoaddress` takes the fee from the
    sending wallet's own inputs ON TOP of what it delivers. That send had nothing
    left to pay the fee with and would have come back "Insufficient funds": an
    --apply run failing for a reason the dry run printed as a go.

    BOTH HALVES ARE ASSERTED, because getting this backwards is the easy mistake.
    With room to spare, the amount is the FULL shortfall -- the fee shrinks what the
    source can spare, never what the recipient receives, since a payout short by the
    fee leaves the desk short by the fee. With the balance exactly equal to the
    shortfall, the amount drops by the fee.

    MUTATION: subtract the reserve from the returned amount instead of from the
    source. The first assertion fails (88.999 where 89.0 is owed). Verified 2026-10-07.
    """
    plenty, _why = amount_to_move(11.0, 100.0, 1000.0, GRC_CHAIN_FEE)
    assert plenty == pytest.approx(89.0), "the recipient gets the full shortfall"

    exact, why = amount_to_move(11.0, 100.0, 89.0, GRC_CHAIN_FEE)
    assert exact == pytest.approx(89.0 - GRC_CHAIN_FEE), "the source keeps the fee back"
    assert "CAPPED AT THE SOURCE" in why
    assert "stays behind for the chain fee" in why


def test_a_source_holding_less_than_the_fee_can_spare_nothing():
    """And it says so as a fact about the source rather than a negative amount.

    MUTATION: drop the `max(..., 0.0)`. `spare` goes to -0.0009, which is falsey
    enough to still refuse here but would read as a direction anywhere it was
    printed. The assertion on the sentence is what catches it.
    """
    amount, why = amount_to_move(11.0, 100.0, 0.0005, GRC_CHAIN_FEE)
    assert amount == 0.0
    assert "can spare 0.0" in why
    assert "fact about the SOURCE" in why


def test_with_no_reserve_the_sentence_says_nothing_about_a_fee():
    """A mint has no chain fee, so the fee clause must not appear in its reasoning.

    icp_plan() passes source_reserve=0.0 explicitly. A top-up whose explanation
    mentioned a fee the ledger does not charge would send the reader looking for a
    figure that is not there.
    """
    _amount, why = amount_to_move(1.0, 1000.0, 999.0, 0.0)
    assert "chain fee" not in why


def test_being_already_funded_and_an_empty_source_are_different_sentences():
    """Both move 0.0, and conflating them is rule 14's "did nothing" failure."""
    _, funded = amount_to_move(500.0, 100.0, 1000.0)
    _, empty = amount_to_move(11.0, 100.0, 0.0)
    assert "at or above the target" in funded
    assert "fact about the SOURCE" in empty
    assert funded != empty


# ------------------------------------------------------- the custody direction


def test_the_only_allowed_direction_is_desk_owns_and_source_does_not():
    assert direction_verdict(GRC_DESK_DEPOSIT, desk_owns=True, source_owns=False) == ""


@pytest.mark.parametrize(("desk_owns", "source_owns", "fragment"), [
    (True, True, "self-transfer"),
    (None, False, "the desk could not say"),
    (True, None, "the source could not say"),
    (False, False, "DESK does not own"),
    (None, None, "NOT ESTABLISHED"),
])
def test_every_other_combination_is_refused_and_says_which(desk_owns, source_owns, fragment):
    """The whole truth table, one row per test, because each row is a different bug.

    MUTATION: `if source_owns:` -> `if source_owns is True and desk_owns is False:`.
    The self-transfer row fails -- which is the expensive one, since that send
    broadcasts, confirms, returns a txid, costs a chain fee and moves nothing.
    Verified 2026-10-07.
    """
    why = direction_verdict(GRC_DESK_DEPOSIT, desk_owns=desk_owns, source_owns=source_owns)
    assert why
    assert fragment in why


def test_no_destination_is_refused_before_any_ownership_question():
    why = direction_verdict("", desk_owns=True, source_owns=False)
    assert "no address to send to" in why


def test_an_unknown_answer_is_not_treated_as_safe():
    """Rule 2's distinction, as a test: "could not find a caller" is not "no caller".

    MUTATION: make the None branch `return ""`. Both None rows above flip to
    allowed, which is the direction that sends money on an unanswered question.
    """
    assert direction_verdict(GRC_DESK_DEPOSIT, desk_owns=None, source_owns=None) != ""


# ------------------------------------------------------------- the port gate


def test_both_gridcoin_test_ports_are_allowed_and_mainnet_is_not():
    assert _port_gate("GRC", OPERATOR_PORT, "the operator's", "GRC_OPERATOR_RPC_PORT") == ""
    assert _port_gate("GRC", DESK_PORT, "the desk's", "GRC_RPC_PORT") == ""
    refused = _port_gate("GRC", MAINNET_PORT, "the operator's", "GRC_OPERATOR_RPC_PORT")
    assert "MAINNET" in refused
    assert "GRC_OPERATOR_RPC_PORT" in refused, (
        "may_read_a_wallet names the DESK's variable, so the wrapper has to add which one applies "
        "here -- a refusal that names the wrong variable is one an operator routes around"
    )


def test_an_unrecognized_port_is_refused_rather_than_guessed():
    assert "not a GRC port this tree knows" in _port_gate("GRC", 31337, "the operator's", "X")


# --------------------------------------------------- what reaches a GRC daemon


class Wallet(RecordingRPCAdapter):
    """A recording GRC adapter with a settable balance and one known address."""

    def __init__(self, balance: float, address: str = "", owns: tuple[str, ...] = ()):
        super().__init__("GRC")
        self._balance = balance
        self._address = address
        self.owned_addresses = set(owns)

    def get_balance(self) -> float:
        return self._balance

    def own_address(self) -> str:
        return self._address

    @property
    def methods(self) -> list[str]:
        return [method for method, _params in self.calls]


def wire(monkeypatch, *, source: Wallet | None, desk: Wallet,
         port: int = OPERATOR_PORT, desk_port: int = DESK_PORT) -> dict:
    """Point fund_desk at two recording wallets. Returns what it recorded.

    `source=None` replaces the GridcoinAdapter CONSTRUCTOR with one that raises, so
    a test can assert a refusal happened BEFORE anything was built. That is the only
    honest way to test "no socket was opened": asserting an empty call list would
    also pass if the adapter were built and simply not used.
    """
    built: dict = {}

    def constructor(**kwargs):
        if source is None:
            raise AssertionError(
                f"a GridcoinAdapter was CONSTRUCTED for port {kwargs.get('port')} -- the refusal "
                f"was supposed to happen before anything opened a socket"
            )
        built["kwargs"] = kwargs
        return source

    monkeypatch.setattr(fund_desk, "GridcoinAdapter", constructor)
    monkeypatch.setattr(fund_desk, "build_adapters", lambda rpc: {"GRC": desk})
    monkeypatch.setattr(fund_desk, "operator_endpoint", lambda: (
        OperatorEndpoint(host="127.0.0.1", port=port, user=FIXTURE_RPC_USER,
                         password=FIXTURE_RPC_AUTH), ""),
    )
    monkeypatch.setattr(fund_desk.Config, "RPC", {
        "GRC": {"user": FIXTURE_RPC_USER, "password": FIXTURE_RPC_AUTH, "host": "127.0.0.1",
                "port": desk_port, "wallet": "", "timeout": 30.0},
    })
    return built


def test_a_mainnet_operator_port_builds_no_adapter_at_all(monkeypatch):
    """The refusal this file exists for, and it has to come before construction.

    An adapter built against the operator's MAINNET wallet is already the defect
    even if nothing is then called on it: `wallet_custody.py` records the 2026-09-25
    run where a balance reader hit 15715 and printed 157,797 real GRC into a
    terminal whose output goes into a chat transcript.
    """
    wire(monkeypatch, source=None, desk=Wallet(11.0), port=MAINNET_PORT)
    plan = grc_plan(lambda _text: None, 100.0)
    assert "MAINNET" in plan["refusal"]


def test_one_daemon_serving_both_is_refused_without_a_socket(monkeypatch):
    """Same host and same port IS one wallet, and two integers say so for free."""
    wire(monkeypatch, source=None, desk=Wallet(11.0), port=DESK_PORT, desk_port=DESK_PORT)
    plan = grc_plan(lambda _text: None, 100.0)
    assert "ONE daemon" in plan["refusal"]


def test_a_destination_the_source_also_owns_is_refused_and_nothing_is_sent(monkeypatch):
    source = Wallet(1000.0, owns=(GRC_DESK_DEPOSIT,))
    desk = Wallet(11.0, address=GRC_DESK_DEPOSIT, owns=(GRC_DESK_DEPOSIT,))
    wire(monkeypatch, source=source, desk=desk)
    plan = grc_plan(lambda _text: None, 100.0)
    assert "self-transfer" in plan["refusal"]
    assert source.sent == [], "a refused plan must not have sent anything"


def test_the_plan_keeps_the_chain_fee_back_from_the_whole_source_balance(monkeypatch):
    """End to end, on the shape the operator actually hit: target far above the source.

    The source's ENTIRE balance is the cap, so this is the run where the missing
    reserve would have produced an unsendable amount. Asserted against the real
    config figure through the real chain_fee_for(), not against a literal.
    """
    source = Wallet(3687.32154338)
    desk = Wallet(11.00248643, address=GRC_DESK_DEPOSIT, owns=(GRC_DESK_DEPOSIT,))
    wire(monkeypatch, source=source, desk=desk)
    plan = grc_plan(lambda _text: None, 120549.32)
    fee, how = fund_desk.chain_fee_for("GRC")
    assert how == "GRC_NETWORK_FEE_RESERVE"
    assert plan["amount"] == pytest.approx(3687.32154338 - fee)
    assert plan["amount"] < 3687.32154338, (
        "the whole balance is not sendable: sendtoaddress pays the fee from the sending wallet's "
        "own inputs on top of what it delivers, so a full-balance send has nothing to pay it with"
    )


def test_the_chain_fee_comes_from_the_one_authority_and_not_the_config_class():
    """chain_fee_for() builds a MAPPING, because get_network_fee_reserve() indexes one.

    The first version passed the Config CLASS and failed with
    `TypeError: argument of type 'type' is not iterable` out of that function's
    `if key not in config`. Four tests caught it; a live run would have been the
    operator's third refusal in a row.
    """
    fee, how = fund_desk.chain_fee_for("GRC")
    assert isinstance(fee, float)
    assert fee > 0
    assert how == "GRC_NETWORK_FEE_RESERVE"
    missing, why = fund_desk.chain_fee_for("DOGE")
    assert missing is None
    assert "DOGE_NETWORK_FEE_RESERVE" in why


def test_the_go_path_plans_the_shortfall_and_sends_nothing_by_itself(monkeypatch):
    """grc_plan() is the whole dry run, so it must reach the amount without sending.

    MUTATION: make grc_plan() call send_to_address at the end. `source.sent == []`
    fails, which is what keeps a dry run a dry run.
    """
    source = Wallet(1000.0)
    desk = Wallet(11.0, address=GRC_DESK_DEPOSIT, owns=(GRC_DESK_DEPOSIT,))
    wire(monkeypatch, source=source, desk=desk)
    plan = grc_plan(lambda _text: None, 100.0)
    assert plan["refusal"] == ""
    assert plan["amount"] == pytest.approx(89.0)
    assert plan["destination"] == GRC_DESK_DEPOSIT
    assert source.sent == []
    assert "sendtoaddress" not in source.methods


def test_the_destination_is_read_and_never_created(monkeypatch):
    """getnewaddress is a wallet WRITE and this tool does not make one.

    Pinned against the DESK adapter's recorded methods rather than against the
    source file's text, because the source-text version of this assertion is what
    tests/test_custody_separation.py already does for wallet_custody.py -- and a
    behavioral one also catches a write that arrives through a helper.
    """
    source = Wallet(1000.0)
    desk = Wallet(11.0, address=GRC_DESK_DEPOSIT, owns=(GRC_DESK_DEPOSIT,))
    wire(monkeypatch, source=source, desk=desk)
    grc_plan(lambda _text: None, 100.0)
    assert "getnewaddress" not in desk.methods
    assert "getnewaddress" not in source.methods


def test_the_send_happens_inside_the_unlock_window_in_that_order(monkeypatch):
    """lock -> unlock -> send -> lock -> unlock for staking, as a SEQUENCE.

    THE WHOLE ORDER, not a membership test. A send outside the window succeeds
    against a wallet that happens to be open already and fails against a locked one,
    so `"sendtoaddress" in methods` would be green on the wrong host.

    MUTATION: move the send above the `with` in grc_send(). The sequence assertion
    fails naming the order it got. Verified 2026-10-07.
    """
    source = Wallet(1000.0)
    desk = Wallet(11.0, address=GRC_DESK_DEPOSIT, owns=(GRC_DESK_DEPOSIT,))
    wire(monkeypatch, source=source, desk=desk)
    monkeypatch.setenv(OPERATOR_UNLOCK_ENV_VAR, "not-the-real-one-and-never-reaches-a-daemon")
    plan = grc_plan(lambda _text: None, 100.0)

    grc_send(plan)

    assert [m for m in source.methods if m in
            ("walletlock", "walletpassphrase", "sendtoaddress")] == [
        "walletlock", "walletpassphrase", "sendtoaddress", "walletlock", "walletpassphrase",
    ]
    assert source.sent == [pytest.approx(89.0)]


def test_no_passphrase_in_the_environment_sends_nothing_and_names_the_variable(monkeypatch):
    source = Wallet(1000.0)
    desk = Wallet(11.0, address=GRC_DESK_DEPOSIT, owns=(GRC_DESK_DEPOSIT,))
    wire(monkeypatch, source=source, desk=desk)
    monkeypatch.delenv(OPERATOR_UNLOCK_ENV_VAR, raising=False)
    plan = grc_plan(lambda _text: None, 100.0)
    with pytest.raises(RuntimeError) as raised:
        grc_send(plan)
    assert OPERATOR_UNLOCK_ENV_VAR in str(raised.value)
    assert "GRIDCOIN_WALLET_PASSPHRASE is the desk's" in str(raised.value)
    assert source.sent == []
    assert "walletpassphrase" not in source.methods, (
        "the wallet must not even be locked before the passphrase is known to exist -- locking it "
        "and failing is what leaves the operator's staking wallet off"
    )


def test_the_tool_accepts_no_passphrase_argument_at_all():
    """There is no flag, and there is no prompt. argv is world-readable via /proc.

    Asserted over the parser rather than over the source text so a flag added by any
    route fails this, and over the source text as well for the `input(`/`getpass`
    half, which a parser cannot see.
    """
    advertised = {flag for action in build_parser()._actions for flag in action.option_strings}
    for forbidden in ("--passphrase", "--password", "--pass", "--secret"):
        assert forbidden not in advertised
    source = (APP_ROOT / "fund_desk.py").read_text()
    assert "input(" not in source
    assert "getpass" not in source


# --------------------------------------------------------- what reaches the ledger


class Ledger:
    """A recording stand-in for the dfx transport closure."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, canister: str, method: str, argument: str, output: str = "idl") -> str:
        self.calls.append((canister, method, argument))
        return self.reply


def mint_plan(reply: str) -> tuple[dict, Ledger]:
    ledger = Ledger(reply)
    return {
        "refusal": "", "asset": "ICP", "destination": "a" * 64, "amount": 5.0, "why": "",
        "held": 1.0, "available": None, "mint": ledger, "ledger": "bkyz2-fmaaa-aaaaa-qaaaq-cai",
        "source_label": "",
    }, ledger


def test_a_mint_names_a_zero_fee_and_the_desk_account():
    """The one field that makes a mint differ from a payout.

    MUTATION: `fee_e8s=MINT_FEE_E8S` -> `fee_e8s=10_000`. This fails. A non-zero fee
    from the minting account comes back BadFee, and chains/icp.py deliberately does
    not retry one -- so the mutation produces a tool that always fails, loudly, for
    a reason whose cause is one token away.
    """
    plan, ledger = mint_plan("(variant { Ok = 7 })")
    assert icp_mint(plan, 1_791_291_996_571_772_365) == "7"
    [(canister, method, argument)] = ledger.calls
    assert canister == "bkyz2-fmaaa-aaaaa-qaaaq-cai"
    assert method == "transfer"
    assert f"fee = record {{ e8s = {MINT_FEE_E8S} : nat64 }}" in argument
    assert "amount = record { e8s = 500000000 : nat64 }" in argument
    assert 'to = blob "' in argument, "the blob keyword is load-bearing: without it candid reads 32 bytes as text"
    assert "timestamp_nanos = 1791291996571772365" in argument


def test_a_duplicate_is_the_same_mint_and_not_a_failure():
    """TxDuplicate carries the ORIGINAL block index, which is what the key is for."""
    plan, _ledger = mint_plan("(variant { Err = variant { TxDuplicate = record { duplicate_of = 7 } } })")
    assert icp_mint(plan, 1) == "7"


def test_an_unrecognized_reply_raises_and_does_not_retry():
    """A retry with a fresh key is a SECOND mint, so there is no retry.

    MUTATION: add a retry on failure. `len(ledger.calls) == 1` fails, which is the
    assertion that matters -- the exception is recoverable and a double mint is not.
    """
    plan, ledger = mint_plan("(variant { Err = variant { BadFee = record { expected_fee = record { e8s = 10_000 } } } })")
    with pytest.raises(RuntimeError) as raised:
        icp_mint(plan, 1)
    assert "NOT ESTABLISHED" in str(raised.value)
    assert "BadFee" in str(raised.value)
    assert len(ledger.calls) == 1


def test_the_mint_identity_reaches_dfx_and_the_read_identity_does_not(monkeypatch):
    """The gap this change filled: the compose transport can now be asked to sign.

    Asserted on the argv dfx would have been given, the same way
    tests/test_icp_adapter.py asserts its two siblings -- and the read transport is
    in the same test so the asymmetry is visible: a read must stay unsigned, because
    forcing anonymous inside the replica container is what would make every ICP
    payout debit an empty account.
    """
    import chains.icp as icp_module  # noqa: PLC0415 -- checked: imported here beside the monkeypatch of its own subprocess, so a reader sees what is being replaced.

    seen: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        seen.append(argv)
        raise AssertionError("stop here -- the argv is the whole assertion")

    monkeypatch.setattr(icp_module.subprocess, "run", fake_run)
    for identity in ("minter", ""):
        with pytest.raises(AssertionError):
            icp_module.dfx_transport("icp-replica", 5.0, identity=identity)(
                "a-canister", "transfer", "()",
            )
    minted, read = seen
    assert minted[:2] == ["docker", "compose"]
    assert "--identity" in minted and minted[minted.index("--identity") + 1] == "minter"
    assert "--network" not in minted, "the compose transport reaches the replica directly"
    assert "--identity" not in read, (
        "an unsigned call must stay unsigned: inside the replica container the desk's own identity "
        "is the default, and that is the one a * -> ICP payout must sign with"
    )


# --------------------------------------------------------------------- the CLI


def test_every_advertised_flag_parses():
    advertised = {flag for action in build_parser()._actions for flag in action.option_strings}
    for flag in ("--asset", "--target", "--minter-identity", "--idempotency-key", "--apply"):
        assert flag in advertised, f"{flag} is documented and the parser does not know it"
    parsed = build_parser().parse_args(
        ["--asset", "ICP", "--target", "1000", "--idempotency-key", "17", "--apply"]
    )
    assert parsed.asset == "ICP"
    assert parsed.target == 1000.0
    assert parsed.idempotency_key == 17
    assert parsed.apply


def test_an_asset_with_no_mechanism_is_refused_by_the_parser():
    """--asset is a choices= list, so an unimplemented chain cannot be asked for.

    The alternative -- accepting any asset and refusing inside -- would print a
    plausible plan for a chain with no top-up mechanism at all.
    """
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--asset", "DOGE", "--target", "1"])


def test_both_mechanisms_say_whether_they_mint_or_send():
    """MINT and SEND are not interchangeable and the operator reads this line.

    One creates tokens and debits nobody; the other moves coins out of a wallet
    somebody owns and needs that wallet's passphrase. A description that blurred
    them would be the one thing on screen before --apply.
    """
    assert MECHANISMS["ICP"].startswith("MINT")
    assert MECHANISMS["GRC"].startswith("SEND")
    assert "NOT STAKING" in MECHANISMS["GRC"]
    assert GRC_PAYOUT not in MECHANISMS["GRC"], "no address belongs in a mechanism description"
