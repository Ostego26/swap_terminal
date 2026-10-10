"""Is this database sitting inside a cloud-sync folder? One decision, one place.

Role: submodule (a decision, read-only)
Reads: directory NAMES and the presence of marker files in the ancestors of a given
       path. It opens no file and reads no file's contents.
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- no socket, no adapter, no credential

=============================================================================
WHY THIS EXISTS, IN THE OPERATOR'S OWN WORDS
=============================================================================

2026-10-10: "we need to have my megashare quit syncing the swap terminal db. it
makes it sync ever 5 seconds."

Five seconds is the symptom and it is the least of it. A cloud-sync client on a live
SQLite WAL database does three distinct things, and only the first is visible:

  CHURN            every write touches swap_terminal.db-wal, so a poll loop writing
                   once a cycle makes the client upload continuously. On a host that
                   has just rebooted it also rescans the whole share, which competes
                   for the same disk the three workers and the chain daemons are on.

  A CORRUPT COPY   the client copies swap_terminal.db and swap_terminal.db-wal at
                   DIFFERENT INSTANTS. A WAL database snapshotted mid-transaction is
                   not a database, it is two halves of two different ones. CLAUDE.md
                   rule 12 already names shutil.copy2 on a WAL database as the
                   wrong-API example and Connection.backup() as the right one; a sync
                   client is shutil.copy2 with a network behind it and no
                   coordination at all.

  A STALE WRITE    and THIS is the one that loses money. Sync is BIDIRECTIONAL. A
                   conflicted copy, a second machine, or a version restore writes an
                   OLDER database over the authority. Every swap that completed since
                   that version then reads as unpaid to workers/payout_worker.py,
                   which pays it again -- on chain, final, with no counterparty to
                   unwind it with. That is the 2026-10-01 two-database failure
                   (workers/common.database_census() has the hour it cost) with a
                   sync client as the second writer instead of a typo.

  AND A SECOND     MEGA, Dropbox and Nextcloud all resolve a conflict by KEEPING BOTH,
  DATABASE         under names like "swap_terminal (conflicted copy).db". db.connect_db()
                   refusing a missing file stops a typo minting a second database;
                   nothing in this tree can stop a sync client from doing it, which is
                   why the answer here is a path and not a code change.

=============================================================================
WHY IT RUNS ON THE HOST AND NOT IN THE CONTAINER
=============================================================================

Inside the web container the database is at /data/swap_terminal.db, and /data is the
far end of a bind mount. The markers that identify a share live on the HOST side of
that mount, usually several directories above it -- so the container is structurally
incapable of seeing them. This module is therefore called from swap_stack.py, which
is the host-side driver, against SWAP_DB_DIR as the host spells it.

=============================================================================
WHAT IS EVIDENCE AND WHAT IS A SUSPICION -- RULE 17, STATED IN THE RETURN VALUE
=============================================================================

I COULD NOT TEST THIS AGAINST A LIVE MEGA, DROPBOX OR ONEDRIVE INSTALL. This process
runs in a container with none of them in it. The marker filenames below come from
those clients' documented and observable behavior, not from a measurement taken here,
and the honest consequence is that the verdict distinguishes two different kinds of
answer rather than pretending to one:

  SHARE_MARKER   a client's OWN marker file was found in an ancestor directory. That
                 is evidence: the file is there because the client put it there.
  SHARE_NAME     a path component is named after a client. That is a SUSPICION. A
                 directory called "Dropbox" usually is one and might not be.
  SHARE_CLEAR    neither was found, which is not the same as proof of absence -- a
                 client configured to sync a plainly-named directory and leaving no
                 marker would read as clear. Said so in the line it prints.

The two are never collapsed into one word, because the operator's next action differs:
a marker is something to act on, a name is something to check.
"""

from __future__ import annotations

from pathlib import Path

#: A marker file was found. Evidence, not a guess.
SHARE_MARKER = "SHARE_MARKER"

#: A directory in the path is named after a sync client. A suspicion.
SHARE_NAME = "SHARE_NAME"

#: Neither. See the docstring for why this is not proof of absence.
SHARE_CLEAR = "SHARE_CLEAR"

#: client -> (marker globs, directory-name components)
#:
#: ONE TABLE, ONE PLACE (rule 11). The alternative shape -- a check per client spread
#: through swap_stack.py -- is how the three atomic_*_client.py files came to disagree
#: with each other, and a share this table does not know about is a line to add here
#: rather than a function to write.
#:
#: MARKERS ARE GLOBS BECAUSE SOME CARRY A HASH. Nextcloud's per-folder database is
#: `.sync_<hash>.db`, so the name cannot be matched literally.
#:
#: WHERE A CLIENT HAS NO MARKER I CAN STATE WITH CONFIDENCE, IT GETS NAMES ONLY. That
#: is a deliberately weaker check rather than a marker I guessed at: a wrong marker
#: name is a check that silently never fires, which is this repository's most expensive
#: recurring defect (a route asserted at a path that 404s, a constraint checked on an
#: error page). A missing marker still leaves the name heuristic and the operator.
SYNC_CLIENTS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "MEGA": ((".megaignore", ".debris"), ("MEGA", "MEGAsync", "MEGAshare", "MEGAsync Downloads")),
    "Dropbox": ((".dropbox", ".dropbox.cache"), ("Dropbox",)),
    "Syncthing": ((".stfolder", ".stignore"), ("Syncthing",)),
    "Nextcloud/ownCloud": (
        (".sync_*.db", "._sync_*.db", ".owncloudsync.log", ".nextcloudsync.log"),
        ("Nextcloud", "ownCloud"),
    ),
    "Google Drive": ((".tmp.driveupload", ".tmp.drivedownload"), ("Google Drive", "GoogleDrive", "My Drive")),
    "OneDrive": ((), ("OneDrive",)),
    "pCloud": ((), ("pCloudDrive", "pCloud Drive")),
    "iCloud": ((), ("iCloud Drive", "iCloudDrive")),
    "Seafile": ((), ("Seafile",)),
    "Resilio Sync": ((), ("Resilio Sync",)),
    "Box": ((), ("Box Sync",)),
    "Yandex.Disk": ((), ("Yandex.Disk",)),
}

#: How far up to walk. The loop stops at the filesystem root anyway; this is the
#: backstop against a pathological path and keeps the walk bounded and finite.
MAX_ANCESTORS = 40


def _ancestors(path: Path) -> list[Path]:
    """`path` and every directory above it, nearest first, bounded.

    NEAREST FIRST BECAUSE THE NEAREST MARKER IS THE MOST INFORMATIVE: a .megaignore in
    the database's own directory and one eight levels up are both true, and the first
    tells the operator where the sync root is.
    """
    resolved = Path(path).expanduser()
    # NOT .resolve(): a symlink into a share is still in the share, and resolving it
    # would report the target's path, which is not the path compose is given. absolute()
    # normalizes without following links.
    resolved = resolved if resolved.is_absolute() else resolved.absolute()
    chain = [resolved, *resolved.parents]
    return chain[:MAX_ANCESTORS]


def _marker_in(directory: Path, globs: tuple[str, ...]) -> str | None:
    """The first marker of `globs` present in `directory`, or None.

    EXISTENCE ONLY -- nothing is opened and nothing is read. A sync client's marker
    can be a directory (Dropbox's .dropbox.cache) or a file (.megaignore), so this
    asks only whether the name is there.

    OSError IS SWALLOWED DELIBERATELY AND NARROWLY (rule 12's legitimate broad catch,
    narrowed): walking above the database can reach a directory this process cannot
    list, and an unreadable ancestor must not take down the `up` whose banner this
    feeds. The caller can tell a failure from an answer because an unreadable
    directory yields None, exactly as a clean one does, and a None here can only ever
    WEAKEN the verdict -- it can never invent a share that is not there.
    """
    for pattern in globs:
        try:
            if any(directory.glob(pattern)):
                return pattern
        except OSError:
            continue
    return None


def share_verdict(db_dir) -> dict:
    """Which sync client, if any, appears to be syncing `db_dir`.

    Returns a dict with:
      verdict    SHARE_MARKER, SHARE_NAME or SHARE_CLEAR
      client     the client's name, or None
      evidence   the marker glob that matched, or the path component that did
      where      the ancestor directory the evidence was found in, or None

    A DICT AND NOT A BOOLEAN, because "is it shared" is the wrong question to answer
    with one bit: the operator needs to know WHICH client (so they can find its
    settings) and WHERE the sync root is (so they can move the database out of it).
    A bare True would send them looking.

    MARKERS ARE CHECKED BEFORE NAMES, ALL THE WAY UP, rather than both per directory.
    A path can be `~/MEGA/work/Nextcloud-notes/db` -- a name match in a lower
    directory would otherwise outrank hard evidence higher up, and evidence is what
    the operator should be shown first.
    """
    chain = _ancestors(db_dir)

    for directory in chain:
        for client, (globs, _names) in SYNC_CLIENTS.items():
            matched = _marker_in(directory, globs)
            if matched:
                return {
                    "verdict": SHARE_MARKER,
                    "client": client,
                    "evidence": matched,
                    "where": str(directory),
                }

    for directory in chain:
        for client, (_globs, names) in SYNC_CLIENTS.items():
            # EXACT COMPONENT MATCH, CASE-INSENSITIVE, never a substring. "Documents"
            # must not match on "Document" and a project called "dropbox-exporter"
            # must not read as a Dropbox share -- a false positive here teaches the
            # operator to ignore the line, which costs more than the check buys.
            if directory.name.casefold() in {n.casefold() for n in names}:
                return {
                    "verdict": SHARE_NAME,
                    "client": client,
                    "evidence": directory.name,
                    "where": str(directory),
                }

    return {"verdict": SHARE_CLEAR, "client": None, "evidence": None, "where": None}


def share_lines(db_dir) -> list[str]:
    """The verdict as operator output. Never empty (rule 14: `(none)` is a result).

    A BLANK GAP HERE WOULD BE AMBIGUOUS between "no share" and "this check broke",
    and the second is the state a silently-wrong marker name produces -- so the clear
    case prints a line too, and that line says what it did NOT prove.

    THE REMEDY IS IN THE OUTPUT AND IS ORDERED STRONGEST FIRST, because an exclusion
    rule is a filter somebody can forget and a path outside the share cannot be synced
    at all. The third line is the one most often got wrong: excluding only the .db and
    not its two sidecars is WORSE than excluding nothing, because the client then
    uploads a -wal with no database to apply it to.
    """
    found = share_verdict(db_dir)

    if found["verdict"] == SHARE_CLEAR:
        return [
            f"  db share        no sync-client marker or folder name above {db_dir}"
            "  <- checked markers and names only; a client syncing a plainly-named"
            " directory would not be seen",
        ]

    strength = (
        f"its own {found['evidence']} is in {found['where']}"
        if found["verdict"] == SHARE_MARKER
        else f"{found['where']} is NAMED for it -- a suspicion, not a finding; confirm in the client"
    )
    return [
        f"  db share        {found['client']} appears to be syncing this database: {strength}",
        "                  WAL copied mid-transaction is not a backup, and sync runs BOTH WAYS:"
        " a conflicted copy or a version restore writes a STALE database over the authority and"
        " a completed swap reads as unpaid -- which pays it twice, on chain, final",
        f"                  fix, strongest first: 1. move SWAP_DB_DIR out of {found['where']}"
        " -- a path outside the share cannot be synced, which is a property rather than a setting",
        "                  2. if it must stay, exclude ALL THREE of swap_terminal.db,"
        " swap_terminal.db-wal and swap_terminal.db-shm -- excluding only the first is worse"
        " than excluding none",
        "                  3. back it up with sqlite3 .backup to a file OUTSIDE this directory,"
        " and let the sync client have THAT",
    ]
