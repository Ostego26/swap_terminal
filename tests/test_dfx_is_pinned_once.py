"""Do the two images that install dfx pin the SAME version?

Role: tests (read-only)
Reads: docker/icp-replica.Dockerfile and docker/web.Dockerfile
Writes: nothing
Can move funds: no. No build, no container, no network.
Mainnet-safe: yes

WHY THIS EXISTS. dfx is installed in two images as of 2026-10-07:

  docker/icp-replica.Dockerfile   runs the replica and the canisters
  docker/web.Dockerfile           added so the web container can reach the ledger
                                  without docker (see 03d09b2's second transport)

The second deliberately copies the first's install, version pin included, rather
than inventing a method -- two ways of installing dfx in one repository is two
versions the first time one of them is bumped, and "which dfx answered" is not a
thing to debug a candid error against.

But a copied pin IS rule 8's failure with a delay on it: both say 0.24.3 today and
nothing makes them agree tomorrow. Bumping the replica and not the web image would
leave two dfx versions talking to one ledger, with the mismatch surfacing as a
candid decode error in whichever one was not bumped -- a class of error this repo
has already paid for twice today in other forms.

A SHARED SOURCE WAS TRIED AND DOES NOT EXIST. Compose build args would still
default in two places; YAML anchors do not cross files. So the enforcement is this
gate, which is a CLEAN GATE and not a ratchet (rule 19): it holds a property that
is true now, with no baseline and nothing tolerated.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
_PIN = re.compile(r"^ARG\s+DFX_VERSION=(\S+)", re.MULTILINE)


def _pins() -> dict[str, str]:
    found = {}
    for dockerfile in sorted(DOCKER_DIR.glob("*.Dockerfile")):
        text = dockerfile.read_text()
        if "internetcomputer.org/install.sh" not in text:
            continue
        match = _PIN.search(text)
        assert match, (
            f"{dockerfile.name} installs dfx and carries no `ARG DFX_VERSION=` -- so what it "
            f"installs is whatever the installer served that day, which is the unpinned build "
            f"docker/icp-replica.Dockerfile's own header refuses"
        )
        found[dockerfile.name] = match.group(1)
    return found


def test_every_image_that_installs_dfx_pins_it():
    """And there is more than one, or this test is measuring nothing."""
    pins = _pins()
    assert len(pins) >= 2, (
        f"only {sorted(pins)} install dfx, so this gate has nothing to compare. If the second "
        f"image stopped installing it, delete this file rather than leaving a test that passes "
        f"by having no subject (rule 19: a gate that reaches zero gets deleted)"
    )


def test_the_pins_AGREE():
    """Two dfx versions against one ledger is a candid error in whichever was not bumped."""
    pins = _pins()
    versions = set(pins.values())
    assert len(versions) == 1, (
        f"dfx is pinned at different versions: {pins}. Both images talk to the same ledger, so "
        f"the mismatch surfaces as a candid decode error in whichever one was not bumped. Bump "
        f"them together."
    )
