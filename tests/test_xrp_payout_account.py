"""The account the signing seed controls, which nothing in the tree could name.

Role: test (pure functions; derives from a seeded value, opens no socket)
Reads: xrp_payout_account.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes

XRP_DEPOSIT_ACCOUNT was the single remaining readiness FAIL on the operator's
host through 2026-10-03, and it is what rendered four XRP pairs UNAVAILABLE on
the customer page. They asked to have it set, and nothing here could say WHAT to
set it to: derive_and_check() VERIFIES a pairing, signing_seed_is_present()
returns a bool by design, and xrp_payout_verify.py and xrp_balances.py both take
the account as an input.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

from chains import xrp_testnet
from chains.xrp_address import is_valid_classic_address
from chains.xrp_signing import derive_and_check
from valid_addresses import XRP_CUSTOMER_PAYOUT

import xrp_payout_account
from xrp_payout_account import agreement_line, derived_account

# A seed that funds nothing and controls an account that has never existed on any
# ledger. There is NO EXPECTED-ADDRESS LITERAL here, and that is the second
# iteration of this fixture.
#
# The first version typed one, got it wrong (the assertion failed with rG31cLy...
# against an invented rLUEXYu...), and was then "fixed" by pasting the measured
# value back in -- which pushed the tree to 61 address literals against
# tests/test_address_literals_are_valid.py's ceiling of 60. That gate's own
# failure message says what to do: "Use tests/valid_addresses.py rather than
# writing one -- a derived address cannot be mistyped and says what it is for."
#
# valid_addresses.py DERIVES its addresses from phrases, which is why it is exempt
# and why it cannot help here: the derivation under test is seed -> address and no
# phrase produces it. So the assertion changed shape instead of the ceiling
# changing (rule 19: never raise a baseline to let your own change land). What is
# pinned now is AGREEMENT WITH chains/xrp_signing.derive_and_check(), the function
# the live payout path actually uses -- which is a stronger statement than any
# literal: a typo'd literal fails loudly, but a literal that is merely the WRONG
# derivation agrees with nothing.
SEED = "sEdTM1uX8pu2do5XvTnutH6HsouMaM2"

#: A different, derived account, for the disagreement case. Taken from the shared
#: fixture registry rather than written, for the reason above.
OTHER_ACCOUNT = XRP_CUSTOMER_PAYOUT


def test_the_derived_account_is_the_one_the_SIGNING_PATH_would_accept():
    """Agreement with derive_and_check(), which is what guards the live payout.

    That function REFUSES to sign when the seed does not control the announced
    account -- chains/xrp_signing.py's threat model: local signing lets us choose
    the account a transaction claims, so a seed paired with the wrong address
    signs a Payment debiting an account nobody announced. So the address this tool
    prints is only useful if it is the address that check will accept, and that is
    what is asserted rather than a literal.

    The seed is read from the environment by the process and used for one thing.
    It is never printed, never logged and never a command-line argument -- argv is
    world-readable through /proc and `ps`, which is why there is no --seed flag.
    """
    address, refusal = derived_account(SEED)

    assert refusal == ""
    assert is_valid_classic_address(address), "the printed value has to decode as a classic address"
    assert SEED not in address
    # The authority, called directly: it raises when the pairing is wrong.
    derive_and_check(SEED, address)


def test_the_derivation_is_the_same_answer_every_time():
    """A seed maps to ONE account, and an operator pastes this into a variable.

    Without this, a version returning a fresh random address per call would pass
    every other test in this file: each one would decode, and derive_and_check()
    is handed whatever came back.
    """
    first, _ = derived_account(SEED)
    second, _ = derived_account(SEED)

    assert first == second


def test_a_DIFFERENT_seed_derives_a_different_account():
    """Or the function could ignore its argument entirely.

    The second seed is as disposable as the first and controls nothing.
    """
    other_seed = "sEdSKaCy2JT7JaM7v95H9SxkhP9wS2r"
    mine, _ = derived_account(SEED)
    theirs, refusal = derived_account(other_seed)

    assert refusal == ""
    assert theirs != mine


def test_an_unreadable_seed_refuses_WITHOUT_quoting_the_value():
    """str(error) is deliberately excluded, and that is not fussiness.

    xrpl-py's own messages for a malformed seed have echoed the offending value,
    and this tool's output is printed, pasted into a chat, and kept. So the
    refusal names the exception TYPE and the variable, which is everything the
    operator can act on, and nothing they must then redact.
    """
    address, refusal = derived_account("not-a-seed-at-all")

    assert address == ""
    assert "XRP_PAYOUT_SECRET_SEED" in refusal, "name the variable, or there is nothing to fix"
    assert "not-a-seed-at-all" not in refusal, (
        "the rejected value must not appear in output that gets pasted -- a real one would be a live key"
    )


def test_an_unset_seed_says_so_rather_than_deriving_nothing_quietly():
    """(none) is a result (rule 14), and the remedy is a shell command."""
    address, refusal = derived_account("")

    assert address == ""
    assert "not set in this process" in refusal
    assert "Export it in this shell" in refusal


@pytest.mark.parametrize(
    ("configured", "fragment"),
    [
        ("", "(unset)"),
        ("SAME", "AGREES with the seed"),
        ("OTHER", "DISAGREES with the seed"),
    ],
)
def test_the_configured_variable_is_compared_against_the_derived_account(configured: str, fragment: str):
    """THE CASE THAT COSTS MONEY IS NOT "UNSET", IT IS "SET TO SOMETHING ELSE".

    chains/xrp_signing.derive_and_check()'s threat model: local signing lets US
    choose the account a transaction CLAIMS, so a seed paired with an address it
    does not control signs a Payment debiting an account nobody announced. The
    payout path refuses that -- at payout time, after the deposit is confirmed and
    irreversible, which is the ordering the funding gate was added for the same
    morning. Reporting it here costs a retry.
    """
    derived, _ = derived_account(SEED)
    resolved = {"SAME": derived, "OTHER": OTHER_ACCOUNT}.get(configured, configured)

    assert fragment in agreement_line(derived, resolved)


def test_the_disagreement_line_names_BOTH_accounts():
    """One of the two alone leaves the operator unable to tell which to keep."""
    derived, _ = derived_account(SEED)
    line = agreement_line(derived, OTHER_ACCOUNT)

    assert derived in line and OTHER_ACCOUNT in line


def test_one_account_serves_both_directions_and_the_lines_say_which():
    """The operator asked whether every chain needs a deposit AND a withdraw account.

    services/swap_service.payout_source_account() reads the SAME variable through
    the SAME table as deposit_account() -- TAG_ATTRIBUTION[asset][0] -- and its
    docstring gives the reason at length: a second variable is how the deposit side
    and the payout side come to point at different accounts, each file looking
    correct on its own. So this tool prints ONE value to set, and says that it
    serves both directions, rather than leaving the reader to wonder where the
    withdraw account went.
    """
    derived, _ = derived_account(SEED)
    line = agreement_line(derived, derived)

    assert "deposits IN" in line and "debited OUT" in line
    assert "one account, both directions" in line


def test_the_variable_name_is_not_spelled_twice():
    """Rule 8 at its smallest: the module names it once and reads it back."""
    assert xrp_payout_account.DEPOSIT_ACCOUNT_VARIABLE == "XRP_DEPOSIT_ACCOUNT"
    assert xrp_payout_account.DEPOSIT_ACCOUNT_VARIABLE in agreement_line("rAnything", "")


# --- the shape of a value that will not decode -------------------------------
#
# MEASURED ON THE OPERATOR'S HOST 2026-10-03: XRP_PAYOUT_SECRET_SEED was SET and
# xrpl-py raised ValueError. The refusal said only "could not read it as a seed
# (ValueError)", which left them to inspect a live key by eye to learn which way
# it was wrong.


@pytest.mark.parametrize(
    ("value", "fragment"),
    [
        ('"sEdTM1uX8pu2do5XvTnutH6HsouMaM2"', "QUOTE CHARACTERS"),
        ("a" * 64, "all hexadecimal"),
        ("notaseed", "does not start with 's'"),
        ("123456 234567 345678", "secret numbers"),
        ("sEdTM1uX8pu", "starts with 'sEd'"),
        ("snotarealsecp", "starts with 's'"),
    ],
)
def test_an_undecodable_seed_is_diagnosed_by_SHAPE_and_never_by_content(value, fragment):
    """Each shape points at a different fix, and none of them prints the value.

    A length and a prefix FAMILY do not narrow a 29-character base58 secret to
    anything usable, and they are the difference between "re-export it without the
    quotes" and "that is a hex master seed, not a base58 secret".
    """
    address, refusal = derived_account(value)

    assert address == ""
    assert fragment in refusal
    assert value not in refusal, "the rejected value must never appear in output that gets pasted"


def test_the_refusal_says_the_PAYOUT_would_fail_too():
    """The finding is live, not cosmetic, and the line has to say so.

    chains/xrp_signing.py:397 derives the signing wallet with the same
    Wallet.from_seed(secret). A value this tool cannot read cannot be read there
    either, so an XRP payout would fail at SIGNING -- after the customer's deposit
    was confirmed and irreversible. An operator who reads this as "the diagnostic
    tool is fussy" leaves that in place.
    """
    _address, refusal = derived_account("notaseed")

    assert "xrp_signing" in refusal
    assert "after a deposit was confirmed" in refusal


def test_whitespace_is_reported_but_not_blamed():
    """Because it decodes. Measured against the installed xrpl-py.

        "sEdTM1uX8pu2do5XvTnutH6HsouMaM2 "   trailing space  -> ACCEPTED

    So a value that BOTH fails and has whitespace failed for another reason, and a
    refusal blaming the whitespace would send the operator to re-export a seed that
    was never the problem. This is the same "a reason to believe is not a
    measurement" line rule 17 draws, inside one diagnostic sentence.
    """
    address, refusal = derived_account(SEED + " ")

    assert address != "", "a trailing space decodes, so this must still derive an account"
    assert refusal == ""
    assert "not the cause" in xrp_payout_account.seed_shape("  " + SEED)


# --- resolving it, not just diagnosing it ------------------------------------
#
# The operator's reply to the refusal was "don't know, help me resolve this",
# which is the correct response to a message that says a value is bad and nothing
# about where a good one lives.


def _keyfile(directory, name, address, secret):
    """Write a faucet-shaped keyfile. The shape is chains/xrp_testnet's, measured."""
    (directory / name).write_text(json.dumps({"account": {"address": address, "secret": secret}}))


@pytest.fixture
def key_directory(tmp_path, monkeypatch):
    """Point chains/xrp_testnet at a temp directory. No real key is ever read."""
    monkeypatch.setattr(xrp_testnet, "KEY_DIRECTORY", tmp_path)
    return tmp_path


def test_a_keyfile_whose_secret_controls_its_address_is_USABLE(key_directory):
    """Both halves checked, and the second is the one no file format guarantees.

    chains/xrp_testnet.saved_faucet_accounts()'s own comment says the address and
    the secret are read from separate key names over two nesting levels, so nothing
    structurally ties them to the same faucet response. A seed paired with an
    address it does not control is what derive_and_check() refuses -- at signing,
    after a deposit.
    """
    derived, _ = derived_account(SEED)
    _keyfile(key_directory, "xrp-testnet-1.json", derived, SEED)

    verdicts = xrp_payout_account.saved_key_verdicts()

    assert len(verdicts) == 1
    assert "USABLE" in verdicts[0][2]
    assert "NOT USABLE" not in verdicts[0][2]


def test_a_keyfile_pairing_a_good_secret_with_the_WRONG_address_is_refused(key_directory):
    """The dangerous case, and it looks fine to any reader of the file.

    The secret decodes, the address decodes, both are well-formed -- and they are
    not each other's. Exporting that seed would arm the terminal against an account
    it cannot debit.
    """
    _keyfile(key_directory, "xrp-testnet-1.json", OTHER_ACCOUNT, SEED)

    verdict = xrp_payout_account.saved_key_verdicts()[0][2]

    assert "NOT USABLE" in verdict
    assert "controls" in verdict and "NOT the address" in verdict
    assert SEED not in verdict, "a verdict about a secret must not contain it"


def test_a_keyfile_whose_secret_does_not_decode_is_refused(key_directory):
    """And the verdict says which of the two failures it is."""
    _keyfile(key_directory, "xrp-testnet-1.json", OTHER_ACCOUNT, "placeholdr")

    verdict = xrp_payout_account.saved_key_verdicts()[0][2]

    assert "NOT USABLE" in verdict
    assert "does not decode" in verdict


def test_the_export_mode_hands_back_only_a_USABLE_secret(key_directory):
    """And refuses rather than handing back a plausible one.

    Two files, the usable one written second so it is not merely "the first found":
    saved_faucet_accounts() sorts newest-name-first, so xrp-testnet-2 is seen
    before xrp-testnet-1.
    """
    derived, _ = derived_account(SEED)
    _keyfile(key_directory, "xrp-testnet-1.json", OTHER_ACCOUNT, "placeholdr")
    _keyfile(key_directory, "xrp-testnet-2.json", derived, SEED)

    secret, refusal = xrp_payout_account.seed_for_export()

    assert secret == SEED
    assert refusal == ""


def test_the_export_mode_refuses_when_nothing_on_disk_is_usable(key_directory):
    """Returning "" with a reason, never a secret it could not vouch for."""
    _keyfile(key_directory, "xrp-testnet-1.json", OTHER_ACCOUNT, SEED)

    secret, refusal = xrp_payout_account.seed_for_export()

    assert secret == ""
    assert "decodes AND controls" in refusal
    assert "Nothing was printed" in refusal


def test_the_export_mode_REFUSES_to_write_a_seed_to_a_terminal(key_directory, monkeypatch, capsys):
    """The guard that makes the whole mode safe, and it is the assertion.

    Printing a seed is what the rest of this tool exists to avoid, so the one path
    that does it refuses when stdout is a tty: inside `$(...)` stdout is a pipe and
    the value goes file -> environment, while run by hand it refuses and explains.
    The seed never reaches a scrollback buffer, a `script` log or a pasted block.

    THIS COULD NOT BE DEMONSTRATED BY RUNNING THE TOOL during development, because
    this environment's stdout is already a pipe -- the refusal did not fire and the
    output looked like the success path. That is precisely why it is a test with
    isatty forced rather than something checked by eye.
    """
    derived, _ = derived_account(SEED)
    _keyfile(key_directory, "xrp-testnet-1.json", derived, SEED)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)

    code = xrp_payout_account.print_seed_for_export()
    captured = capsys.readouterr()

    assert code == 2
    assert SEED not in captured.out, "nothing may reach stdout on the refusal path"
    assert "REFUSED" in captured.err
    assert "command substitution" in captured.err, "and it has to say how to use it correctly"


def test_the_export_mode_writes_the_secret_ALONE_to_a_pipe(key_directory, monkeypatch, capsys):
    """Whatever stdout holds becomes the variable, so a banner would corrupt it.

    No label, no newline, no trailing context -- and the refusal path above writes
    to STDERR for the same reason: a refusal printed to stdout would silently
    export its own error message as the seed.
    """
    derived, _ = derived_account(SEED)
    _keyfile(key_directory, "xrp-testnet-1.json", derived, SEED)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)

    code = xrp_payout_account.print_seed_for_export()
    captured = capsys.readouterr()

    assert code == 0
    assert captured.out == SEED, "exactly the seed, with nothing around it"


def test_the_saved_key_block_does_not_print_the_helpers_chatter(key_directory, capsys):
    """The same two files were named THREE times, measured on the operator's screen.

    chains/xrp_testnet.saved_faucet_accounts() prints a provenance line per file as
    a side effect of READING, so two callers produced two sets of them -- and
    because the print happens during the read, the first set landed ABOVE the
    "saved keys" label that introduces the block:

            xrp-testnet-...233739Z.json: address under 'address', secret under 'seed'
            xrp-testnet-...233156Z.json: address under 'address', secret under 'seed'
          saved keys      2 faucet keyfile(s) in ~/.config/swap_terminal/keys/
                          xrp-testnet-...233739Z.json  rnjG8n16...  USABLE
                          xrp-testnet-...233156Z.json  rBfM7je6...  USABLE
            xrp-testnet-...233739Z.json: address under 'address', secret under 'seed'
            xrp-testnet-...233156Z.json: address under 'address', secret under 'seed'

    Rule 14: pasted output has to be self-describing a day later, and nothing in
    that block says why the same files appear three times.

    The helper is NOT changed -- xrp_send_tagged.py prints those lines as its
    account listing -- so what is asserted is that THIS tool does not forward them.
    """
    derived, _ = derived_account(SEED)
    _keyfile(key_directory, "xrp-testnet-1.json", derived, SEED)

    accounts = xrp_payout_account.read_saved_keys()
    captured = capsys.readouterr()

    assert len(accounts) == 1
    assert "secret under" not in captured.out, "the helper's chatter must not reach this tool's report"
    assert "secret under" not in captured.err, "and must not be merely moved to stderr, where it still prints"


def test_the_keyfiles_are_read_ONCE_and_handed_to_both_consumers(key_directory, monkeypatch):
    """Two reads is how the duplicate chatter happened, so one read is the fix.

    Counting the calls rather than the lines: suppressing the output twice in two
    different places would satisfy the test above while still reading the directory
    twice, and the next caller added would print a third set.
    """
    derived, _ = derived_account(SEED)
    _keyfile(key_directory, "xrp-testnet-1.json", derived, SEED)
    reads = []
    real = xrp_testnet.saved_faucet_accounts
    monkeypatch.setattr(xrp_testnet, "saved_faucet_accounts",
                        lambda: (reads.append(1), real())[1])

    accounts = xrp_payout_account.read_saved_keys()
    xrp_payout_account.saved_key_verdicts(accounts)
    xrp_payout_account.seed_for_export(accounts)

    assert len(reads) == 1, f"the directory was read {len(reads)} times for one report"
