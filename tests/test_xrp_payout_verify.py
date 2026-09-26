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

from chains.xrp_signing import CONFIRM_XRP_SEND

from xrp_payout_verify import TESTNET_URL, arming_arguments

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
