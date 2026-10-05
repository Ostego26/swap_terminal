"""The refusals that stop a devnet deposit going out wrong. Pure functions only.

Role: test (no socket, no subprocess except a stubbed one, no key read)
Reads: pay_test_deposit.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHAT THESE ARE ABOUT. pay_test_deposit.py prints "about to send" and then hands the
transfer to the Solana CLI. Every line above that point is a claim that the send
will work, so each refusal here is a claim this file checks.

THE GAP THESE CLOSE, from the operator's 2026-10-02 dry run. Every refusal in that
file was about configuration or about the swap; nothing asked whether the SENDING
keypair had the money. The block printed

    about to send   solana transfer ... 0.25 ...

for a swap twenty-five times the size of every earlier rehearsal, funded by a
keypair that had been paying for a day of them. An insufficient balance would have
surfaced in the CLI, after this file had announced it was sending -- rule 14's
"announce before, not only after" read in the other direction: a block that says
it is about to do something has claimed it can.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

import pay_test_deposit as tool  # noqa: E402
from tests.valid_addresses import GRC_PAYOUT, SOL_DEPOSIT_ACCOUNT  # noqa: E402

#: The operator's funded devnet keypair path, and their swap's amount.
#:
#: The ADDRESSES come from tests/valid_addresses.py, not from literals here.
#: tests/test_address_literals_are_valid.py caps how many address literals the tree
#: may hold, and the first draft of this file pushed the count over it -- the SECOND
#: time that happened today, after tests/test_fee_ledger.py did the same thing this
#: morning. A shared fixture cannot be mistyped and says what it is for; writing
#: one out is how the 25 undecodable literals that ratchet exists for arrived, one
#: at a time, each by somebody who needed a plausible string for one test.
KEYPAIR = "/home/mpjones26/.config/solana/swap-terminal-2026-09-25T17-58-55-491Z.json"
PUBKEY = SOL_DEPOSIT_ACCOUNT
AMOUNT = 0.25
LAMPORTS_PER_SOL = 1_000_000_000


class FakeCompleted:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode


def stub_cluster(monkeypatch, lamports: int | None = None, raises: bool = False):
    """A SolanaAdapter whose getBalance answers, or refuses to."""
    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        def call(self, method, *params):
            if raises:
                raise RuntimeError("getBalance returned HTTP 429")
            assert method == "getBalance", f"unexpected call {method}"
            return {"value": lamports}

    monkeypatch.setattr(tool, "SolanaAdapter", FakeAdapter)


def stub_cli(monkeypatch, stdout: str = PUBKEY, returncode: int = 0, missing: bool = False):
    monkeypatch.setattr(tool.shutil, "which", lambda name: None if missing else "/usr/local/bin/solana")
    monkeypatch.setattr(tool.subprocess, "run", lambda *a, **k: FakeCompleted(stdout, returncode))


# --------------------------------------------------- the public key, not the key


def test_the_public_key_comes_from_the_cli_and_the_file_is_never_opened(monkeypatch):
    """The boundary transfer_command() already draws, kept by the balance check.

    `solana address --keypair <path>` prints the PUBLIC key and nothing else. This
    process never opens the keypair file -- not to sign, and not to check a
    balance.
    """
    seen = {}

    def record(argv, **kwargs):
        seen["argv"] = argv
        return FakeCompleted(PUBKEY)

    monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/local/bin/solana")
    monkeypatch.setattr(tool.subprocess, "run", record)

    assert tool.sender_pubkey(KEYPAIR) == PUBKEY
    assert seen["argv"] == ["/usr/local/bin/solana", "address", "--keypair", KEYPAIR]
    assert "sign" not in " ".join(seen["argv"])


def test_the_binary_is_resolved_rather_than_a_bare_name(monkeypatch):
    """ruff's S607, fixed rather than suppressed (rule 19), and which() is the
    answer swap_terminal_desktop.py already uses for the browser binary."""
    monkeypatch.setattr(tool.shutil, "which", lambda name: "/opt/solana/bin/solana")
    monkeypatch.setattr(tool.subprocess, "run", lambda argv, **k: FakeCompleted(PUBKEY)
                        if argv[0] == "/opt/solana/bin/solana" else FakeCompleted("", 1))
    assert tool.sender_pubkey(KEYPAIR) == PUBKEY


def test_no_cli_installed_is_not_established_rather_than_empty(monkeypatch):
    stub_cli(monkeypatch, missing=True)
    assert tool.sender_pubkey(KEYPAIR) == ""


def test_a_failed_cli_call_is_not_established(monkeypatch):
    stub_cli(monkeypatch, stdout="Error: could not read keypair", returncode=1)
    assert tool.sender_pubkey(KEYPAIR) == ""


# ------------------------------------------------------------- the balance check


def test_a_short_balance_refuses_and_states_both_figures(monkeypatch):
    """The whole point: refuse HERE, not in the CLI after announcing the send."""
    stub_cli(monkeypatch)
    stub_cluster(monkeypatch, lamports=int(0.1 * LAMPORTS_PER_SOL))

    refusal = tool.sender_funding(KEYPAIR, AMOUNT).refusal

    assert refusal is not None
    assert PUBKEY in refusal.what
    assert "0.1 SOL" in refusal.what
    assert "0.25" in refusal.what, "the figure needed must be on screen beside what is held"
    assert "airdrop" in refusal.fix
    assert "Nothing was sent" in refusal.fix


def test_exactly_enough_including_the_fee_does_not_refuse(monkeypatch):
    """The boundary, asserted on both sides, because an off-by-one here either
    blocks a good send or lets a doomed one through."""
    needed = int(AMOUNT * LAMPORTS_PER_SOL) + tool.FEE_HEADROOM_LAMPORTS
    stub_cli(monkeypatch)

    stub_cluster(monkeypatch, lamports=needed)
    assert tool.sender_funding(KEYPAIR, AMOUNT).refusal is None

    stub_cluster(monkeypatch, lamports=needed - 1)
    assert tool.sender_funding(KEYPAIR, AMOUNT).refusal is not None, (
        "one lamport short of the fee headroom must still refuse"
    )


def test_the_amount_alone_without_fee_headroom_is_refused(monkeypatch):
    """A balance of EXACTLY the transfer amount cannot pay the signature.

    5000 lamports per signature, and this transaction carries one plus a memo. A
    check against the bare amount would pass a transfer that cannot be fee-paid.
    """
    stub_cli(monkeypatch)
    stub_cluster(monkeypatch, lamports=int(AMOUNT * LAMPORTS_PER_SOL))
    assert tool.sender_funding(KEYPAIR, AMOUNT).refusal is not None


def test_a_cluster_that_will_not_answer_does_NOT_refuse(monkeypatch):
    """"I could not check your balance" is not "you have no money".

    Refusing on an unanswerable cluster would block a send that would have worked,
    and the HTTP 429s this devnet account draws make that a live possibility rather
    than a hypothetical. Rule 17's line between a reason to believe and having
    checked -- and the send's own failure is then the honest answer.
    """
    stub_cli(monkeypatch)
    stub_cluster(monkeypatch, raises=True)
    assert tool.sender_funding(KEYPAIR, AMOUNT).refusal is None


def test_an_unknown_public_key_does_NOT_refuse(monkeypatch):
    """Same judgment one step earlier: no CLI means no check, not no funds."""
    stub_cli(monkeypatch, missing=True)
    assert tool.sender_funding(KEYPAIR, AMOUNT).refusal is None


def test_the_fee_headroom_is_well_above_a_signature_and_well_below_anything_real():
    """Stated as a property rather than a literal, so a retune has to stay sane.

    5000 lamports is one signature. 50_000 is ten times that and 0.00005 SOL --
    far above what the fee can become and far below anything worth saving.
    """
    one_signature = 5_000
    assert 2 * one_signature <= tool.FEE_HEADROOM_LAMPORTS
    assert int(0.001 * LAMPORTS_PER_SOL) > tool.FEE_HEADROOM_LAMPORTS


# --- the call site, not just the function --------------------------------------


def drive_main(monkeypatch, funding: tool.Refusal | None):
    """Run main() with every OTHER check stubbed to pass, in --dry-run.

    Only the funding refusal varies, so what this asserts is whether main()
    CONSULTS it -- which is a different question from whether the function works,
    and the one a mutation of the call site answers.
    """
    swap = {
        "id": "s_e820c23626002c37",
        "created_at": "2026-10-02T00:51:43.106818+00:00",
        "expires_at": "2026-10-02T01:01:43.106818+00:00",
        "deposit_tag": 10,
        "expected_input_amount": AMOUNT,
        "payout_address": GRC_PAYOUT,
    }

    class FakeSession:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setenv("SOL_RPC_URL", "https://api.devnet.solana.com")
    monkeypatch.setenv("SOL_DEPOSIT_ACCOUNT", PUBKEY)
    monkeypatch.setattr(tool, "db_session", lambda path: FakeSession())
    monkeypatch.setattr(tool, "open_sol_swap", lambda db, swap_id="": swap)
    monkeypatch.setattr(tool, "deposit_events_for", lambda db, swap_id: 0)
    monkeypatch.setattr(tool, "open_sol_swap_count", lambda db: 1)
    monkeypatch.setattr(tool, "ownership_refusal", lambda address: None)
    monkeypatch.setattr(tool, "sender_funding",
                        lambda keypair, amount: tool.SenderFunding(funding, "stubbed"))
    return tool.main(["--keypair", KEYPAIR, "--dry-run"])


def test_main_surfaces_a_funding_refusal_and_sends_nothing(monkeypatch, capsys):
    """MUTATION-FOUND. Deleting the call from main() changed nothing.

    Every test above calls sender_refusal() directly, so the two lines in main()
    that consult it were exercised by nothing -- a check written correctly and
    wired to nothing, which is indistinguishable from no check at all. This is the
    second call-site mutation to survive a first pass today; the other was
    describe_wallet_lock's can_unlock in swap_readiness.py.
    """
    refusal = tool.Refusal("the sending keypair holds 0.1 SOL and this transfer needs 0.25005 SOL",
                           "fund it with `solana airdrop 1 --url devnet`. Nothing was sent.")
    code = drive_main(monkeypatch, refusal)
    out = capsys.readouterr().out

    assert code == 1
    assert "REFUSED" in out
    assert "holds 0.1 SOL" in out
    assert "about to send" not in out, "a refused run must not print the transfer command"


def test_main_proceeds_when_funding_is_sufficient(monkeypatch, capsys):
    """The other half. A version that always refused would pass the test above."""
    code = drive_main(monkeypatch, None)
    out = capsys.readouterr().out

    assert code == 0
    assert "about to send" in out
    assert "--with-memo 10" in out, "the memo is the whole discriminator"
    assert "dry run" in out
    assert "REFUSED" not in out


# --- a check nobody can see ran is not a check ---------------------------------


def test_a_sufficient_balance_is_REPORTED_not_merely_allowed(monkeypatch):
    """MEASURED ON THE OPERATOR'S HOST 2026-10-02, two hours after I wrote the check.

    sender_refusal() returned None for "verified and sufficient" AND for "could not
    ask", so the dry run printed nothing in either case. Their block read

        payout owned    YES -- the GRC wallet holds the key, asked of the daemon just now
        about to send   solana transfer ... 0.25 ...

    with silence in between, and there was no way to tell a passed check from a
    skipped one -- in the file whose next statement hands money to the Solana CLI.
    Rule 14's "make did-nothing look different from did-work", shipped by me into
    the tooling this session has been removing it from all evening.
    """
    stub_cli(monkeypatch)
    stub_cluster(monkeypatch, lamports=int(1.0 * LAMPORTS_PER_SOL))
    funding = tool.sender_funding(KEYPAIR, AMOUNT)

    assert funding.refusal is None
    # "1 SOL", not "1.0": decimal_amount() normalizes, which is what also turns
    # the fee headroom from "5e-05" into "0.00005". A round balance printing
    # without a trailing ".0" is the same property.
    assert "1 SOL" in funding.line
    assert "0.00005 for the fee" in funding.line, (
        "the fee headroom printed as 5e-05 on the operator's host -- scientific notation in a money "
        "figure, on the screen read immediately before sending"
    )
    assert "e-0" not in funding.line, "no figure on this line may be in scientific notation"
    assert PUBKEY in funding.line
    assert "asked of the cluster just now" in funding.line, (
        "the line must say the figure was MEASURED, not assumed"
    )


def test_an_unaskable_balance_says_NOT_CHECKED_rather_than_nothing(monkeypatch):
    """Unknown and sufficient must not render the same way.

    Either lets the send proceed -- "I could not check your balance" is not "you
    have no money" -- and only one of them means the sender was verified.
    """
    stub_cli(monkeypatch, missing=True)
    no_cli = tool.sender_funding(KEYPAIR, AMOUNT)
    assert no_cli.refusal is None
    assert "NOT CHECKED" in no_cli.line
    assert "solana address --keypair" in no_cli.line

    stub_cli(monkeypatch)
    stub_cluster(monkeypatch, raises=True)
    no_cluster = tool.sender_funding(KEYPAIR, AMOUNT)
    assert no_cluster.refusal is None
    assert "NOT CHECKED" in no_cluster.line
    assert "RuntimeError" in no_cluster.line
    assert "NOT insufficient" in no_cluster.line, (
        "the distinction is the whole point: unknown must never read as empty"
    )

    assert no_cli.line != no_cluster.line, "the two reasons it could not ask must be distinguishable"


def test_an_insufficient_balance_carries_both_the_line_and_the_refusal(monkeypatch):
    """The line is not a substitute for the refusal, and vice versa."""
    stub_cli(monkeypatch)
    stub_cluster(monkeypatch, lamports=int(0.1 * LAMPORTS_PER_SOL))
    funding = tool.sender_funding(KEYPAIR, AMOUNT)

    assert funding.refusal is not None
    assert "NOT ENOUGH" in funding.line
    assert "0.1 SOL" in funding.line
    assert "see the refusal below" in funding.line, "the line must point at where the detail is"


def test_main_prints_the_funding_line_whatever_the_verdict(monkeypatch, capsys):
    """The call site, because a line returned and never printed is the same silence."""
    drive_main(monkeypatch, None)
    out = capsys.readouterr().out
    assert "sender funded   stubbed" in out, (
        "main() must print the line on the PASSING path -- that is the case that was silent"
    )


@pytest.mark.parametrize(
    ("lamports", "forbidden"),
    [
        (100, "1e-07"),                 # 0.0000001 SOL -- one of nine decimals
        (50_000, "5e-05"),              # the fee headroom, the figure that showed it
        (1_000_000_000, "1.0 SOL"),     # a round amount must not carry a trailing .0
    ],
)
def test_no_money_figure_on_the_funding_line_is_in_scientific_notation(monkeypatch, lamports, forbidden):
    """base_units_to_amount() returns a FLOAT, and a float's repr goes exponential
    at both ends. Every amount on this line goes through decimal_amount().

    Parametrized over the three shapes that reach it rather than the one that was
    observed: 5e-05 is what the operator saw, and 1e-07 is an ordinary quantity on
    a nine-decimal chain, so fixing only the observed one would have left the
    hazard in place for a different value.
    """
    stub_cli(monkeypatch)
    stub_cluster(monkeypatch, lamports=lamports)
    line = tool.sender_funding(KEYPAIR, AMOUNT).line

    assert forbidden not in line, f"{forbidden!r} reached a line somebody reads before sending"
    assert "e-0" not in line
    assert "e+" not in line


# --- the ownership check inverted when the adapter moved to the desk -----------

class _Ownership:
    def __init__(self, verdict, why=""):
        self.verdict = verdict
        self.why = why


def _stub_adapter(monkeypatch, verdict, why=""):
    """Replace the Gridcoin adapter with one that answers a seeded verdict."""
    monkeypatch.setattr(tool, "missing_settings", lambda rpc, asset: [])

    class _Adapter:
        def __init__(self, **_kwargs):
            pass

        def address_ownership(self, _address):
            return _Ownership(verdict, why)

    monkeypatch.setattr(tool, "GridcoinAdapter", _Adapter)


def test_an_address_the_desk_OWNS_is_refused_rather_than_required(monkeypatch):
    """MUTATION: swap the True/False branches back, which is what they were.

    Until 2026-10-05 this function refused an address the GRC wallet did NOT own,
    on the premise that the terminal's GRC daemon was the operator's own wallet --
    true while GRC_RPC_PORT was 25715. It became false when the port moved to the
    desk daemon on 25779, and the two guards then disagreed about one address:

        swap s_7170571c428b7912, SOL -> GRC, paying a customer address
        pay_test_deposit:         refused -- "the GRC wallet holds NO KEY"
        services/payout_service:  requires exactly that, or the desk pays itself

    The desk owning the payout address is the failure now: the coins never leave,
    and the swap is marked completed anyway.
    """
    _stub_adapter(monkeypatch, verdict=True)
    refusal = tool.ownership_refusal(GRC_PAYOUT)
    assert refusal is not None, "an address the desk owns must be refused"
    assert "OWNS the payout address" in refusal.what
    assert "paying itself" in refusal.fix

    _stub_adapter(monkeypatch, verdict=False)
    assert tool.ownership_refusal(GRC_PAYOUT) is None, (
        "an address the desk does NOT own is what a real payout needs"
    )


def test_an_unanswerable_ownership_question_is_still_refused(monkeypatch):
    """None is not a no, and that half did not invert.

    On 2026-10-01 a None from a shell with no GRC_RPC_* was read as "this chain
    cannot answer ownership" when it was a connection refusal. Inverting the
    True/False branches must not quietly turn the unknown case into a pass.
    """
    _stub_adapter(monkeypatch, verdict=None, why="connection refused")
    refusal = tool.ownership_refusal(GRC_PAYOUT)
    assert refusal is not None
    assert "NOT ESTABLISHED" in refusal.what
    assert "connection refused" in refusal.what


def test_the_dropped_assurance_is_NAMED_rather_than_left_blank():
    """Rule 14: the line that used to claim something must not silently vanish.

    It read "payout owned YES -- the GRC wallet holds the key". That assurance is
    genuinely gone -- the wallet that should own a customer payout is a different
    daemon this shell has no credentials for -- so the replacement says it was not
    checked, and prints the command that would check it.
    """
    lines = tool.ownership_lines(GRC_PAYOUT)
    text = "\n".join(lines)
    assert "NOT CHECKED" in text
    assert "82.65 tGRC" in text, "the loss that justified the original check stays named"
    assert f"validateaddress {GRC_PAYOUT}" in text
    assert "YES" not in text, "it must not still claim the assurance it can no longer give"
