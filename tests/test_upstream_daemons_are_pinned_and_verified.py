"""Every upstream daemon tarball this repository installs is pinned, verified, and attributed.

Role: code hygiene (read-only)
Reads: docker/*.Dockerfile, docker/upstream_binaries.sha256, docker/licenses/**,
       .dockerignore, `git ls-files`, and swap_terminal/architecture.py
Writes: nothing
Can move funds: no. No build, no container, no network, no credential read.
Mainnet-safe: yes, and network-independent -- every assertion is a file read.

=============================================================================
WHY THIS EXISTS. The operator, 2026-11-21: "can you just integrate these
independent daemons that open source into our own closed source?"
=============================================================================

The measured answer is that MIT permits it outright -- the grant in each project's
own COPYING covers "use, copy, modify, merge, publish, distribute, sublicense,
and/or sell" with ONE condition, that the copyright and permission notices travel
with the binary -- so the cheapest way to satisfy the request is to copy NONE of
their code. What lives in this repository is a version string, a digest and a
license notice. The binaries arrive from the projects that built them.

That design is only worth anything if three things hold, and on 2026-11-21 none of
the three was checked by anything:

  1. THE BYTES ARE PROVEN. docker/harness.Dockerfile curled two tarballs over TLS
     and extracted them, with nothing in between. That is a weaker claim than the
     Docker Hub image its own header refuses as "an unauditable binary on a machine
     that holds keys" -- at least a registry digest is pinnable.
  2. THE NOTICES SHIP. Measured by extracting both release tarballs and listing
     every file: NEITHER contains a COPYING or a LICENSE. So the image installed
     MIT binaries with no notice at all, while docker/grc-desk.Dockerfile:90 goes
     out of its way to `COPY --from=build /src/COPYING` and explains at length why.
     Two images in one directory, opposite answers to one obligation.
  3. NOTHING GETS VENDORED BY ACCIDENT. The alternative designs -- a submodule, a
     committed source tree, committed binaries -- were rejected with numbers (see
     the module header of swap_terminal/architecture.py and .gitignore's own
     cargo/dfx block, which records that doing it once "cost 891 files and 198MB of
     build output committed to this repository"). A decision that lives only in a
     document is a decision the next person reverses by committing a tarball,
     because that is easier than adding a digest.

CLEAN GATES, NOT RATCHETS (rule 19). Every count asserted below is zero or derived.
There is no baseline file, nothing is tolerated by appearing anywhere, and
test_no_upstream_daemon_binary_or_source_tree_is_committed starts at zero --
measured 2026-11-21, 567 tracked files, zero whose first bytes are ELF or gzip
magic -- the way rule 18's spelling gate did. (architecture.py's comment beside
VENDORED_UPSTREAM_TREES records why that count read 561 until this change landed.)

=============================================================================
WHAT IS DERIVED AND WHAT IS NOT, said up front because rule 17 asks which you hold
=============================================================================

DERIVED from the Dockerfile and the manifest, so a change to either fails here:
the arch list, the version, the installed binary list, the download hosts, the
verify's position relative to the extraction, the manifest's coverage and shape,
and every license COPY.

NOT DERIVED, and NOT DERIVABLE OFFLINE: whether each project publishes its own
checksum manifest (a fact about someone else's website) and whether the shipped
daemon contains Berkeley DB (a fact about a binary this repository deliberately
does not hold). Both are dated measurements carried in architecture.py with the
command that produced them, and this file does not pretend to check them. What it
DOES check is that the recorded Berkeley DB finding has a written home that ships
into the image, because the thing that must not happen is shipping the two MIT
notices alone -- accurate about Bitcoin Core and Litecoin Core, and false about
what is inside the binaries.

PROVEN SINCE 2026-11-22, AND THE PARAGRAPH THIS REPLACES WAS WRONG ABOUT WHY. It
said "no image was built. There is no docker daemon in the environment this was
written in", citing `docker info` against the default socket. A daemon starts here
perfectly well with a short --exec-root -- the first attempt failed on "unix socket
path too long (> 104)", a sun_path limit, not a privilege -- so the blocker was the
route, not the capability.

What was then measured, by running this stage's own RUN script verbatim with real
downloads from bitcoincore.org and download.litecoin.org:

    pinned ok: bitcoin-28.1-x86_64-linux-gnu.tar.gz
    pinned ok: litecoin-0.21.4-x86_64-linux-gnu.tar.gz
    bitcoin-28.1-x86_64-linux-gnu.tar.gz: OK
    litecoin-0.21.4-x86_64-linux-gnu.tar.gz: OK
    bitcoind --version  -> Bitcoin Core version v28.1.0
    litecoind --version -> Litecoin Core version v0.21.4

STILL NOT PROVEN, narrowed to what it actually is: docker's own layer and COPY
mechanics for this stage. The `apt-get install ca-certificates curl` step cannot
run here because this session's egress is a CONNECT-only proxy -- apt over http://
gets 405, and apt over https:// cannot bootstrap in debian:bookworm-slim, which has
no ca-certificates until that step installs them. The coreutils caveat below is
also narrower than it was, because the build no longer depends on
`--ignore-missing`'s zero-match exit status at all.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
from prose import dockerfile_without_comments

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = REPOSITORY_ROOT / "docker"
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

import architecture  # noqa: E402

#: A manifest line, as `sha256sum` writes and reads one: 64 lowercase hex, two
#: spaces, a filename. Anchored at both ends so a 63- or 65-character digest fails
#: HERE rather than being skipped by coreutils with a warning the build ignores.
MANIFEST_LINE = re.compile(r"^([0-9a-f]{64})  (\S+)$")

#: `curl -fsSLO "<url>"`, which is the only download form either Dockerfile uses.
CURL_TARBALL = re.compile(r'curl\s+-[A-Za-z]*O[A-Za-z]*\s+"(https://[^"]+\.tar\.gz)"')

#: `install -m 0755 "<project>-${VER}/bin/<binary>" ... /usr/local/bin/`
INSTALLED_PATH = re.compile(r'"([A-Za-z0-9_.${}-]+/bin/[A-Za-z0-9_-]+)"')

#: The coverage guard's own grep, lifted out of the Dockerfile so the test below
#: executes THE BUILD'S expression rather than a second copy of it (rule 8).
COVERAGE_GUARD = re.compile(r'grep\s+-Eq\s+"([^"]+)"\s+(\S+)')


def dockerfiles() -> list[Path]:
    """Every Dockerfile in docker/, which is where they all live."""
    return sorted(DOCKER_DIR.glob("*.Dockerfile"))


def instructions(dockerfile: Path) -> str:
    """One Dockerfile's text with its comment lines blanked.

    PROSE IS STRIPPED AND IT IS LOAD-BEARING, not hygiene. harness.Dockerfile
    explains its own download ordering at length, so the words "sha256sum" and
    "tar" appear in comments ABOVE the RUN that does the work. A raw scan finds the
    explanation first and passes whatever the instructions actually do.

    dockerfile_without_comments() IS IMPORTED RATHER THAN COPIED (rule 8). It lives
    in tests/prose.py, whose header counts this as the TENTH time a gate in this
    repository matched its own explanation -- the first nine in Python and
    JavaScript, this one in a Dockerfile.
    """
    return dockerfile_without_comments(dockerfile.read_text())


def arg_value(dockerfile: Path, name: str) -> str:
    """The value of `ARG <name>=` in that Dockerfile, or "" if there is none."""
    match = re.search(rf"^ARG\s+{re.escape(name)}=(\S+)", instructions(dockerfile), re.MULTILINE)
    return match.group(1) if match else ""


def downloads(dockerfile: Path) -> list[dict[str, str]]:
    """Every .tar.gz this Dockerfile curls, resolved as far as the file allows.

    Returns one dict per download with the host, the filename TEMPLATE as written,
    the ARG that supplies the version, and the shell variable that supplies the
    arch. Nothing here is looked up in architecture.py -- that is the whole point:
    the table is checked against this, not the other way round.
    """
    found = []
    for url in CURL_TARBALL.findall(instructions(dockerfile)):
        host = url.split("//", 1)[1].split("/", 1)[0]
        template = url.rsplit("/", 1)[1]
        placeholders = re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", template)
        found.append({
            "host": host,
            "template": template,
            "version_arg": placeholders[0] if placeholders else "",
            "arch_var": placeholders[1] if len(placeholders) > 1 else "",
        })
    return found


def arch_tokens(dockerfile: Path, arch_var: str) -> list[str]:
    """Every value the `case "$arch"` block assigns to `arch_var`, in file order.

    DERIVED FROM THE CASE BLOCK AND NOT FROM A LIST HERE, so the Dockerfile growing
    a third arm is what this sees. Upstream really does publish more: bitcoin
    publishes arm-linux-gnueabihf, riscv64 and powerpc64 builds for 28.1 and
    litecoin publishes arm-linux-gnueabihf and riscv64 for 0.21.4 -- measured from
    their own checksum manifests on 2026-11-21 -- so a third arm is a plausible
    edit, not a hypothetical.
    """
    seen: list[str] = []
    for value in re.findall(rf"\b{re.escape(arch_var)}=([A-Za-z0-9_.-]+)", instructions(dockerfile)):
        if value not in seen:
            seen.append(value)
    return seen


def manifest_path() -> Path:
    """The one digest manifest, taken from architecture.py's own pointer."""
    recorded = {row.digest_manifest for row in architecture.UPSTREAM_BINARIES.values()}
    assert len(recorded) == 1, (
        f"architecture.UPSTREAM_BINARIES records {sorted(recorded)} as digest manifests. More "
        f"than one manifest is more than one home for the same kind of fact (rule 8); the "
        f"single-copy property this whole design rests on is the one thing that cannot be "
        f"split for convenience"
    )
    return REPOSITORY_ROOT / recorded.pop()


def manifest_lines() -> dict[str, str]:
    """{filename: digest} from the manifest, with comments and blank lines dropped.

    COMMENTS AND BLANK LINES ARE SKIPPED HERE BECAUSE coreutils SKIPS THEM THERE.
    Measured 2026-11-21 with GNU coreutils 9.4: a `#` line and an empty line are
    both accepted silently by `sha256sum -c`, with no "improperly formatted"
    warning and rc=0. That measurement is what lets the manifest carry its own
    at-length header in the file the tool reads, instead of in a second file beside
    it that nobody opens.
    """
    out: dict[str, str] = {}
    for raw in manifest_path().read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = MANIFEST_LINE.match(line)
        assert match, (
            f"{manifest_path().name} line {raw!r} is not `<64 lowercase hex><two spaces><name>`. "
            f"coreutils would SKIP it with a warning and still exit 0 if another line verified, "
            f"so a malformed line here disarms part of the build's check silently"
        )
        out[match.group(2)] = match.group(1)
    return out


def expected_tarball_names() -> dict[str, list[str]]:
    """{host: [every filename a build of that host's download could produce]}.

    Fully derived: the filename template comes from the curl line, the version from
    the ARG that template names, and the arches from the case block that assigns the
    variable that template names.
    """
    out: dict[str, list[str]] = {}
    for dockerfile in dockerfiles():
        for entry in downloads(dockerfile):
            version = arg_value(dockerfile, entry["version_arg"])
            if not version or not entry["arch_var"]:
                continue
            names = [
                entry["template"]
                .replace("${" + entry["version_arg"] + "}", version)
                .replace("${" + entry["arch_var"] + "}", arch)
                for arch in arch_tokens(dockerfile, entry["arch_var"])
            ]
            out.setdefault(entry["host"], []).extend(names)
    return out


def tracked_files() -> list[str]:
    """`git ls-files`, which is what the repository actually publishes."""
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPOSITORY_ROOT, capture_output=True, text=True, check=True,
    )
    return [name for name in result.stdout.split("\n") if name]


def binary_assets() -> list[str]:
    """Every prebuilt-binary project's asset key, sorted.

    PARAMETRIZED ON THE KEY AND NOT ON THE (key, row) PAIR, which is a gate
    requirement rather than a style choice. tests/param_ids.py records what happened
    on 2026-11-21 when a parametrize id was left for pytest to invent:
    `suite_baseline.py check` reported 19 DISAPPEARED tests in a tree where all 19
    passed, because pytest 9.2.0 shortens a long string param to `<argname><index>`
    and pytest 8 did not -- and requirements-dev.txt declares a range spanning the
    change. A NamedTuple param has no verbatim form at all, so its id component is
    always pytest's. "BTC" and "LTC" are three characters and are ours under every
    pytest in that range; the row is looked up inside the test.
    """
    return sorted(architecture.UPSTREAM_BINARIES)


# ---------------------------------------------------------------------------
# 1. THE VERIFY MUST RUN BEFORE THE EXTRACTION
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dockerfile", dockerfiles(), ids=lambda p: p.name)
def test_every_downloaded_tarball_is_verified_before_it_is_extracted(dockerfile):
    """A digest check placed after `tar -x` verifies an archive already installed.

    MUTATION CAUGHT: move the `sha256sum --ignore-missing -c` line below the first
    `tar -xzf`. That diff looks like a harmless reordering, the build stays green,
    and the check now runs against an archive whose contents are already on disk and
    already in /usr/local/bin -- it proves nothing about what shipped while looking
    exactly like a check. Deleting the verify line outright is also caught, and that
    is the state harness.Dockerfile was in until 2026-11-21: curl at :66, tar at
    :67, nothing in between.

    OFFSETS AND NOT LINE NUMBERS, because the whole download lives in ONE `RUN` with
    backslash continuations, so "which instruction" is not the question -- the
    question is which command the shell reaches first.

    A DOCKERFILE THAT DOWNLOADS NOTHING IS NOT EXEMPTED, IT IS IRRELEVANT, and the
    difference is checked rather than assumed: the test only runs its assertion when
    `downloads()` found a .tar.gz, and test_the_download_hosts_are_the_ones_recorded
    separately pins which Dockerfiles those are. grc-desk.Dockerfile DOES download
    one -- Berkeley DB 4.8.30 from download.oracle.com, with no digest anywhere --
    and is deliberately not fixed here; see
    test_the_gridcoin_build_records_its_own_weaker_provenance.
    """
    text = instructions(dockerfile)
    if not downloads(dockerfile):
        pytest.skip(f"{dockerfile.name} curls no .tar.gz, so there is no ordering to check")

    extractions = [m.start() for m in re.finditer(r"tar\s+-[a-zA-Z]*x[a-zA-Z]*f?\s", text)]
    if not extractions:
        pytest.skip(f"{dockerfile.name} downloads a tarball and never extracts it")
    first_extraction = min(extractions)

    verifies = [m.start() for m in re.finditer(r"sha256sum\s", text)]
    # THE grc-desk EXEMPTION WAS DELETED 2026-11-22, as its own message instructed.
    # It read: "grc-desk.Dockerfile now runs a checksum check. That is an improvement
    # and this branch is now wrong: delete it, set
    # architecture.UPSTREAM_SOURCE['GRC'].digest_verified = True, and let the ordering
    # assertion below cover this file too." That is exactly what happened: grc-desk now
    # verifies Berkeley DB's tarball against ARG BDB_SHA256 before extracting it, the
    # table says digest_verified=True, and this file is covered by the same ordering
    # assertion as the harness.
    #
    # WORTH NAMING because it is the opposite of a ratchet (rule 19): the exemption
    # carried the instruction for its own removal and FAILED THE DAY the gap closed,
    # rather than going quiet and letting the branch outlive the defect. A baseline
    # would have reported green and said nothing.

    assert verifies, (
        f"{dockerfile.name} downloads {[d['template'] for d in downloads(dockerfile)]} and never "
        f"runs sha256sum, so the image trusts whatever TLS served. docker/upstream_binaries.sha256 "
        f"is the manifest to check it against"
    )
    assert min(verifies) < first_extraction, (
        f"{dockerfile.name} extracts a downloaded tarball at offset {first_extraction} and its "
        f"earliest sha256sum is at {min(verifies)}. A verify that runs after the extraction "
        f"verifies an archive already unpacked and installed -- it proves nothing about what "
        f"shipped. Move every curl above every tar"
    )


def test_the_verify_covers_every_tarball_the_same_run_downloads():
    """One `sha256sum -c` of the shared manifest must sit between the last curl and the first tar.

    The ordering test above is satisfied by ONE early verify even if a second
    download arrives after it. MUTATION CAUGHT: add a third chain's curl below the
    sha256sum line. The ordering assertion still passes -- there is a verify before
    the first tar -- and the new tarball is extracted having been checked against
    nothing.
    """
    harness = DOCKER_DIR / "harness.Dockerfile"
    text = instructions(harness)
    curls = [m.start() for m in re.finditer(r"curl\s", text)]
    verify = [m.start() for m in re.finditer(r"sha256sum\s", text)]
    assert curls and verify, "harness.Dockerfile must both download and verify"
    assert max(curls) < min(verify), (
        f"harness.Dockerfile's last curl is at offset {max(curls)} and its first sha256sum at "
        f"{min(verify)}, so at least one download happens AFTER the verify and is checked "
        f"against nothing. Every curl goes above the sha256sum"
    )


# ---------------------------------------------------------------------------
# 2. THE MANIFEST
# ---------------------------------------------------------------------------


def test_the_manifest_pins_a_digest_for_every_arch_the_dockerfile_can_build():
    """Expand the Dockerfile's own case block and require a manifest line for each result.

    MUTATION CAUGHT: add a third arm to harness.Dockerfile's `case "$arch"` --
    upstream really does publish arm-linux-gnueabihf and riscv64 builds for both
    projects, measured from their checksum manifests on 2026-11-21 -- without adding
    those two digests. The build would then fail only on that architecture, on
    somebody else's machine, as coreutils' bare "no file was verified" in a build
    log. This fails on every machine at suite time with the filename named.
    """
    pinned = manifest_lines()
    missing = {
        host: [name for name in names if name not in pinned]
        for host, names in expected_tarball_names().items()
        if any(name not in pinned for name in names)
    }
    assert missing == {}, (
        f"{manifest_path().name} pins no digest for {missing}. Those filenames are derived from "
        f"the Dockerfiles' own ARG versions and `case \"$arch\"` arms, so a build on that "
        f"architecture downloads a tarball the manifest cannot verify and `sha256sum "
        f"--ignore-missing -c` exits 1 with 'no file was verified'"
    )


def test_the_manifest_pins_nothing_no_build_can_download():
    """The other direction: a stale line is a line nobody will ever notice is wrong.

    MUTATION CAUGHT: bump `ARG BITCOIN_VERSION` and ADD the new digests while leaving
    the old version's lines behind. The build passes -- `--ignore-missing` skips
    them -- and the manifest grows a permanent record of a version nothing installs,
    which is the state in which "is this digest right?" stops being answerable.
    """
    buildable = {name for names in expected_tarball_names().values() for name in names}
    orphans = sorted(set(manifest_lines()) - buildable)
    assert orphans == [], (
        f"{manifest_path().name} pins {orphans}, which no Dockerfile in docker/ can download at "
        f"its current ARG versions and arches. Remove the stale lines with the version bump that "
        f"stranded them"
    )


def test_the_manifest_is_well_formed_and_has_no_duplicate_or_short_digests():
    """Exactly 64 lowercase hex per line, and no filename or digest reused inconsistently.

    THIS IS THE ONE THAT CATCHES A TYPO, AND THE TYPO IS WORSE THAN IT LOOKS.
    MEASURED 2026-11-21, GNU coreutils 9.4: a 63-hex digest line makes `sha256sum
    -c` treat the whole line as improperly formatted and SKIP it -- printing
    "WARNING: 1 line is improperly formatted" and still EXITING 0, because the other
    project's line verified. So a single dropped character disarms half the build's
    check and the build goes green. The exact run:

        $ sha256sum --ignore-missing -c short.sha256
        litecoin-0.21.4-x86_64-linux-gnu.tar.gz: OK
        sha256sum: WARNING: 1 line is improperly formatted
        rc=0

    MUTATION CAUGHT, three ways: drop a hex character from any digest; give the
    aarch64 filename the x86_64 digest (a copy-paste that would otherwise fail only
    on arm64 builds, on somebody else's machine); or add a second line for a
    filename already pinned, which leaves two answers to one question and
    `sha256sum` honoring whichever it reads.
    """
    seen_names: dict[str, str] = {}
    seen_digests: dict[str, str] = {}
    for raw in manifest_path().read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = MANIFEST_LINE.match(line)
        assert match, f"malformed manifest line {raw!r}"
        digest, name = match.group(1), match.group(2)
        assert name not in seen_names or seen_names[name] == digest, (
            f"{name} is pinned twice with different digests ({seen_names.get(name)} and {digest})"
        )
        assert digest not in seen_digests or seen_digests[digest] == name, (
            f"digest {digest} is pinned for both {seen_digests[digest]} and {name} -- two "
            f"different artifacts cannot have one digest, so one of those lines was copied"
        )
        seen_names[name] = digest
        seen_digests[digest] = name
    assert len(seen_names) >= 2, (
        f"{manifest_path().name} pins {len(seen_names)} artifact(s). With fewer than two there is "
        f"nothing for the coverage tests above to measure"
    )


def test_a_manifest_that_covers_nothing_downloaded_fails_the_build():
    """The fail-closed property, PROVEN BY RUNNING THE REAL TOOL, not by reading the flag.

    `--ignore-missing` is what lets one manifest hold all four arch/project
    filenames while any single build downloads two. The whole design rests on what
    the tool does when NOTHING in the manifest matched, because that is the state a
    version bump without a new digest line produces. If it exited 0 there, the flag
    would be a hole and the build would install an unverified binary.

    SO IT IS MEASURED HERE RATHER THAN ASSERTED IN A COMMENT: a real file is hashed,
    a real manifest naming a DIFFERENT filename is written, and the real
    `sha256sum --ignore-missing -c` is run. CLAUDE.md: "Never accept 'the code
    contains a check for X' as evidence X is enforced."

    MUTATION CAUGHT: this is the test that would catch coreutils changing that
    behavior under us, or somebody replacing `--ignore-missing` with a flag that
    tolerates a total miss.

    NO LONGER LOAD-BEARING, and that is the point of the change above it.
    debian:bookworm-slim ships coreutils 9.1 and this runs on the host's 9.4, and
    9.1's exit status with zero files verified was never observed -- ftp.gnu.org is
    403 at this session's proxy and deb.debian.org does not resolve. Rather than
    leave a fail-closed claim resting on an unobserved exit status,
    harness.Dockerfile grew the `grep -Eq "^[0-9a-f]{64}  ${want}$"` coverage loop
    that refuses on every coreutils ever shipped. This test still earns its place:
    it keeps the belt-and-braces half honest, and would catch coreutils changing the
    behavior under anyone who does rely on it.
    """
    import tempfile  # noqa: PLC0415 -- used only by this test

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        payload = b"not a tarball, and it does not need to be: sha256sum hashes bytes\n"
        (work / "present.tar.gz").write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()

        covering = work / "covering.sha256"
        covering.write_text(f"# a comment line, which coreutils accepts silently\n\n{digest}  present.tar.gz\n")
        good = subprocess.run(
            ["sha256sum", "--ignore-missing", "-c", str(covering)],
            cwd=work, capture_output=True, text=True, check=False,
        )
        assert good.returncode == 0, (
            f"a manifest that DOES cover the downloaded file must pass, and it did not: "
            f"rc={good.returncode} {good.stdout!r} {good.stderr!r}"
        )

        stale = work / "stale.sha256"
        stale.write_text(f"{digest}  some-other-version.tar.gz\n")
        refused = subprocess.run(
            ["sha256sum", "--ignore-missing", "-c", str(stale)],
            cwd=work, capture_output=True, text=True, check=False,
        )
        assert refused.returncode != 0, (
            f"`sha256sum --ignore-missing -c` exited 0 against a manifest that verified NOTHING "
            f"({refused.stdout!r} {refused.stderr!r}). docker/harness.Dockerfile's fail-closed "
            f"claim depends on this being non-zero: without it, bumping a version without adding "
            f"a digest installs an unverified daemon binary and the build stays green"
        )
        assert "no file was verified" in refused.stderr, (
            f"expected coreutils' 'no file was verified' on stderr, got {refused.stderr!r}"
        )


def test_the_build_refuses_an_uncovered_version_without_relying_on_a_coreutils_flag():
    """The coverage guard must sit between the downloads and the extractions, and WORK.

    WHY THIS EXISTS SEPARATELY FROM THE `--ignore-missing` TEST ABOVE. That one
    proves what `sha256sum` does when nothing in the manifest matched, by running the
    real tool -- on whatever coreutils the suite happens to run on. The image runs
    debian:bookworm-slim's 9.1 and the suite here runs 9.4, and 9.1 could not be
    observed from this container: ftp.gnu.org returns 403 at the proxy and
    deb.debian.org does not resolve, so neither the source nor the .deb was
    readable. A fail-closed property that rests on an unobserved exit status is the
    thing harness.Dockerfile's own comment warns about, so the build grew a
    `grep`-based coverage check whose exit status is the same on every coreutils
    ever shipped, and that check is what this test measures.

    THE EXPRESSION UNDER TEST IS READ OUT OF THE DOCKERFILE, not written here. A
    regex copied into a test is rule 8's duplicate rule: it agrees on the day it is
    written and drifts silently afterwards, and the drift would leave this passing
    while the build no longer refuses anything. So the pattern comes from the
    instructions, `${want}` is substituted with each filename the build can actually
    download (derived, via expected_tarball_names()), and the real `grep` is run.

    MUTATION CAUGHT: delete the guard and the search fails. Loosen `{64}` to `+` and
    the short-digest case stops refusing. Move the guard below the first `tar` and
    the ordering assertion fails, because a coverage check that runs after the
    extraction is a check on an archive already unpacked.
    """
    import tempfile  # noqa: PLC0415 -- used only by this test

    text = instructions(DOCKER_DIR / "harness.Dockerfile")
    match = COVERAGE_GUARD.search(text)
    assert match, (
        "harness.Dockerfile has no `grep -Eq \"<pattern>\" <manifest>` coverage guard. Without "
        "it the refusal of a version the manifest does not cover rests entirely on "
        "`sha256sum --ignore-missing` exiting non-zero when nothing matched, which is "
        "unobserved on the coreutils the image actually ships"
    )
    pattern = match.group(1)
    assert "${want}" in pattern, (
        f"the guard's pattern {pattern!r} does not mention ${{want}}, so it cannot be checking "
        f"the filenames this build downloads"
    )

    curls = [m.start() for m in CURL_TARBALL.finditer(text)]
    tars = [m.start() for m in re.finditer(r"tar\s+-[a-zA-Z]*x", text)]
    assert curls and tars, "harness.Dockerfile no longer downloads and extracts tarballs"
    assert max(curls) < match.start() < min(tars), (
        f"the coverage guard is at offset {match.start()}; the last curl is at {max(curls)} and "
        f"the first tar at {min(tars)}. A guard above the downloads checks names nothing fetched "
        f"yet, and one below the extraction checks an archive already unpacked and installed"
    )

    real = manifest_path()
    hosts = {row.host for row in architecture.UPSTREAM_BINARIES.values()}
    names = [name for host, got in expected_tarball_names().items() if host in hosts for name in got]
    assert names, "no expected tarball filename could be derived, so this test would assert nothing"

    for name in names:
        probe = pattern.replace("${want}", name)
        covered = subprocess.run(["grep", "-Eq", probe, str(real)], check=False)
        assert covered.returncode == 0, (
            f"the build's own guard does not find {name} in {real.name}, so a build for that "
            f"arch would be REFUSED even though the manifest is supposed to cover it"
        )

    body = real.read_text()
    probe = pattern.replace("${want}", names[0])
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)

        renamed = work / "renamed.sha256"
        renamed.write_text(re.sub(r"  (\S+)$", r"  other-\1", body, flags=re.MULTILINE))
        miss = subprocess.run(["grep", "-Eq", probe, str(renamed)], check=False)
        assert miss.returncode != 0, (
            f"the guard matched {names[0]} in a manifest where every filename was renamed, so a "
            f"version bump without a new digest line would NOT be refused by the build"
        )

        shortened = work / "short.sha256"
        shortened.write_text(re.sub(r"^([0-9a-f]{63})[0-9a-f]  ", r"\1  ", body, flags=re.MULTILINE))
        typo = subprocess.run(["grep", "-Eq", probe, str(shortened)], check=False)
        assert typo.returncode != 0, (
            f"the guard accepted a 63-hex digest for {names[0]}. `sha256sum -c` SKIPS a short line "
            f"with a warning and still exits 0 if any other line verified, so the guard is the "
            f"only thing that turns that typo into a refusal"
        )


def test_the_manifest_reaches_every_build_context_that_verifies_with_it():
    """No .dockerignore line may exclude the manifest from a context that COPYs it.

    LITERAL LINES ONLY, DELIBERATELY. tests/test_docker_build_context.py's header
    argues at length against reimplementing docker's ignore-pattern matcher: "the
    moment the two disagree, this test passes while a build ships a key." The same
    discipline applies in reverse here, so this checks the exact spellings that
    would break the COPY rather than simulating the matcher.

    MUTATION CAUGHT: add `*.sha256` or `docker` to .dockerignore. The COPY then fails
    at build time on the operator's host rather than here, and the plausible fix
    under time pressure is to delete the COPY -- and the verify with it.
    """
    manifest = {row.digest_manifest for row in architecture.UPSTREAM_BINARIES.values()}.pop()
    forbidden = {manifest, str(Path(manifest).parent), "docker", "docker/", f"{manifest}*", "*.sha256"}
    lines = [
        line.strip()
        for line in (REPOSITORY_ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    collisions = sorted(set(lines) & forbidden)
    assert collisions == [], (
        f".dockerignore excludes {collisions} from every build context rooted at the repository, "
        f"so `COPY {manifest}` cannot resolve and the harness image fails to build. The notices "
        f"under docker/licenses/ go the same way -- which is why they are NOT under docs/, a "
        f"path .dockerignore does exclude"
    )


# ---------------------------------------------------------------------------
# 3. ATTRIBUTION, AND THE COPYLEFT FINDING THE MIT NOTICES DO NOT COVER
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("asset", binary_assets())
def test_each_upstream_project_ships_its_OWN_license_notice_in_the_image(asset):
    """A tracked, MIT-bearing notice per project, COPYed into the image, distinct from the other's.

    MUTATION CAUGHT, and it is the one every other check here would pass: point both
    COPY lines at one shared docker/licenses/COPYING, or copy Bitcoin Core's notice
    into litecoin-core/. The bodies ARE byte-for-byte the same MIT grant, so a
    reviewer skimming the diff sees a tidy-up. What it drops is
    "Copyright (c) 2011-2021 The Litecoin Core developers" plus two different date
    ranges -- holders the text being shipped explicitly requires be carried.

    THE NON-IDENTITY ASSERTION IS THE ONLY ONE THAT CATCHES IT, and that is the
    error docs/vendoring_upstream_daemon_code.md measured on Gridcoin: its COPYING
    carries the MIT body under NO title and opens with FIVE copyright lines, so a
    Bitcoin notice in its place drops four holders. "Copy the notice from the
    project the code came from, not from memory of what MIT looks like."
    """
    row = architecture.UPSTREAM_BINARIES[asset]
    source = REPOSITORY_ROOT / row.license_source
    assert row.license_source in tracked_files(), (
        f"{row.license_source} is not tracked, so a fresh clone cannot build the image: the COPY "
        f"would fail, and the notice is the one thing MIT actually requires"
    )
    text = source.read_text()
    assert "The above copyright notice and this permission notice shall be included" in text, (
        f"{row.license_source} does not contain MIT's permission sentence, which is the clause "
        f"being satisfied by shipping it"
    )
    assert re.search(r"^Copyright \(c\) ", text, re.MULTILINE), (
        f"{row.license_source} carries no `Copyright (c)` line. MIT requires 'the ABOVE copyright "
        f"notice' travel with the binary, and a permission grant with no holder names satisfies "
        f"nothing -- Litecoin's own src/univalue/COPYING is exactly that file"
    )

    copied = f"COPY {row.license_source} {row.license_in_image}"
    assert copied in instructions(DOCKER_DIR / Path(row.installed_by).name), (
        f"{row.installed_by} has no `{copied}`, so the image installs {row.project}'s binaries "
        f"with no notice. Measured 2026-11-21: the release tarballs contain NO COPYING, so this "
        f"COPY is the only way the notice gets in"
    )

    others = {
        hashlib.sha256((REPOSITORY_ROOT / other.license_source).read_bytes()).hexdigest(): name
        for name, other in architecture.UPSTREAM_BINARIES.items()
        if name != asset
    }
    mine = hashlib.sha256(source.read_bytes()).hexdigest()
    assert mine not in others, (
        f"{asset}'s notice ({row.license_source}) is byte-identical to {others[mine]}'s. The MIT "
        f"bodies are the same; the COPYRIGHT BLOCKS are not, and that difference is the entire "
        f"reason there are two files"
    )


def test_the_license_destinations_are_distinct_paths_per_project():
    """Two notices COPYed to one path means the second silently overwrites the first.

    MUTATION CAUGHT: give both rows `license_in_image =
    "/usr/share/licenses/core/COPYING"`. Each COPY line then exists, each source is
    tracked and MIT-bearing, the sources are not identical -- every other assertion
    here passes -- and the built image ships one project's notice under a path that
    claims to cover both.

    The per-project directory convention already exists: grc-desk.Dockerfile:90
    writes /usr/share/licenses/gridcoin/COPYING. This makes it a rule rather than a
    habit, across the binary rows AND the source row.
    """
    destinations = [row.license_in_image for row in architecture.UPSTREAM_BINARIES.values()]
    destinations += [row.license_in_image for row in architecture.UPSTREAM_SOURCE.values()]
    duplicates = sorted({d for d in destinations if destinations.count(d) > 1})
    assert duplicates == [], (
        f"{duplicates} is the license destination of more than one project. In an image, the last "
        f"COPY wins and the earlier notice is gone"
    )


@pytest.mark.parametrize("asset", binary_assets())
def test_a_bundled_copyleft_dependency_is_recorded_and_ships_with_the_binaries(asset):
    """The MIT notices must not be the whole story when a measured Sleepycat dependency is inside.

    THE NON-NEGOTIABLE THIS ANSWERS: "do not ship a notice that papers over it."

    MEASURED 2026-11-21 against both upstream release tarballs -- the artifacts
    harness.Dockerfile actually installs, not a build from source:

        strings -a <daemon> | grep -c '^Berkeley DB 4\\.8\\.30: (April  9, 2010)$'
          bitcoind 1   bitcoin-cli 0   litecoind 1   litecoin-cli 0

    That string is Berkeley DB's own DB_VERSION_STRING. `ldd` on either daemon names
    no libdb at all, because the release builds are static -- so a linkage audit done
    with ldd calls these clean and is wrong, which is how the finding gets missed.

    WHICH CORRECTS THE AUDIT THIS CHANGE WAS WRITTEN FROM. It concluded "BITCOIN
    CORE 28.1 -- CLEAN", having PROVEN by a build that `--without-bdb --with-sqlite`
    yields a wallet-capable bitcoind with no BDB (`/* #undef USE_BDB */`, zero bdb.o,
    zero libdb in ldd). True, and a fact about a build THIS IMAGE DOES NOT PERFORM:
    nothing here runs ./configure, so no flag excludes anything.

    THE EXCLUSION THAT IS AVAILABLE IS ASSERTED SEPARATELY, by
    test_the_image_installs_only_the_binaries_it_declares: four of the thirteen
    binaries in the two tarballs are installed, and the wallet tools left behind
    carry the identical BDB. Berkeley DB cannot be removed from a prebuilt daemon,
    so the remaining exclusion is not distributing it at all --
    test_no_upstream_daemon_binary_or_source_tree_is_committed.

    MUTATION CAUGHT: delete docker/licenses/berkeley-db/MEASURED_LINKAGE.md, or set
    `copyleft_note=""` while leaving `bundles_berkeley_db=True`, or point the note at
    one of the MIT COPYING files to make the gate green. Each leaves the image
    shipping two MIT notices as if MIT were the whole obligation.
    """
    row = architecture.UPSTREAM_BINARIES[asset]
    if not row.bundles_berkeley_db:
        assert row.copyleft_note == "", (
            f"{asset} records no bundled Berkeley DB and still names a copyleft note "
            f"({row.copyleft_note!r}). One of the two is stale"
        )
        return

    assert row.copyleft_note, (
        f"{asset}'s shipped daemon is recorded as containing Berkeley DB -- Sleepycat, the only "
        f"license in this image with a source-disclosure condition -- and no note records it. The "
        f"two MIT notices alone are accurate about {row.project} and false about what is in the "
        f"binary"
    )
    note = REPOSITORY_ROOT / row.copyleft_note
    # EXISTENCE BEFORE READ, AND THE REASON IS A MEASURED BAD MESSAGE. Deleting the
    # note while it is still in git's index -- which is what a plain `rm` leaves --
    # made this test die with `pathlib FileNotFoundError` instead of saying what was
    # gone. The gate caught the mutation either way; rule 14 is that an operator
    # reading the output has to be able to tell what happened without opening the
    # test, and a traceback into the standard library is not that.
    assert note.is_file(), (
        f"architecture.py records {asset}'s bundled Berkeley DB finding at {row.copyleft_note} and "
        f"no such file exists. That note is what keeps the two MIT notices from reading as the "
        f"whole obligation -- restore it, or if the finding is genuinely obsolete, change "
        f"bundles_berkeley_db with the measurement that changed it"
    )
    assert row.copyleft_note in tracked_files(), f"{row.copyleft_note} is not tracked"

    mit_sources = {other.license_source for other in architecture.UPSTREAM_BINARIES.values()}
    assert row.copyleft_note not in mit_sources, (
        f"{asset}'s copyleft note points at {row.copyleft_note}, which is an MIT notice. The "
        f"finding it is supposed to record is the one MIT does not cover"
    )

    text = note.read_text()
    for required in ("Berkeley DB", "Sleepycat", "complete source code"):
        assert required in text, (
            f"{row.copyleft_note} does not mention {required!r}. It is the file that keeps the MIT "
            f"notices from reading as the whole obligation, so it has to name the license and the "
            f"condition that differs"
        )

    harness = instructions(DOCKER_DIR / Path(row.installed_by).name)
    assert f"COPY {row.copyleft_note} " in harness, (
        f"{row.installed_by} does not COPY {row.copyleft_note} into the image. A finding that "
        f"only exists in the repository does not travel with the binary it is about"
    )


def test_the_image_installs_only_the_binaries_it_declares():
    """The exclusion that IS available on a prebuilt artifact, derived from the install lines.

    Berkeley DB cannot be configured out of a binary somebody else compiled. What can
    be left behind is every binary in those tarballs the harness does not need, and
    MEASURED 2026-11-21 that matters rather than being tidiness: `bitcoin-wallet`
    carries the identical BDB (DB_VERSION_STRING=1, 161 DbEnv strings) and so does
    `litecoin-wallet` (162), while `bitcoin-qt` and `litecoin-qt` would drag Qt's
    GPL/LGPL into an image with no GUI at all. Four of thirteen are installed.

    MUTATION CAUGHT: add `bitcoin-wallet` to the install line -- a one-word edit that
    looks like convenience and that nothing else here would notice -- or add
    `bitcoin-qt`, which is the serious one. Deleting `bitcoin-cli` from the install
    line is caught the other way: the harness drives both daemons through their CLIs.
    """
    harness = DOCKER_DIR / "harness.Dockerfile"
    text = instructions(harness)
    installed_by_prefix: dict[str, list[str]] = {}
    for path in INSTALLED_PATH.findall(text):
        prefix, binary = path.split("/bin/", 1)
        installed_by_prefix.setdefault(prefix, []).append(binary)

    for asset in binary_assets():
        row = architecture.UPSTREAM_BINARIES[asset]
        template = next(d["template"] for d in downloads(harness) if d["host"] == row.host)
        # "bitcoin-${BITCOIN_VERSION}-${btc_arch}.tar.gz" -> "bitcoin-${BITCOIN_VERSION}",
        # which is the directory prefix the install lines use. Derived from the curl
        # line rather than typed, so a renamed ARG moves both together.
        prefix = template.split("}-", 1)[0] + "}"
        assert prefix in installed_by_prefix, (
            f"{harness.name} downloads {template} and installs nothing out of {prefix}/bin/"
        )
        assert sorted(installed_by_prefix[prefix]) == sorted(row.binaries), (
            f"{harness.name} installs {sorted(installed_by_prefix[prefix])} from {prefix}/bin/ and "
            f"architecture.UPSTREAM_BINARIES[{asset!r}].binaries records "
            f"{sorted(row.binaries)}. Every extra binary is another copy of the same Berkeley DB "
            f"obligation, and a *-qt would add Qt's copyleft to an image with no GUI"
        )


# ---------------------------------------------------------------------------
# 4. NOTHING GETS VENDORED BY ACCIDENT
# ---------------------------------------------------------------------------


def test_no_upstream_daemon_binary_or_source_tree_is_committed():
    """A CLEAN GATE at zero: no tracked file is an executable or an archive.

    MEASURED 2026-11-21: 567 tracked files, ZERO whose first bytes are ELF or gzip
    magic. No baseline, nothing tolerated, and it starts at zero the way rule 18's
    spelling gate did (rule 19).

    WHY THIS IS THE TEST THAT MAKES THE DESIGN SURVIVE. "We decided not to vendor"
    is a sentence in a document, and the next person who hits a broken build finds
    committing the tarball easier than adding a digest. Nothing else here would
    notice: the manifest would still be well-formed, the notices would still ship,
    the verify would still run. This is the only assertion that fails.

    IT IS ALSO THE SLEEPYCAT EXCLUSION. Berkeley DB cannot be removed from a
    prebuilt daemon, and every obligation in Sleepycat clause 3 triggers on
    DISTRIBUTION. This repository distributes nothing, and that is what this holds.

    AND THE COST WAS ALREADY PAID ONCE. .gitignore's own cargo/dfx block records
    that committing build output "cost 891 files and 198MB of build output committed
    to this repository", and its tools/dleq_helper block says "a committed binary is
    a thing nobody can review and everybody trusts". A bitcoind is a strictly larger
    version of that trade: measured from the tarballs, bitcoind is 16,541,800 bytes
    and git cannot delta compress it, so every version bump is a second full copy in
    history forever -- in a repository whose history has already been rewritten once
    (rule 4's one exception) to purge a credential.

    MUTATION CAUGHT: commit bitcoin-28.1/bin/bitcoind (ELF), or the tarball itself
    (gzip), or a Core checkout under a vendor directory without declaring it.
    """
    offenders = []
    for name in tracked_files():
        path = REPOSITORY_ROOT / name
        if not path.is_file() or path.is_symlink():
            continue
        head = path.open("rb").read(4)
        if head[:4] == b"\x7fELF":
            offenders.append((name, "ELF executable"))
        elif head[:2] == b"\x1f\x8b":
            offenders.append((name, "gzip archive"))
    assert offenders == [], (
        f"tracked binaries: {offenders}. Nothing upstream builds belongs in this repository -- "
        f"the harness image downloads it, verified against docker/upstream_binaries.sha256"
    )

    assert architecture.VENDORED_UPSTREAM_TREES == (), (
        f"architecture.VENDORED_UPSTREAM_TREES is {architecture.VENDORED_UPSTREAM_TREES!r} and no "
        f"vendor directory has been declared or reviewed. If a tree really is being vendored, "
        f"docs/vendoring_upstream_daemon_code.md says what every vendor site must carry -- "
        f"project, path and COMMIT SHA, the upstream notice, every modification, and why it was "
        f"vendored rather than called"
    )
    assert architecture.UPSTREAM_BINARIES_ARE_REDISTRIBUTED is False, (
        "architecture.UPSTREAM_BINARIES_ARE_REDISTRIBUTED is True, which turns on every "
        "obligation in docker/licenses/berkeley-db/MEASURED_LINKAGE.md: Sleepycat clause 3 "
        "triggers on distribution, and satisfying it is an operator decision with counsel, not a "
        "flag flip"
    )


def test_the_download_hosts_are_the_ones_recorded():
    """Every .tar.gz download in docker/ is accounted for by exactly one architecture.py row.

    MUTATION CAUGHT, in both directions. Delete the LTC row and this fails naming
    download.litecoin.org. Add a fourth daemon's download to any Dockerfile without
    recording it and it fails the other way -- which is the direction that matters: a
    binary nobody wrote down is a binary nobody verified, and it is exactly how
    .dockerignore's own comment came to be "short by two" when icp-replica and
    grc-desk were added with `context: .` and nobody re-counted.
    """
    actual = {entry["host"] for f in dockerfiles() for entry in downloads(f)}
    from_source = {host for row in architecture.UPSTREAM_SOURCE.values() for host in row.fetches_from}
    recorded = {row.host for row in architecture.UPSTREAM_BINARIES.values()}
    assert actual - from_source == recorded, (
        f"docker/ downloads tarballs from {sorted(actual)}; "
        f"architecture.UPSTREAM_SOURCE accounts for {sorted(from_source)} and "
        f"architecture.UPSTREAM_BINARIES for {sorted(recorded)}. The difference is a download "
        f"nothing in this repository describes"
    )
