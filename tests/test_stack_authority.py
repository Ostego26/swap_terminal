"""Does the stack's reaper know what it may stop, and prove what it stopped?

Role: test / measurement (seeds a fake /proc, runs the real decision functions)
Reads: swap_terminal/stack_authority.py
Writes: directories and symlinks under pytest's tmp_path
Can move funds: no. Nothing here signals a process, runs docker, or opens a socket
        to anything but a free local port it binds and closes.
Mainnet-safe: yes

WHAT THIS PINS, and all of it is a decision that would otherwise only be checkable
by running the real thing against the real host:

  - a chain daemon is NEVER stoppable, under any flag
  - a listener whose command line does not name this repository is FOREIGN and is
    left alone, which is the case that stops this project doing to a stranger what
    its own orphans did to it
  - the /proc/net/tcp parse filters on TCP_LISTEN, so the operator's open browser
    tab is not reported as a server holding the port
  - the absence proof is a BIND, not an exit code

The fake /proc is directories and symlinks because that is what the real one is:
pids_owning_inodes() takes its root as an argument for exactly this reason.
"""

from __future__ import annotations

import socket
from pathlib import Path

from swap_terminal.stack_authority import (
    NEVER_STOPPED,
    hex_port,
    listening_inodes,
    pids_owning_inodes,
    port_is_free,
    process_name,
    stray_verdict,
)

# A real /proc/net/tcp body. 0100007F is 127.0.0.1 little-endian; 13EE is 5102.
# Row 1 LISTENs on 5102. Row 2 is ESTABLISHED (st=01) on the same port -- a browser
# tab -- and must NOT be reported. Row 3 LISTENs on 9999, outside our set.
_PROC_NET_TCP = """\
  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:13EE 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 884411
   1: 0100007F:13EE 0100007F:C350 01 00000000:00000000 00:00000000 00000000  1000        0 884412
   2: 00000000:270F 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 884413
"""


def test_the_port_comes_out_of_the_kernels_hex_pair():
    assert hex_port("0100007F:13EE") == 5102
    assert hex_port("00000000:1388") == 5000
    assert hex_port("00000000:1337") == 4919


def test_an_ESTABLISHED_connection_to_our_port_is_not_a_listener():
    """The defect this avoids: counting an open browser tab as a server.

    Both rows carry local port 5102. Only the LISTEN one is a process holding the
    port; the other is the operator looking at the page. Reporting the second would
    make `down` claim a port is still bound after the server is gone.
    """
    found = listening_inodes(_PROC_NET_TCP, {5102})
    assert found == {5102: 884411}, "the ESTABLISHED row's inode 884412 must not appear"


def test_a_port_outside_the_set_is_ignored():
    assert listening_inodes(_PROC_NET_TCP, {5102}) == {5102: 884411}
    assert listening_inodes(_PROC_NET_TCP, {9999}) == {9999: 884413}
    assert listening_inodes(_PROC_NET_TCP, {5000}) == {}


def _fake_proc(root: Path, pid: int, inode: int, cmdline: str) -> None:
    """One pid under a fake /proc, holding one socket inode, with a cmdline."""
    fd_dir = root / str(pid) / "fd"
    fd_dir.mkdir(parents=True)
    (fd_dir / "5").symlink_to(f"socket:[{inode}]")
    (root / str(pid) / "cmdline").write_text(cmdline)


def test_the_pid_holding_a_socket_inode_is_found_by_scanning(tmp_path):
    _fake_proc(tmp_path, 1403174, 884411, "gunicorn\0wsgi:app\0")
    (tmp_path / "self").mkdir()          # a non-numeric entry, which must be skipped
    (tmp_path / "uptime").write_text("x")

    assert pids_owning_inodes({884411}, tmp_path) == {884411: 1403174}
    assert pids_owning_inodes({999999}, tmp_path) == {}, "an inode nobody holds is absent, not an error"


def test_a_pid_whose_fds_cannot_be_read_does_not_break_the_scan(tmp_path):
    """A process exiting between the listdir and the readlink is ordinary, not an error."""
    _fake_proc(tmp_path, 100, 884411, "python\0worker.py\0")
    (tmp_path / "200").mkdir()           # no fd/ directory at all
    assert pids_owning_inodes({884411}, tmp_path) == {884411: 100}


def test_the_process_name_is_argv0s_basename_not_the_whole_line():
    assert process_name("/usr/bin/gridcoinresearchd\0-testnet\0") == "gridcoinresearchd"
    assert process_name("tail\0-f\0bitcoind.log\0") == "tail"
    assert process_name("") == ""


def test_a_chain_daemon_is_REFUSED_and_a_log_tail_naming_one_is_not():
    """NEVER_STOPPED matches the NAME. `tail -f bitcoind.log` is not bitcoind.

    If it matched the whole command line, the tail would be refused -- harmless --
    and, far worse, the reverse mistake becomes easy to write: a pattern loose
    enough to catch a daemon by its log file is loose enough to catch a daemon this
    file has no business classifying at all.
    """
    repo = Path("/home/someone/swap_terminal")
    for daemon in NEVER_STOPPED:
        verdict, reason = stray_verdict(f"/usr/bin/{daemon}\0-testnet\0", repo)
        assert verdict == "refused", daemon
        assert reason, "a refusal with no reason is a refusal an operator cannot act on"

    verdict, _ = stray_verdict("tail\0-f\0/var/log/bitcoind.log\0", repo)
    assert verdict != "refused"


def test_a_listener_naming_this_repository_is_OURS_and_anything_else_is_FOREIGN():
    repo = Path("/home/someone/swap_terminal")

    verdict, reason = stray_verdict(f"python\0{repo}/swap_terminal/workers/payout_worker.py\0", repo)
    assert verdict == "ours"
    assert str(repo) in reason

    # The ambiguous case, and the answer is deliberately the cautious one: a
    # gunicorn whose argv does not carry the path reads FOREIGN and is left alone.
    verdict, reason = stray_verdict("gunicorn\0wsgi:app\0", repo)
    assert verdict == "foreign"
    assert "will not stop it" in reason

    verdict, _ = stray_verdict("/usr/bin/postgres\0-D\0/var/lib/postgresql\0", repo)
    assert verdict == "foreign"


def test_the_absence_proof_is_a_BIND(tmp_path):
    """port_is_free() answers by binding, which is what makes `down` provable.

    Bound to 0 first to get a port the OS says is free, then asserted free; then
    held open and asserted NOT free. Both directions, because a function that
    always returned True would pass a one-sided test and turn rule 13's proof into
    the false reassurance it replaces.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
        holder.bind(("127.0.0.1", 0))
        port = holder.getsockname()[1]
        holder.listen(1)
        assert port_is_free(port) is False, "a port with a listener on it is not free"

    assert port_is_free(port) is True, "and it is free once the holder is closed"
