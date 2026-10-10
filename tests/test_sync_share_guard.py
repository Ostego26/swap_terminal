"""A cloud-sync client syncing the swap database is detected and named.

Role: tests (sync_share_guard's decision, and the operator output it feeds)
Reads: directories it creates under tmp_path
Writes: marker files under tmp_path only
Can move funds: no
Mainnet-safe: yes

WHY THERE IS A GUARD TO TEST. Operator, 2026-10-10: "we need to have my megashare
quit syncing the swap terminal db. it makes it sync ever 5 seconds."

Five seconds of churn is the visible third of it. The other two are in
sync_share_guard's own docstring and are the reason this is a check in code rather
than a sentence in a README: a sync client copies swap_terminal.db and
swap_terminal.db-wal at different instants, which is two halves of two different
databases; and it syncs BOTH WAYS, so a conflicted copy or a version restore writes
a STALE database over the authority and a completed swap reads as unpaid to the
payout worker -- which pays it again, on chain, final.

WHAT THESE TESTS CAN AND CANNOT ESTABLISH, said plainly because rule 17 asks which
one a reader is holding. They establish that the DECISION is right given a marker or
a name: put `.megaignore` above a database and the verdict names MEGA and the
directory. They do NOT establish that MEGA actually writes `.megaignore`, because no
MEGA, Dropbox or OneDrive install exists in the container this suite runs in. The
marker names come from those clients' documented behavior; the directory-name
heuristic is the backstop for a client whose marker this table has wrong, and
SHARE_CLEAR's own output line says what it did not prove.
"""

from __future__ import annotations

import pytest
from sync_share_guard import (
    MAX_ANCESTORS,
    SHARE_CLEAR,
    SHARE_MARKER,
    SHARE_NAME,
    SYNC_CLIENTS,
    share_lines,
    share_verdict,
)


def _db_dir(tmp_path, *parts):
    """A database directory nested under `parts`, created."""
    path = tmp_path.joinpath(*parts) if parts else tmp_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_there_is_something_to_check():
    """A table that emptied would make every test below pass by checking nothing."""
    assert len(SYNC_CLIENTS) >= 10, f"{len(SYNC_CLIENTS)} clients; there were 12 when this was written"
    with_markers = [name for name, (globs, _) in SYNC_CLIENTS.items() if globs]
    assert len(with_markers) >= 5, (
        f"only {with_markers} carry marker globs. A table that lost its markers would fall back to "
        f"the NAME heuristic everywhere, which is the weak check -- and nothing would fail."
    )


@pytest.mark.parametrize("marker", [".megaignore", ".debris"])
def test_a_mega_marker_above_the_database_is_evidence(tmp_path, marker):
    """The operator's actual case, and the one the name heuristic cannot catch.

    Their database is at ~/Documents/Python/swap_terminal/swap_terminal -- NOT ONE
    COMPONENT OF WHICH IS NAMED AFTER A SYNC CLIENT. If MEGA is syncing ~/Documents,
    the only thing that says so from the filesystem is a marker MEGA left there. So
    this is not the easy case being tested twice; it is the case that decided the
    module's shape (markers checked all the way up, before any name).
    """
    share_root = _db_dir(tmp_path, "Documents")
    (share_root / marker).write_text("")
    database = _db_dir(tmp_path, "Documents", "Python", "swap_terminal", "swap_terminal")

    found = share_verdict(database)
    assert found["verdict"] == SHARE_MARKER
    assert found["client"] == "MEGA"
    assert found["evidence"] == marker
    assert found["where"] == str(share_root), "the sync ROOT is what the operator has to move out of"


def test_a_marker_that_is_a_directory_counts(tmp_path):
    """Existence only -- nothing is opened, so a directory marker works.

    Dropbox's `.dropbox.cache` is a DIRECTORY. A check that did `is_file()` or tried
    to read the marker would miss it, and the module's header claim that it "opens no
    file" would be false. One assertion keeps both honest.
    """
    share_root = _db_dir(tmp_path, "work")
    (share_root / ".dropbox.cache").mkdir()
    found = share_verdict(_db_dir(tmp_path, "work", "db"))
    assert found["verdict"] == SHARE_MARKER
    assert found["client"] == "Dropbox"


def test_a_hashed_marker_name_is_matched_as_a_glob(tmp_path):
    """Nextcloud's per-folder database carries a hash, so a literal name cannot match.

    `.sync_<hash>.db` is why the table holds globs rather than names. A literal-name
    check would pass its own tests and never fire in the field, which is this
    repository's most expensive recurring defect in its purest form.
    """
    share_root = _db_dir(tmp_path, "cloud")
    (share_root / ".sync_3f9a2b1c4d5e.db").write_text("")
    found = share_verdict(_db_dir(tmp_path, "cloud", "db"))
    assert found["verdict"] == SHARE_MARKER
    assert found["client"] == "Nextcloud/ownCloud"
    assert found["evidence"] == ".sync_*.db"


def test_a_directory_named_for_a_client_is_a_suspicion_and_says_so(tmp_path):
    """SHARE_NAME is deliberately a weaker answer, and the output must not hide that.

    A directory called "Dropbox" usually is one and might not be. Collapsing it into
    the same word as a marker would teach the operator to discount both.
    """
    database = _db_dir(tmp_path, "OneDrive", "terminal")
    found = share_verdict(database)
    assert found["verdict"] == SHARE_NAME
    assert found["client"] == "OneDrive"
    assert found["where"] == str(tmp_path / "OneDrive")
    assert any("suspicion, not a finding" in line for line in share_lines(database))


def test_a_name_match_is_exact_and_never_a_substring(tmp_path):
    """A false positive teaches the operator to ignore the line, which costs more.

    `dropbox-exporter` and `megatron` are plausible directory names in a tree full of
    chain tooling. A substring check would flag both, and a line that cries wolf is
    worse than no line -- it spends the attention the real case needs.
    """
    for name in ("dropbox-exporter", "megatron", "Documents", "my-onedrive-notes"):
        database = _db_dir(tmp_path, name, "db")
        assert share_verdict(database)["verdict"] == SHARE_CLEAR, f"{name} wrongly read as a share"


def test_case_does_not_matter_for_a_name(tmp_path):
    """Filesystems and operators disagree about capitalization; the check must not."""
    assert share_verdict(_db_dir(tmp_path, "dropbox", "db"))["client"] == "Dropbox"
    assert share_verdict(_db_dir(tmp_path, "NEXTCLOUD", "db"))["client"] == "Nextcloud/ownCloud"


def test_evidence_higher_up_outranks_a_name_lower_down(tmp_path):
    """Markers are checked all the way up BEFORE any name, and this is why.

    `~/MEGA/work/Nextcloud-notes/db` is a real shape. Checking both per-directory
    would report Nextcloud -- a guess from a folder name -- while a MEGA marker sat
    two levels up. The operator would then go looking in the wrong client's settings.
    """
    mega_root = _db_dir(tmp_path, "shared")
    (mega_root / ".megaignore").write_text("")
    database = _db_dir(tmp_path, "shared", "Nextcloud", "db")

    found = share_verdict(database)
    assert found["verdict"] == SHARE_MARKER
    assert found["client"] == "MEGA"


def test_a_clear_path_still_prints_a_line_that_says_what_it_did_not_prove(tmp_path):
    """Rule 14: a blank gap is ambiguous between "no share" and "this check broke".

    And the clear case is the one most likely to be WRONG: a client syncing a
    plainly-named directory and leaving no marker reads as clear. Saying so in the
    line is the difference between a result and a reassurance.
    """
    lines = share_lines(_db_dir(tmp_path, "plain", "db"))
    assert lines, "share_lines() returned nothing; an empty result must still print"
    text = " ".join(lines)
    assert "no sync-client marker" in text
    assert "would not be seen" in text, "the clear line must not read as proof of absence"


def test_the_remedy_names_all_three_filenames_strongest_first(tmp_path):
    """Excluding only the .db is WORSE than excluding nothing, so the line must say so.

    A client told to skip swap_terminal.db but not its sidecars uploads a -wal with no
    database to apply it to -- and the operator believes they have handled it. The
    remedy is also ordered: moving the directory is a property, an exclusion rule is a
    setting somebody can forget.
    """
    share_root = _db_dir(tmp_path, "sync")
    (share_root / ".stfolder").mkdir()
    lines = share_lines(_db_dir(tmp_path, "sync", "db"))
    text = " ".join(lines)

    assert "Syncthing" in text
    for name in ("swap_terminal.db", "swap_terminal.db-wal", "swap_terminal.db-shm"):
        assert name in text, f"the exclusion remedy does not name {name}"
    assert text.index("move SWAP_DB_DIR") < text.index("if it must stay"), "remedy is out of order"
    assert "BOTH WAYS" in text, "the stale-write hazard is the one that pays a swap twice"


def test_an_unreadable_ancestor_does_not_take_down_the_banner(tmp_path, monkeypatch):
    """This feeds `swap_stack up`. An OSError here must not stop the stack starting.

    Walking above the database can reach a directory this process cannot list. The
    swallow is narrow -- OSError from one glob, continue to the next -- and it can
    only ever WEAKEN the verdict: an unreadable directory yields no marker, exactly as
    a clean one does, so it can never invent a share that is not there.
    """
    database = _db_dir(tmp_path, "locked", "db")

    real_glob = type(database).glob

    def exploding_glob(self, pattern):
        if self.name == "locked":
            raise PermissionError(13, "Permission denied")
        return real_glob(self, pattern)

    monkeypatch.setattr(type(database), "glob", exploding_glob)
    assert share_verdict(database)["verdict"] == SHARE_CLEAR
    assert share_lines(database), "the banner line must still be produced"


def test_the_walk_is_bounded(tmp_path):
    """A deep path must not make `up` crawl. MAX_ANCESTORS is the backstop."""
    deep = tmp_path.joinpath(*[f"d{i}" for i in range(MAX_ANCESTORS + 20)])
    deep.mkdir(parents=True)
    # Marker placed ABOVE the bound, so finding it would prove the bound is not held.
    (tmp_path / ".megaignore").write_text("")
    assert share_verdict(deep)["verdict"] == SHARE_CLEAR, (
        "a marker beyond MAX_ANCESTORS was found, so the walk is not bounded"
    )


def test_a_relative_path_is_made_absolute_rather_than_resolved(tmp_path, monkeypatch):
    """SWAP_DB_DIR can be relative -- `./swap_terminal` is the shipped default.

    absolute() AND NOT resolve(): a symlink into a share is still in the share, and
    resolving it would report the target's path, which is not the path compose was
    given and not the path the operator has to change.
    """
    share_root = _db_dir(tmp_path, "Dropbox")
    (share_root / ".dropbox").write_text("")
    _db_dir(tmp_path, "Dropbox", "project", "db")
    monkeypatch.chdir(tmp_path / "Dropbox" / "project")

    found = share_verdict("./db")
    assert found["verdict"] == SHARE_MARKER
    assert found["client"] == "Dropbox"
