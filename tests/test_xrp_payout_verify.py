"""The arming decision in xrp_payout_verify.py, which is the only thing there that moves money.

Role: test (pure function; opens no socket, reads no key file)
Reads: xrp_payout_verify.py
Writes: nothing
Can move funds: no
Mainnet-safe: yes
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "swap_terminal"))

import requests
from chains.xrp_signing import (
    CONFIRM_XRP_SEND,
    XRPMainnetRefused,
    XRPReserveRefused,
    XRPSendNotArmed,
)

from xrp_payout_verify import TESTNET_URL, arming_arguments, report_preview_refusal

SEED = "s" * 31


def test_a_preview_withholds_the_token():
    """The default. Without --send nothing may be armed."""
    _seed, confirm = arming_arguments(False, SEED)

    assert confirm == ""


def test_a_preview_also_withholds_the_SEED_not_only_the_token():
    """Both, and this is the half worth writing a test for.

    Withholding only the arming token would still hand a live seed to a code path
    that one editing mistake later might use. The adapter needs both to send, so
    supplying one alone is never useful -- which makes withholding both free.
    """
    seed, _confirm = arming_arguments(False, SEED)

    assert seed == "", "a preview must not carry the seed at all"


def test_arming_passes_the_exact_token_and_the_seed():
    """The positive case, or the guard above would pass with a function that always refuses."""
    seed, confirm = arming_arguments(True, SEED)

    assert seed == SEED
    assert confirm == CONFIRM_XRP_SEND


def test_the_token_is_the_shared_constant_and_not_a_second_spelling():
    """Rule 8. A retyped token here would drift from chains/xrp_signing.py and,
    because the adapter matches it EXACTLY, would refuse every send with a
    message about arming -- sending the reader to look at the guard rather than
    at the typo in this file.
    """
    _seed, confirm = arming_arguments(True, SEED)

    assert confirm is CONFIRM_XRP_SEND, "must be the imported constant, not a copy of its text"


def test_the_endpoint_is_pinned_to_testnet():
    """No flag reaches mainnet from this file; getting there means editing it.

    The adapter's own network check is the real guarantee -- it reads the
    SERVER's network_id rather than trusting a URL -- but a script that signs
    should not also be one URL typo away from pointing somewhere else.
    """
    assert "altnet.rippletest.net" in TESTNET_URL
    assert "s1.ripple.com" not in TESTNET_URL


def test_the_expected_preview_refusal_exits_zero_and_says_every_guard_before_it_ran(capsys):
    """XRPSendNotArmed is the one outcome a preview is SUPPOSED to produce.

    A preview withholds the arming token, so reaching that guard proves everything
    ahead of it ran: the addresses decoded, the server answered, the network was not
    mainnet, the balance was read and the reserve fit. Exit 0, and the message says
    so rather than just "that was expected".
    """
    code = report_preview_refusal(XRPSendNotArmed("this XRP send was NOT armed"))

    printed = capsys.readouterr()
    assert code == 0
    assert "DEFAULT and is correct" in printed.out
    assert "every guard before it ran" in printed.out


def test_an_unreachable_endpoint_is_NOT_reported_as_the_expected_refusal(capsys):
    """THE DEFECT THIS FIXES, measured in this container 2026-10-02.

    Both preview paths in xrp_payout_verify.py printed, unconditionally:

        ConnectionError: ('Connection aborted.', ConnectionResetError(104, ...))

        That refusal is the DEFAULT and is correct: this was a preview.

    It was neither. The XRP testnet is unreachable from the container this was
    written in, so NO guard ran at all -- and the script called the outcome expected
    and exited 0. "Skipped" and "success" in the same output is the defect rule 13
    names, in the one script whose whole job is establishing whether the payout path
    works.

    MUTATION: make report_preview_refusal() return 0 unconditionally. This test fails
    on the exit code AND on the absence of the warning, and
    test_the_expected_preview_refusal... above still passes -- which is the pair that
    makes the branch load-bearing rather than decorative.
    """
    code = report_preview_refusal(requests.ConnectionError("Connection reset by peer"))

    printed = capsys.readouterr()
    assert code == 1, "an unreachable endpoint must not exit 0; nothing was established"
    assert "THIS IS NOT THE PREVIEW REFUSAL" in printed.out
    assert "DEFAULT and is correct" not in printed.out


def test_a_guard_that_fired_EARLIER_than_arming_is_also_not_the_expected_refusal(capsys):
    """A mainnet id and a reserve shortfall are real answers, and they are not THIS one.

    Both mean the preview stopped before the arming check, so it established less
    than a clean preview does -- and a mainnet refusal in particular is the single
    most important thing in this file's output not to render as "working as
    intended".
    """
    for error in (XRPMainnetRefused("reports network_id 0, which is MAINNET"),
                  XRPReserveRefused("short by 500000 drops")):
        code = report_preview_refusal(error)
        printed = capsys.readouterr()
        assert code == 1, f"{type(error).__name__} must not exit 0"
        assert "THIS IS NOT THE PREVIEW REFUSAL" in printed.out
