"""Every boost `-dev` the build stage installs needs a runtime library in the final image.

Role: test (static, parses Dockerfiles; no docker, no network)
Reads: docker/*.Dockerfile
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

=========================================================================
THE BUG THIS WOULD HAVE CAUGHT, AND DID NOT EXIST TO
=========================================================================

Measured 2026-11-22 on the operator's host. docker/grc-desk.Dockerfile built clean -- all 16
steps, `Successfully tagged swap-terminal/grc-desk:local` -- and then the first container
from it died on startup, once per restart behind `restart: on-failure:5`:

    gridcoinresearchd: error while loading shared libraries:
    libboost_iostreams.so.1.74.0: cannot open shared object file

The build stage installed `libboost-iostreams-dev`, so the binary linked against it. The
runtime stage installed `libboost-system1.74.0`, `libboost-filesystem1.74.0` and
`libboost-thread1.74.0` -- and not iostreams. A link-time dependency with no run-time
counterpart: the image was correct and the container was dead.

NOTHING COULD HAVE CAUGHT IT EXCEPT STARTING A CONTAINER, which is why it is worth a static
test now. `docker build` succeeds -- the compile has the dev package. A `COPY` check passes
-- the binary is there. Only running it fails, and this repository's suite cannot run a
container: there is no docker daemon in the environment these files are written in, which is
the same gap that let the autotools breakage survive in this very file.

=========================================================================
WHY BOOST SPECIFICALLY, AND NOT EVERY -dev PACKAGE
=========================================================================

A general "every -dev needs a runtime twin" rule is false and would be a ratchet in
disguise. `libssl-dev` pairs with `libssl3`, not `libssl-dev`'s own name. `zlib1g-dev` pairs
with `zlib1g`, which is in the base image. `libboost-test-dev` is for a test build this image
does not make. So a broad rule would need a table of exceptions, and a table of exceptions
is where a guard goes to die.

Boost is the narrow case where the rule is mechanical and true: Debian ships
`libboost-<component>-dev` against a runtime `libboost-<component><version>`, one for one,
same component name. That is checkable without a table, so it is checked.

IT IS NOT A CLAIM ABOUT COMPLETENESS. Passing this does not mean the runtime stage has
every library the binary needs -- only that it has not dropped a boost one. The complete
answer requires `ldd` inside the built image, which needs a docker daemon. The honest
version of that is recorded in the Dockerfile beside the package list, together with the
fact that the ldd it cites was run on a different distribution.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOCKER = Path(__file__).resolve().parent.parent / "docker"

#: Boost components whose `-dev` needs no runtime library in the image, and WHY for each.
#: Two entries, two different reasons, and the reasons are what keep this from becoming the
#: table of exceptions the module docstring warns about -- an entry here has to be
#: justifiable from something other than "the test failed".
#:
#: `system` IS HEADER-ONLY IN MODERN BOOST, and that is measured rather than assumed: `ldd`
#: on a gridcoinresearchd built from this tree's pinned Gridcoin commit lists
#: boost_filesystem, boost_iostreams and boost_thread, and NOT boost_system. grc-desk
#: installs libboost-system1.74.0 anyway -- harmless, and it arrives transitively through
#: thread regardless -- so this entry exists so a future image that omits it does not fail
#: for a library nothing loads.
#:
#: `test` IS BUILD-ONLY AND ITS BUILD IS OFF. grc-desk passes -DENABLE_TESTS=OFF, so boost
#: test is compiled against by nothing. THE BETTER FIX IS TO DELETE libboost-test-dev FROM
#: THE BUILD STAGE, and it is deliberately NOT done here: that line is the apt layer the
#: expensive compile is cached behind, so touching it costs a full 155MB re-download and a
#: from-scratch rebuild of Gridcoin. The defect being fixed in the same commit -- a dead
#: container -- is in the RUNTIME stage, which leaves the build cache intact and makes the
#: operator's next build cheap. Named as owed work rather than left to be rediscovered
#: (rule 19: this is not a baseline, it is one line with a reason and an owner).
NO_RUNTIME_NEEDED = frozenset({"system", "test"})


def dockerfiles() -> list[Path]:
    found = sorted(DOCKER.glob("*.Dockerfile"))
    assert found, f"no Dockerfiles under {DOCKER}; this test would be vacuous"
    return found


def boost_components(text: str, suffix_pattern: str) -> set[str]:
    """Component names for every `libboost-<component><suffix>` token in `text`."""
    return set(re.findall(rf"libboost-([a-z0-9_]+?){suffix_pattern}", text))


@pytest.mark.parametrize("path", dockerfiles(), ids=lambda p: p.name)
def test_every_boost_dev_has_a_runtime_library_in_the_same_dockerfile(path: Path):
    """A boost component compiled against must be loadable at run time.

    Both halves are read out of ONE file on purpose. A multi-stage Dockerfile is where the
    build stage's `-dev` list and the runtime stage's library list can disagree, and it is
    the only place both are visible at once.
    """
    text = path.read_text()

    # `-dev` tokens, minus the version-suffixed ones: Debian has both
    # `libboost-thread-dev` and `libboost-thread1.74-dev`, and either means the same
    # component is being compiled against.
    dev = {component.rstrip("0123456789.") for component in boost_components(text, r"[0-9.]*-dev\b")}
    runtime = boost_components(text, r"[0-9]+\.[0-9]+\.[0-9]+\b")

    missing = sorted(component for component in dev - runtime if component not in NO_RUNTIME_NEEDED)
    assert not missing, (
        f"{path.name}: compiles against boost {missing} but installs no runtime "
        f"libboost-<component><version> for them. This is how a container that BUILDS "
        f"clean dies at startup with 'error while loading shared libraries: "
        f"libboost_<component>.so.<version>: cannot open shared object file' -- measured "
        f"2026-11-22 for iostreams. dev={sorted(dev)} runtime={sorted(runtime)}"
    )


def test_grc_desk_installs_the_three_boost_libraries_its_daemon_actually_loads():
    """The specific regression, pinned by name rather than only by the general rule.

    `ldd` on a gridcoinresearchd built from ARG GRIDCOIN_COMMIT names exactly three boost
    shared objects: filesystem, iostreams and thread. iostreams is the one that was absent
    and killed the container, so it is asserted by name -- a general rule that someone
    later narrows should still leave this failing.
    """
    text = (DOCKER / "grc-desk.Dockerfile").read_text()
    for component in ("filesystem", "iostreams", "thread"):
        assert re.search(rf"libboost-{component}[0-9]+\.[0-9]+\.[0-9]+\b", text), (
            f"grc-desk.Dockerfile's runtime stage installs no libboost-{component}<version>. "
            f"The daemon loads boost_{component} at startup; without it the container exits "
            f"with 'cannot open shared object file' before it opens a port."
        )


def test_the_dev_and_runtime_spellings_are_actually_told_apart():
    """Guard the parser, not the Dockerfile: the two patterns must not match each other.

    `libboost-thread1.74-dev` and `libboost-thread1.74.0` differ by one character class.
    If the runtime pattern matched a `-dev` token, every file would pass vacuously -- which
    is a worse outcome than a false failure, because nobody investigates a green test.
    """
    sample = "libboost-iostreams-dev libboost-thread1.74-dev libboost-filesystem1.74.0"
    assert boost_components(sample, r"[0-9]+\.[0-9]+\.[0-9]+\b") == {"filesystem"}
    dev = {c.rstrip("0123456789.") for c in boost_components(sample, r"[0-9.]*-dev\b")}
    assert dev == {"iostreams", "thread"}


def test_this_test_would_have_failed_before_the_fix():
    """The counterfactual, run against the pre-fix text rather than asserted in prose.

    Without this, "it would have caught the bug" is a claim nobody can check. The runtime
    list below is grc-desk.Dockerfile's exactly as it stood when the container died.
    """
    broken = (
        "RUN apt-get install -y libboost-iostreams-dev libboost-thread-dev\n"
        "RUN apt-get install -y libboost-system1.74.0 libboost-filesystem1.74.0 "
        "libboost-thread1.74.0\n"
    )
    dev = {c.rstrip("0123456789.") for c in boost_components(broken, r"[0-9.]*-dev\b")}
    runtime = boost_components(broken, r"[0-9]+\.[0-9]+\.[0-9]+\b")
    missing = sorted(c for c in dev - runtime if c not in NO_RUNTIME_NEEDED)

    assert missing == ["iostreams"], (
        f"the rule does not flag the real defect, so it is not the rule that would have "
        f"caught it: got {missing}"
    )
