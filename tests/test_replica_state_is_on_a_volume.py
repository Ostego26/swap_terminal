"""Is the replica's ACTUAL state directory on a named volume?

Role: test / measurement (reads the compose file and the Dockerfile; starts no
      container and makes no network call)
Reads: docker-compose.icp.yml, docker/icp-replica.Dockerfile
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHY THIS FILE EXISTS, and it is two incidents rather than a precaution.

docker/icp-replica.Dockerfile sets DFX_CONFIG_ROOT=/state and said, for as long as
it existed, that this was "so a `dfx start` survives a rebuild". It is not.
DFX_CONFIG_ROOT moves dfx's CONFIG and does not move its DATA. Measured inside the
running container on the operator's host 2026-10-07:

    /state                    40K    .config only          <- the named volume
    /root/.local/share/dfx    180M   network/local/<hash>/state
    /repo/.dfx                2.3M   canister ids and wasm copies

So the volume persisted forty kilobytes of config while a hundred and eighty
megabytes of canister state sat on the container's writable layer. Both of these
are that one fact:

    8455339  a `docker compose down` destroyed the ICP ledger, with the desk's
             1000 LICP, deployed and funded across two days
    1373fa3  threshold_custody.public_key returned a DIFFERENT key across one
             ordinary container recreation -- 038b01b0... then 03508d61... -- so
             every address derived from it was throwaway

THIS IS A TEXTUAL TEST AND THAT IS A COMPROMISE I AM NAMING RATHER THAN HIDING.
The behavioral-verification principle says to assert on rows a real run produced,
and the real run here is `down` then `up` then comparing a public key -- which
needs a docker daemon, a replica, and about a minute, none of which a unit suite
has. What this can do is stop the SPECIFIC regression that cost two incidents: the
state path silently not being on a volume. The behavioral proof is 1373fa3's own
test and belongs to whoever next recreates that container.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

#: Where dfx actually keeps the replica, measured in the running container rather
#: than read from documentation. The <hash> below it is derived from the network
#: config, so the PARENT is what must be persisted -- mounting network/local would
#: orphan the state the moment the config changed.
DFX_DATA_DIR = "/root/.local/share/dfx"


def replica_service() -> dict:
    return yaml.safe_load((REPO / "docker-compose.icp.yml").read_text())["services"]["icp-replica"]


def test_the_directory_dfx_actually_writes_is_mounted_on_a_volume():
    """The one regression this file exists to stop.

    Asserted on the DESTINATION of the mount, not on the volume's name, because
    the name is ours to choose and the path is dfx's.
    """
    mounts = replica_service()["volumes"]
    destinations = [entry.split(":")[1] for entry in mounts if ":" in entry]
    assert DFX_DATA_DIR in destinations, (
        f"{DFX_DATA_DIR} is not mounted. That is where dfx keeps network/local/<hash>/state -- the "
        f"replica, the canisters and the ledger, measured at 180M on the operator's host while the "
        f"/state volume held 40K of config. Without a volume there, `docker compose down` or any "
        f"image change destroys the ledger (8455339) and the threshold key (1373fa3). Mounts are: "
        f"{mounts}"
    )


def test_it_is_a_named_volume_and_not_a_bind_into_the_checkout():
    """180M of replica state under the operator's checkout would be a different bug.

    `git status` would have to ignore it, and `git clean -xdf` -- which an operator
    runs to get back to a known tree -- would silently destroy the ledger.
    """
    mounts = replica_service()["volumes"]
    entry = next(m for m in mounts if m.split(":")[1] == DFX_DATA_DIR)
    source = entry.split(":")[0]
    assert not source.startswith("."), (
        f"{DFX_DATA_DIR} is bind-mounted from {source!r}, which puts the replica's state inside the "
        f"checkout. `git clean -xdf` would then destroy the ledger."
    )
    declared = yaml.safe_load((REPO / "docker-compose.icp.yml").read_text())["volumes"]
    assert source in declared, f"{source} is used as a volume but is not declared in `volumes:`"


def test_the_dockerfile_no_longer_claims_DFX_CONFIG_ROOT_persists_state():
    """The sentence that made two incidents look like surprises.

    It read "STATE OUTSIDE THE WORKING DIRECTORY, so a `dfx start` survives a
    rebuild". A future reader trusting it would diagnose the next lost ledger as a
    docker bug rather than as a missing mount -- which is what happened twice.
    Rule 16: a wrong comment is a bug, and this one was load-bearing.
    """
    dockerfile = (REPO / "docker" / "icp-replica.Dockerfile").read_text()
    assert "DFX_CONFIG_ROOT=/state" in dockerfile, "this test is about that line; it has moved"

    # A CLAIM AND A QUOTATION OF A RETRACTED CLAIM ARE NOT THE SAME STRING, and the
    # first version of this assertion could not tell them apart -- it forbade the
    # phrase anywhere, and then failed on the correction that QUOTES it. Rule 1 is
    # explicit that the superseded wording stays so the drift is visible, so the
    # quote has to be allowed and only a live claim refused.
    #
    # The test is therefore: every occurrence of the phrase is introduced as
    # something the file USED to say. An occurrence that is not is a claim.
    claim = "so a `dfx start` survives a rebuild"
    for line in dockerfile.splitlines():
        if claim not in line:
            continue
        assert 'It read "' in line or "used to" in line.lower(), (
            f"the Dockerfile asserts {claim!r} rather than quoting it as retracted, on the line:\n"
            f"  {line.strip()}\n"
            f"Measured false 2026-10-07: DFX_CONFIG_ROOT moves dfx's CONFIG (40K at /state) and not "
            f"its DATA (180M at {DFX_DATA_DIR})."
        )
    # And it must say where the data really is, so the next reader does not have to
    # re-measure it inside a container (rule 1: the reasoning survives next to the code).
    assert DFX_DATA_DIR in dockerfile, (
        f"the Dockerfile should name {DFX_DATA_DIR} as the path that actually holds the replica, "
        f"since that is the measurement the comment is now making"
    )


def test_the_identity_volume_is_still_there_and_still_separate():
    """/state keeps its own job: dfx's identity keys, outside the repository tree.

    Asserted so that fixing the data mount cannot be mistaken for replacing this
    one -- they persist different things and only one of them is key material.
    """
    destinations = [entry.split(":")[1] for entry in replica_service()["volumes"] if ":" in entry]
    assert "/state" in destinations, "the identity volume was removed along with the fix"
    assert destinations.count(DFX_DATA_DIR) == 1
    assert DFX_DATA_DIR != "/state"
