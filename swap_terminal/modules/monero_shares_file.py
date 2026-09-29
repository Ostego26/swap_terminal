"""The 2-of-2 shares fixture: one file format, one writer, one reader.

Role: submodule (serialization -- no network, no chain, no console)
Reads: a shares JSON written by this module
Writes: a shares JSON, mode 0600
Can move funds: no. It holds the scalars that CAN, which is why the warnings below are in
        the payload itself rather than only in a docstring.
Mainnet-safe: the format is, the CONTENTS are not -- see below. Every caller must refuse a
        non-test network before writing one, and both do.
Live-safe: yes -- pure serialization.

MOVED HERE 2026-09-29 BECAUSE A SECOND WRITER APPEARED. `monero_shared_key_verify.py` held
these two functions and was the only thing that spoke this format. Closing gap (e) of
docs/branch_coverage.md needs `regtest/adaptor_join.py` to write the SAME file -- so that the
Monero share RECOVERED from a Gridcoin scriptSig can be handed to the sweeper -- and two
writers of one format is rule 8's bug with a delay on it: they agree the day they are written,
and the day they stop agreeing is the day a fixture loads with the wrong scalars in it and
every later step measures something else.

THE SUM IS RECOMPUTED ON LOAD, NEVER TRUSTED. A fixture whose stored sum disagrees with its
stored shares would make every later step measure the wrong thing, and it is one line to check.

THESE ARE PRIVATE KEYS AND THEY GO IN THE CLEAR. Correct for a stagenet or regtest rehearsal --
the coins are worthless and the entire point is that a SECOND PROCESS can reconstruct the sum --
and a key-disclosure bug anywhere else. Written 0600 anyway, because a habit that only holds on
test networks is not a habit.
"""

from __future__ import annotations

import json
from pathlib import Path

from chains.monero_keys import shared_private_spend_key

#: Every scalar a fixture must carry. Named once so the writer and the reader cannot disagree
#: about what "complete" means -- which is the failure this module exists to prevent.
REQUIRED_SCALARS = ("spend_share_a", "spend_share_b", "view_share_a", "view_share_b",
                    "spend_summed", "view_summed")


class SharesFileError(Exception):
    """A fixture that cannot be trusted. Callers translate this into their own refusal type."""


def save_shares(path: Path, shares: dict, address: str, network: str) -> None:
    """Write the fixture so a later invocation can finish the experiment.

    THESE ARE PRIVATE KEYS AND THEY GO IN THE CLEAR. That is correct for a stagenet
    or regtest rehearsal -- the coins are worthless and the whole point is that a
    second process can reconstruct the sum -- and it is why step 1 refuses on
    mainnet. The file is written 0600 anyway, because a habit that only holds on test
    networks is not a habit.
    """
    payload = {
        "network": network,
        "shared_address": address,
        "public_spend": shares["public_spend"],
        "public_view": shares["public_view"],
        "WARNING": "PRIVATE KEY SHARES IN THE CLEAR. Test networks only. Delete when done.",
        **{key: hex(value) for key, value in shares.items() if isinstance(value, int)},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")
    path.chmod(0o600)


def load_shares(path: Path) -> dict:
    """Read a fixture back, and REFUSE one this script did not write cleanly."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SharesFileError(
            f"could not read the shares file {path}: {error}. Run without --sweep first; it "
            f"writes one"
        ) from error
    shares = {}
    for key in REQUIRED_SCALARS:
        if key not in payload:
            raise SharesFileError(f"the shares file {path} has no `{key}` -- it was not written by this script")
        shares[key] = int(str(payload[key]), 16)
    # Recompute the sums rather than trusting the file's own. A fixture whose stored
    # sum disagrees with its stored shares would make every later step measure the
    # wrong thing, and this is one line.
    for label, summed, a, b in (
        ("spend", "spend_summed", "spend_share_a", "spend_share_b"),
        ("view", "view_summed", "view_share_a", "view_share_b"),
    ):
        recomputed = shared_private_spend_key(shares[a], shares[b])
        if recomputed != shares[summed]:
            raise SharesFileError(
                f"the {label} sum stored in {path} does not equal the sum of its own shares. "
                f"The file is inconsistent and nothing computed from it would mean anything"
            )
    shares["public_spend"] = str(payload.get("public_spend", ""))
    shares["public_view"] = str(payload.get("public_view", ""))
    shares["shared_address"] = str(payload.get("shared_address", ""))
    return shares


