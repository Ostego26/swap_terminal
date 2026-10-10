"""connect_db() refuses a path that names no database, and only init_db() may create one.

Role: tests (the cause half of the two-database failure of 2026-10-01)
Reads: nothing outside tmp_path; swap_terminal/db.py and
       docker/web_workers_entrypoint.py are imported, not executed against anything
       real
Writes: SQLite files under pytest's tmp_path only
Can move funds: no
Mainnet-safe: yes -- no socket is opened and no adapter is built

=============================================================================
WHAT THIS FILE IS THE TEST FOR
=============================================================================

On 2026-10-01 three workers were started from a shell whose SWAP_DB_PATH pointed at
repo_root/runtime/swap_terminal.db instead of repo_root/swap_terminal/swap_terminal.db.
`sqlite3.connect()` CREATES a missing file, and db.connect_db() was a bare
sqlite3.connect(), so the typo did not fail. It manufactured a second database, and
the workers polled it for an hour printing

    deposit_watcher cycle=57 IDLE  active_swaps=0 refreshed=0

while three swaps sat in awaiting_deposit in the file every root tool reads.

On 2026-10-10 the operator found that file still on disk, nine days later, holding one
failed SOL swap with a real deposit_events row and five audit rows:

    327,680 bytes   52 swaps   newest 2026-10-10T21:57   the authority
    118,784 bytes    1 swap    newest 2026-10-01T22:34   the orphan

Their question was "is that fucking bifurcated database fixed as well? or did you just
gloss over that huge BFD". absorb_db.py merges the orphan and is CLEANUP. Rule 19's own
test is "does it stop the symptom being reported, or does it stop the cause existing?
Only the second is a fix" -- and the cause is this function. THIS FILE IS THE TEST FOR
THE SECOND.

=============================================================================
WHY EACH ASSERTION BELOW IS HERE RATHER THAN BEING OBVIOUS
=============================================================================

Every one of them is a way the fix could be undone by someone acting reasonably:

  no file is left behind  "it raises" is not the property that matters. The property
                          is that NOTHING WAS CREATED. A version that opened the
                          connection and then raised would pass a test that only
                          looked at the exception, and would leave the third database
                          on disk anyway.
  FileNotFoundError       there are `except FileNotFoundError` handlers in this tree
                          that predate DatabaseNotFound, and a bare Exception subclass
                          would slip past all of them.
  keyword-only `create`   connect_db() took one positional string for its whole life.
                          If `create` were positional, any existing two-argument call
                          -- or a future one passing a timeout -- becomes a silent
                          create, which is the exact defect this change removes.
  the message names
  SWAP_DB_PATH            the operator reading it believes their path is right, so
                          "no such file" sends them looking for a missing database
                          when what is wrong is the path (rule 14).
  the entrypoint creates
  BEFORE it spawns        the refusal would otherwise have broken a fresh deployment:
                          docker/web_workers_entrypoint.main() starts the three
                          workers BEFORE it execs gunicorn, and gunicorn is the thing
                          that imports wsgi -> app -> create_app() -> init_db(). This
                          is the one assertion that is about the fix's blast radius
                          rather than the fix.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import db
import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _load_entrypoint():
    """Import docker/web_workers_entrypoint.py as a module.

    BY PATH, because docker/ is not a package and has no __init__.py -- it holds
    Dockerfiles and two entrypoint scripts, and making it importable as a package
    would be a layout change in service of a test.

    tests/test_supervisor.py and tests/test_stack_authority.py read this same file as
    TEXT. They are asserting on what it says; this is asserting on what it does, and
    the ordering assertion below cannot be made from source text without becoming the
    thing CLAUDE.md's "verify by behavior, never by reading the code" forbids.
    """
    path = REPOSITORY_ROOT / "docker" / "web_workers_entrypoint.py"
    spec = importlib.util.spec_from_file_location("web_workers_entrypoint_under_test", path)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec_module so that a dataclass or a pickle inside it could
    # resolve its own __module__; harmless here and the shape every loader-by-path
    # in this suite uses.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_missing_database_raises_and_leaves_nothing_behind(tmp_path):
    """The whole fix in one assertion: it refuses, AND no file appears.

    THE SECOND HALF IS THE ONE THAT MATTERS. A connect_db() that called
    sqlite3.connect() and then checked would raise exactly the same exception and
    still have put a 0-byte file on disk -- and a 0-byte file is enough: the next
    caller finds it existing, connects without complaint, and gets "no such table:
    swaps" or, worse, applies SCHEMA to it.
    """
    path = tmp_path / "typo" / "swap_terminal.db"
    path.parent.mkdir()

    with pytest.raises(db.DatabaseNotFound):
        db.connect_db(str(path))

    assert not path.exists(), f"connect_db() created {path} on the way to refusing"
    assert list(path.parent.iterdir()) == [], "something was written into the directory"


def test_the_refusal_is_a_FileNotFoundError(tmp_path):
    """So existing `except FileNotFoundError` handlers still catch it.

    Not decoration: this tree already catches FileNotFoundError in several root tools
    around keypair and config reads. A DatabaseNotFound that inherited only from
    Exception would turn a handled condition into a traceback in those callers, which
    is a regression introduced by a fix.
    """
    with pytest.raises(FileNotFoundError):
        db.connect_db(str(tmp_path / "absent.db"))

    assert issubclass(db.DatabaseNotFound, FileNotFoundError)


def test_the_message_names_the_variable_rather_than_just_the_file(tmp_path):
    """Rule 14: the reader is holding a path they believe is right.

    "No such file or directory" sends an operator to look for a missing database. The
    thing that is actually wrong is nearly always the path, so the message names
    SWAP_DB_PATH, the relative-path case and the unmounted-volume case -- the three
    ways this has actually happened.
    """
    with pytest.raises(db.DatabaseNotFound) as caught:
        db.connect_db(str(tmp_path / "absent.db"))

    message = str(caught.value)
    assert "SWAP_DB_PATH" in message
    assert "working directory" in message
    assert "volume" in message
    # And it says who is allowed to create one, so the reader does not go looking for
    # a flag to pass.
    assert "init_db" in message


def test_create_is_keyword_only_so_a_positional_argument_cannot_mean_create(tmp_path):
    """A positional `create` would make any two-argument call a silent create.

    connect_db(db_path) took one string for its whole life. The next person to add a
    parameter -- a timeout, a row factory, an isolation level -- would pass it second,
    and with a positional `create` that argument would be truthy and the refusal would
    be gone without a single line of this file changing.
    """
    path = tmp_path / "positional.db"
    with pytest.raises(TypeError):
        # A positional bool IS the thing under test, so it is written as one.
        db.connect_db(str(path), True)
    assert not path.exists()


def test_create_true_is_how_a_database_comes_into_existence(tmp_path):
    """The exception, and it is an exception rather than the default on purpose.

    A missing database has two meanings -- first boot, and a wrong path -- and they
    want opposite answers. Creating by default makes the first free and the second
    SILENT, and the second is the one that happens.
    """
    path = tmp_path / "first-boot.db"
    conn = db.connect_db(str(path), create=True)
    try:
        assert path.is_file()
    finally:
        conn.close()


def test_db_session_refuses_the_same_way_and_passes_create_through(tmp_path):
    """Both doors, because every worker and root tool goes through db_session().

    connect_db() alone would have left db_session() -- which is what
    workers/deposit_watcher.py, payout_worker.py and reconcile_worker.py actually
    call -- creating files, and those three workers are the processes that spent the
    hour on 2026-10-01.
    """
    missing = tmp_path / "session-absent.db"
    with pytest.raises(db.DatabaseNotFound), db.db_session(str(missing)) as _:
        pass  # pragma: no cover -- the raise happens before the body
    assert not missing.exists()

    asked = tmp_path / "session-created.db"
    with db.db_session(str(asked), create=True) as conn:
        conn.execute("CREATE TABLE marker (id INTEGER)")
    assert asked.is_file()


def test_init_db_creates_and_initializes_and_is_idempotent(tmp_path):
    """The one caller that may create, called with an explicit path.

    THE PATH ARGUMENT IS NOT A TEST CONVENIENCE. It is what
    docker/web_workers_entrypoint.prepare_database() uses, and the alternative was a
    second copy of "connect, executescript(SCHEMA), apply_migrations" inside the
    entrypoint -- two copies of one rule, which rule 8 says is a bug with a delay on
    it.

    Idempotence is asserted because the container reapplies the schema on every
    restart and a second call must not fail or duplicate anything.
    """
    path = tmp_path / "initialized.db"
    db.init_db(path)
    assert path.is_file()

    def swap_count():
        conn = db.connect_db(str(path))
        try:
            return conn.execute("SELECT COUNT(*) AS n FROM swaps").fetchone()["n"]
        finally:
            conn.close()

    assert swap_count() == 0, "a brand new database with a swaps table holds no swaps"

    db.init_db(path)
    assert swap_count() == 0, "the second init_db() changed the row count"


def test_the_container_creates_the_database_before_it_starts_a_single_worker(tmp_path, capsys, monkeypatch):
    """The blast-radius assertion, and it is behavioral rather than a source read.

    docker/web_workers_entrypoint.main() calls start_workers() BEFORE it execs
    gunicorn, and gunicorn is what imports wsgi -> app -> create_app() -> init_db().
    So on a fresh volume the workers reach the database first. Before 2026-10-10
    whichever of them got there first created the file and applied SCHEMA itself;
    with connect_db() refusing, all three would instead die at startup on a path that
    was CORRECT -- the volume mounted, the variable right, and the only thing missing
    a file the container itself owns.

    HOW THIS IS PROVEN WITHOUT RUNNING A CONTAINER: start_workers is replaced with a
    recorder that asks whether the database exists AT THE MOMENT IT IS CALLED, and
    subprocess.Popen is replaced with a raise so main() stops at the gunicorn line
    instead of spawning a server. What is asserted is the recorded observation, not
    the order of two lines in a file.
    """
    entrypoint = _load_entrypoint()
    path = tmp_path / "data" / "swap_terminal.db"
    path.parent.mkdir()

    monkeypatch.setattr(entrypoint.Config, "DB_PATH", str(path))
    monkeypatch.setattr(entrypoint, "RUN_DIR", tmp_path / "run")

    observed = {}

    def recording_start_workers():
        observed["database_existed"] = path.is_file()
        # And it must be USABLE, not merely present: a 0-byte file would satisfy
        # is_file() and fail every cycle with "no such table: swaps".
        conn = sqlite3.connect(path)
        try:
            observed["swaps_table"] = bool(
                conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='swaps'").fetchone()
            )
        finally:
            conn.close()
        return []

    class StoppedBeforeGunicorn(RuntimeError):
        """Raised in place of spawning a real server, to end main() at the right line."""

    def refusing_popen(*_args, **_kwargs):
        raise StoppedBeforeGunicorn

    monkeypatch.setattr(entrypoint, "start_workers", recording_start_workers)
    monkeypatch.setattr(entrypoint.subprocess, "Popen", refusing_popen)

    with pytest.raises(StoppedBeforeGunicorn):
        entrypoint.main()

    assert observed["database_existed"] is True, (
        "start_workers() ran before the database existed; on a fresh volume all three "
        "workers would raise DatabaseNotFound for a correct path"
    )
    assert observed["swaps_table"] is True, "the database existed but had no schema in it"

    # Rule 14: the two cases want different reactions from whoever reads the log, so
    # they must not share a line.
    banner = capsys.readouterr().out
    assert "CREATED, it did not exist at this path" in banner
    assert "SWAP_DB_PATH or the /data mount is not what you meant" in banner


def test_the_container_says_already_present_on_a_restart(tmp_path, capsys, monkeypatch):
    """Did-nothing must not look like did-work (rule 14), at the level of a database.

    CREATED on a restart means the volume is not the volume the operator thinks it
    is and yesterday's swaps are in another file -- which IS the 2026-10-01 failure,
    now on line one of the container log instead of invisible for an hour. So the
    restart case has to be visibly different, or the signal is worthless.
    """
    entrypoint = _load_entrypoint()
    path = tmp_path / "data" / "swap_terminal.db"
    path.parent.mkdir()
    db.init_db(path)

    monkeypatch.setattr(entrypoint.Config, "DB_PATH", str(path))
    capsys.readouterr()

    entrypoint.prepare_database()

    banner = capsys.readouterr().out
    assert "already present" in banner
    assert "CREATED" not in banner
