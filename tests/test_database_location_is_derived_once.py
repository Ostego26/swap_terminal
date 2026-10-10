"""SWAP_DB_DIR and SWAP_DB_PATH are two names for one fact, reconciled in one place.

Role: tests (config.database_path()'s precedence, and the provenance it reports)
Reads: dotenv files it writes under tmp_path. Never the repository's own .env.
Writes: nothing outside tmp_path
Can move funds: no
Mainnet-safe: yes

=============================================================================
WHAT THIS IS THE TEST FOR
=============================================================================

Measured on the operator's host 2026-10-10. They moved the swap database out of a
MEGA sync folder -- correctly, and with this repository's own tooling telling them
to:

    mv swap_terminal/swap_terminal.db ~/.local/share/swap_terminal/
    sed -i 's|^SWAP_DB_DIR=.*|SWAP_DB_DIR=/home/.../.local/share/swap_terminal|' .env
    docker compose up -d --force-recreate web

The CONTAINER followed, because docker compose reads .env and
docker-compose.web.yml sets SWAP_DB_PATH=/data/swap_terminal.db over the new mount.
Every HOST tool did not, because nothing on the host read .env and SWAP_DB_PATH was
unset in their shell -- so Config.DB_PATH fell back to BASE_DIR/swap_terminal.db,
a file that no longer existed. `swap_stack.py status` then reported the OLD path,
and its own share check dutifully announced that MEGA was syncing a database that
had already been moved.

Rule 11 in its exact stated form: one concept derived in two places, agreeing on the
day they were written and drifting the moment one moved. And rule 15's, one layer
down: if the authority's LOCATION is ambiguous, "one database is the authority" is
not a property anything enforces.

=============================================================================
THE PART THAT IS A SECURITY BOUNDARY AND NOT A CONVENIENCE
=============================================================================

.env holds GRIDCOIN_WALLET_PASSPHRASE and the RPC credentials. config.read_env_file()
reads TWO KEYS out of it, both of which name a filesystem location, and the allowlist
is the whole design: reading the rest would ARM every host process that imports Config
-- a reporting tool, a root diagnostic, anything -- with the ability to unlock a
wallet. That is an arming decision and it belongs to the operator (rule 16), not to an
import. test_a_secret_in_the_file_is_never_read is the gate on that, and it is the one
test in this file that must never be relaxed.
"""

from __future__ import annotations

import os

import config
import pytest
import workers.common as workers_common
from config import DB_FILENAME, ENV_FILE_KEYS, database_path, read_env_file
from workers.common import db_path_source


@pytest.fixture
def dotenv(tmp_path, monkeypatch):
    """Point config.ENV_FILE at a temp dotenv and clear SWAP_DB_PATH from the shell.

    BOTH HALVES MATTER. tests/conftest.py sets SWAP_DB_PATH for the whole session so
    that importing the app cannot touch a real database -- and the environment WINS
    over the file, so without clearing it every precedence case below would report the
    same answer and the file would never be consulted.

    monkeypatch.setattr ON THE MODULE, which works only because read_env_file()
    resolves ENV_FILE in its body rather than binding it as a default argument. The
    first version of that function used `path=ENV_FILE` in the signature, which
    evaluates once at definition time -- three of these cases silently read the
    repository's own .env instead, and reported a confident wrong provenance.
    """
    monkeypatch.delenv("SWAP_DB_PATH", raising=False)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / ".env")
    # BOTH MODULES, because workers.common imported the name rather than the module --
    # `from config import ENV_FILE` binds a copy, so patching config alone leaves
    # db_path_source() reading the repository's own .env and reporting a provenance
    # about a file this test never wrote.
    monkeypatch.setattr(workers_common, "ENV_FILE", tmp_path / ".env")

    def write(*lines: str):
        (tmp_path / ".env").write_text("\n".join(lines) + "\n")
        return tmp_path / ".env"

    return write


def test_the_process_environment_always_wins(dotenv, monkeypatch):
    """A file must never second-guess an export, and this is the strongest case.

    THE CONTAINER DEPENDS ON IT. docker-compose.web.yml sets
    SWAP_DB_PATH=/data/swap_terminal.db in the container's environment while the SAME
    .env sits in the build context's parent on the host. If the file won, the
    container would open a host path that does not exist inside it.

    tests/conftest.py depends on it too, and so does an operator overriding for one
    command.
    """
    dotenv("SWAP_DB_PATH=/from/the/file.db", "SWAP_DB_DIR=/from/the/dir")
    monkeypatch.setenv("SWAP_DB_PATH", "/from/the/shell.db")

    assert database_path() == "/from/the/shell.db"
    assert db_path_source() == "SWAP_DB_PATH", "the provenance must name the shell, not the file"


def test_the_file_is_consulted_when_the_shell_is_silent(dotenv):
    """The case the operator was actually in: .env edited, nothing exported."""
    dotenv("SWAP_DB_PATH=/from/the/file.db")

    assert database_path() == "/from/the/file.db"
    source = db_path_source()
    assert "SWAP_DB_PATH in" in source
    assert "NOT from this shell" in source, (
        "a provenance that said plain 'SWAP_DB_PATH' would send an operator to check `env`, "
        "find nothing, and conclude the report is lying"
    )


def test_SWAP_DB_DIR_is_the_bridge_between_the_two_names(dotenv):
    """THE FIX. MUTATION: delete the SWAP_DB_DIR branch of database_path().

    This is the line that makes the host agree with the container BY CONSTRUCTION
    rather than by the operator remembering to set two variables to the same place.
    SWAP_DB_DIR is the only one of the two that compose requires, so it is the one an
    operator actually edits -- and before this, editing it moved the container and left
    every host tool behind.
    """
    dotenv("SWAP_DB_DIR=/moved/here")

    assert database_path() == f"/moved/here/{DB_FILENAME}"
    source = db_path_source()
    assert "SWAP_DB_DIR" in source and DB_FILENAME in source
    assert "by construction rather than by remembering" in source


def test_SWAP_DB_PATH_in_the_file_outranks_SWAP_DB_DIR_in_the_file(dotenv):
    """Within the file, the more specific key wins.

    Otherwise an operator who set both -- which the template invites, since it
    documents both -- gets the directory's default filename and no way to see why the
    explicit path they wrote was ignored.
    """
    dotenv("SWAP_DB_DIR=/moved/here", "SWAP_DB_PATH=/explicitly/this.db")
    assert database_path() == "/explicitly/this.db"


def test_the_directory_is_joined_with_the_filename_and_never_used_as_one(dotenv):
    """SWAP_DB_DIR names a DIRECTORY, and that is not pedantry.

    docker-compose.web.yml refuses a SWAP_DB_DIR that names the file, because WAL
    keeps -wal and -shm sidecars beside the database and all three must be inside one
    mount. Joining DB_FILENAME here is the same rule applied on the host side, so the
    two sides resolve to the same file.
    """
    dotenv("SWAP_DB_DIR=/moved/here")
    resolved = database_path()
    assert resolved.endswith(f"/{DB_FILENAME}")
    assert resolved != "/moved/here", "the directory was used as the database file"


def test_a_quoted_value_loses_its_quotes(dotenv):
    """set_grc_passphrase.sh writes single-quoted values, and compose reads them literally.

    A path read with its quotes still attached is a path that does not exist, and the
    failure would land as DatabaseNotFound naming a path with a stray apostrophe in
    it -- which reads as a corrupted file rather than a parsing bug.
    """
    dotenv("SWAP_DB_DIR='/single/quoted'")
    assert database_path() == f"/single/quoted/{DB_FILENAME}"

    dotenv('SWAP_DB_DIR="/double/quoted"')
    assert database_path() == f"/double/quoted/{DB_FILENAME}"


def test_a_secret_in_the_file_is_never_read(dotenv):
    """THE ALLOWLIST IS A SECURITY BOUNDARY. MUTATION: widen ENV_FILE_KEYS.

    .env holds GRIDCOIN_WALLET_PASSPHRASE -- set_grc_passphrase.sh puts it there and
    chmods the file to 600. If config.read_env_file() returned it, every host process
    that imports Config would hold the passphrase that unlocks the GRC wallet: a
    reporting tool, a diagnostic, anything. Arming is the operator's decision (rule
    16) and never an import's.

    ASSERTED ON WHAT COMES BACK, not on the constant alone, because a future reader
    could keep the tuple narrow and add a second read beside it.
    """
    found = read_env_file(
        dotenv(
            "SWAP_DB_DIR=/moved/here",
            "GRIDCOIN_WALLET_PASSPHRASE='a-passphrase-no-import-may-hold'",
            "GRIDCOIN_RPC_PASSWORD='also-not-yours'",
            "XRP_PAYOUT_SECRET_SEED='nor-this'",
        )
    )

    assert found == {"SWAP_DB_DIR": "/moved/here"}, (
        f"read_env_file() returned {sorted(found)}; it may return only {sorted(ENV_FILE_KEYS)}"
    )
    assert "a-passphrase-no-import-may-hold" not in str(found)
    for secret_name in ("GRIDCOIN_WALLET_PASSPHRASE", "GRIDCOIN_RPC_PASSWORD", "XRP_PAYOUT_SECRET_SEED"):
        assert secret_name not in ENV_FILE_KEYS, (
            f"{secret_name} is in the allowlist. Reading it arms every host process that imports "
            f"Config with the ability to unlock a wallet. That is the operator's decision."
        )


def test_both_allowlisted_keys_name_a_location_and_there_are_only_two(dotenv):
    """A gate on the allowlist's SIZE, because a third entry is an arming decision.

    Rule 19: this is not a baseline to grow. If a third key genuinely belongs here, the
    test changes in the same commit and the commit message says why -- which is the
    point: the change has to be visible rather than arriving inside a tuple.
    """
    assert ENV_FILE_KEYS == ("SWAP_DB_PATH", "SWAP_DB_DIR"), (
        f"ENV_FILE_KEYS is {ENV_FILE_KEYS}. Every entry must name a filesystem location and no "
        f"entry may be a credential; see this module's docstring."
    )


def test_an_absent_or_unreadable_dotenv_falls_through_rather_than_raising(tmp_path, monkeypatch):
    """Config is imported by everything, so it must not fail to import over a missing file.

    AND OVER AN UNREADABLE ONE. set_grc_passphrase.sh chmods .env to 600 on purpose, so
    a process running as another user hits a PermissionError rather than a
    FileNotFoundError -- and a diagnostic that died on that would be unrunnable on
    exactly the host that has a passphrase stored.
    """
    monkeypatch.delenv("SWAP_DB_PATH", raising=False)

    missing = tmp_path / "nothing" / ".env"
    monkeypatch.setattr(config, "ENV_FILE", missing)
    assert read_env_file() == {}
    assert database_path().endswith(DB_FILENAME), "the built-in default must still resolve"

    unreadable = tmp_path / ".env"
    unreadable.write_text("SWAP_DB_DIR=/unreadable\n")
    unreadable.chmod(0o000)
    monkeypatch.setattr(config, "ENV_FILE", unreadable)
    try:
        # root can read a 0000 file, so this asserts on the OUTCOME either way: it
        # must not raise, and whichever answer it gives must be a usable path.
        assert read_env_file() in ({}, {"SWAP_DB_DIR": "/unreadable"})
        assert database_path().endswith(DB_FILENAME)
    finally:
        unreadable.chmod(0o600)


def test_reading_the_file_does_not_touch_the_process_environment(dotenv, monkeypatch):
    """NOT load_dotenv(), and rule 12's import-time-side-effect ban is why.

    config.py's own header says, and has said since it was written, that nothing here
    reads a .env because "adding load_dotenv() to a module read at import is the
    import-time side effect rule 12 names as a measured past defect." That stays true:
    what the rule forbids is MUTATING os.environ, which makes every later import
    order-dependent. This reads into a dict and returns it.

    So the assertion is that os.environ is unchanged -- which is also what keeps the
    precedence above honest, since a read that leaked into the environment would make
    the file indistinguishable from an export on the NEXT call.
    """
    dotenv("SWAP_DB_DIR=/moved/here", "GRIDCOIN_WALLET_PASSPHRASE='not-yours'")
    before = dict(os.environ)

    read_env_file()
    database_path()

    assert dict(os.environ) == before, "reading the dotenv file mutated the process environment"
    assert "SWAP_DB_DIR" not in os.environ
    assert "GRIDCOIN_WALLET_PASSPHRASE" not in os.environ
