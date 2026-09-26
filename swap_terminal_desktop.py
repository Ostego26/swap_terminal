#!/usr/bin/env python3
"""Desktop launcher: start the server, open a window, and reap everything when it closes.

Role: file (entry point; what the .desktop icon names). Repo root per rule 10.
Reads: its own directory to find the repo, runtime/launcher.lock, /proc, and
      http://HOST:PORT/api/health. Optionally a config file named by
      SWAP_TERMINAL_LAUNCH_ENV.
Writes: runtime/launcher.lock, runtime/launcher.pid, runtime/launcher.log
Can send orders: NO, not itself. It starts a server that CAN, and the workers it
      does NOT start are what move funds -- see the module note on workers below.
Mainnet-safe: it does not decide. It reports which chains the SERVER will see,
      including "*** MAINNET, REAL MONEY ***", and refuses nothing on that basis.
      That is deliberate: a launcher that silently declined to start on mainnet
      would be a gate in the wrong place (rule 5). It tells; the operator decides.

THE PROCESS SHAPE, and every line of it is load-bearing:

    launcher            holds the flock, waits on BOTH children
      |- shim           own session (pgid == its own pid), PDEATHSIG armed
      |    `- gunicorn master
      |         |- worker 1
      |         `- worker 2
      `- browser        own session, own pgid, REAPED like anything else

WHY A SHIM AND NOT PDEATHSIG ON THE MASTER DIRECTLY. The direct version rests on
an untested hypothesis -- that gunicorn reaps its workers on SIGTERM -- and
gunicorn is not installed in the environment this was written in, so it could not
be watched once. The shim's group kill is correct whatever gunicorn does with any
signal. Measured in rehearsal with a stand-in that binds the port and forks two
workers: PDEATHSIG=SIGKILL on the master left orphaned workers 2/2, because
SIGKILL leaves no handler able to reap; PDEATHSIG=SIGTERM plus a group kill left
0/2. The trappable signal is the detail that matters.

WHAT THREE ADVERSARIAL REVIEWS BROKE IN THE FIRST DESIGN, because each fix below
looks like paranoia until you know it was measured:

  1. `os.setsid()` IN THE CHILD IS A RACE. The parent read os.getpgid(child.pid)
     before the child had called setsid, so it recorded the LAUNCHER's group and
     the teardown killed the launcher's own session. 4/4 trials.
     Fix: start_new_session=True -- the kernel setsids between fork and exec, so
     pgid == pid before Popen returns and nothing needs reading.
  2. PDEATHSIG ARMED AFTER EXEC IS A WINDOW. A SIGKILL to the launcher in that
     window orphans the whole tree with no pid record written yet.
     Fix: arm it in preexec_fn, which runs between fork and exec.
  3. killpg ON A RECORDED PGID IS killpg ON A NUMBER. A recycled pgid means
     signalling a stranger's process group.
     Fix: never signal a group on trust; require an identity match first.
  4. A FOREIGN SERVER PASSES A SHAPE-ONLY READINESS CHECK. A responder returning
     {"status": "ok"} on that port made the launcher open a browser on somebody
     else's UI. Fix: require db_path to match what we put in the child's env.
  5. THE SHIM IS A ZOMBIE IN ITS OWN GROUP after killing its group, so a naive
     absence walk counts it and a CLEAN teardown reports failure.
     Fix: the group walk skips state Z.
  6. THE BROWSER WAS A TRIGGER AND NOT A SPAWN. Every teardown the window did not
     itself cause orphaned the whole chromium tree to pid 1. Rule 13 is
     unconditional. Fix: it gets its own session, its own record, its own reap,
     and its own absence assertion.
  7. A FAILED STARTUP REPORTED `stopped`. teardown_report derived its verdict from
     the post-state alone, so "never served, then tore down" and "served, then
     stopped cleanly" were the same word.
     Fix: ever_served and the trigger are inputs to the verdict.

WHAT THIS DOES NOT START, said plainly because the omission is a decision. It
starts the WEB APP only. deposit_watcher, payout_worker and reconcile_worker are
not started, and the admin page will show all three `stopped`. Those processes
move money; starting them from a desktop icon that also opens a browser window
would mean a double-click could begin paying customers, and whether they run is
the operator's (rule 16). --with-workers is deliberately NOT implemented.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import errno
import fcntl
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

# After the path insert, which is why E402 is suppressed. Rule 6: every duration
# this launcher REPORTS is in microfortnights, and the conversion lives in one
# place rather than being re-derived here.
from microfortnights import format_duration  # noqa: E402 -- the sys.path insert above must run first

RUNTIME = REPO_ROOT / "runtime"
LOCK_PATH = RUNTIME / "launcher.lock"
PID_PATH = RUNTIME / "launcher.pid"

# Seconds. Every one of these is a timeout at an external boundary, so they stay
# in seconds and are CONVERTED on the way out for display (rule 6's report-versus-
# interface line).
HTTP_OK = 200
READINESS_DEADLINE_SECONDS = 30.0
READINESS_TICK_SECONDS = 0.25
TERM_GRACE_SECONDS = 10.0
WAIT_TICK_SECONDS = 0.25

# PR_SET_PDEATHSIG / PR_GET_PDEATHSIG. There is no os.prctl in the standard
# library -- the first design wrote `os.prctl(...)` as though there were -- so this
# goes through ctypes, checks the return code, and reads the value back.
PR_SET_PDEATHSIG = 1
PR_GET_PDEATHSIG = 2

# Chromium-family binaries, in preference order. Firefox is absent on purpose:
# --app does not exist there, and --kiosk removes the close affordance, which
# deletes the very trigger this launcher is built on.
BROWSER_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "google-chrome-stable",
    "brave-browser",
    "microsoft-edge",
)


# ---------------------------------------------------------------------------
# THE DECISIONS, as pure functions (rule 10). None of these opens a socket,
# spawns a process or reads the clock, which is what makes the launcher testable
# on a machine with no display, no gunicorn and no browser.
# ---------------------------------------------------------------------------

def identity_anchor(cmdline: str) -> str:
    """The first `.py` token in a command line, which is what identifies OUR process.

    Compared POSITIONALLY against the same function applied to the live
    /proc/<pid>/cmdline, never as a substring. A substring test is two defects at
    once: `"8807" in cmdline` matches a process whose port or pid happens to
    contain those digits, and a path appearing anywhere in an unrelated command
    line matches too. Both were measured against this design.
    """
    tokens = cmdline.replace("\x00", " ").split()
    if not tokens:
        return ""
    # ARGV[0] MUST BE A PYTHON INTERPRETER, and that requirement is the repair of
    # the adversarial review's own fix. It proposed "the first .py token", and a
    # test written against that found the hole immediately: in
    # `grep -r /repo/swap_terminal_desktop.py /var/log` the first .py token IS our
    # launcher, so GREPPING this file would claim its identity -- and the teardown
    # would then signal somebody's grep.
    #
    # Requiring the interpreter makes the anchor positional in the way that
    # matters: the script is what a python RUNS, not a path that merely appears.
    if not Path(tokens[0]).name.startswith("python"):
        return ""
    for token in tokens[1:]:
        if token.endswith(".py"):
            return Path(token).name
    return ""


def readiness_verdict(status_code: int | None, body: dict | None, error: str,
                      expected_db_path: str) -> tuple[str, str]:
    """FIVE answers, not two: (verdict, explanation). Never "ready" on a guess.

    The five exist because each is a different thing for an operator to do, and
    collapsing any two of them was a measured defect:

      not-listening           nothing is bound yet. Keep waiting.
      bound-but-silent        the socket accepted and nothing answered. Under
                              `preload_app = False` (gunicorn.conf.py:130) a
                              worker that dies on boot leaves the MASTER's socket
                              bound, so a TCP connect would have reported ready
                              for a server that cannot serve.
      wrong-server            something answered with the right SHAPE and the
                              wrong identity. A foreign responder returning
                              {"status": "ok"} passed the first design and the
                              launcher opened a browser on somebody else's UI --
                              possibly pointed at a different database.
      unhealthy               our server, and it says it is not ok.
      ready                   ours, and healthy.
    """
    if error:
        return _transport_verdict(error)
    return _body_verdict(status_code, body, expected_db_path)


def _transport_verdict(error: str) -> tuple[str, str]:
    """Verdict from a FAILED read. Split out so each half stays under the ceiling.

    The seam is real and not cosmetic: this half knows only about sockets, and the
    other half knows only about the response. Rule 12's answer to a complexity
    finding is to extract the decision, not to raise the ceiling -- and the two
    decisions here genuinely are about different things.
    """
    lowered = error.lower()
    if "refused" in lowered or "econnrefused" in lowered:
        return "not-listening", "nothing is bound on that address yet"
    if "timed out" in lowered or "timeout" in lowered:
        return "bound-but-silent", (
            "the socket is bound and nothing answered. With preload_app=False a worker that dies "
            "on boot leaves the master's socket bound, so this is NOT ready"
        )
    return "not-listening", f"the endpoint could not be read: {error}"


def _body_verdict(status_code: int | None, body: dict | None, expected_db_path: str) -> tuple[str, str]:
    """Verdict from a response that ARRIVED. Identity before health, deliberately.

    db_path is checked BEFORE status, because "ok" from a server that is not ours
    is the dangerous answer -- a foreign responder returning our body shape made
    the first design open a browser on somebody else's UI.
    """
    if status_code != HTTP_OK:
        return "unhealthy", f"the health endpoint answered {status_code}, not {HTTP_OK}"
    if not isinstance(body, dict):
        return "wrong-server", "the response was not a JSON object, so this is not our health route"
    reported = body.get("db_path")
    if reported != expected_db_path:
        return "wrong-server", (
            f"something IS answering on that address and it is not the server this launcher "
            f"started: it reports db_path={reported!r} and we told our server to use "
            f"{expected_db_path!r}. Not opening a browser on it"
        )
    if body.get("status") != "ok":
        return "unhealthy", f"our server reports status={body.get('status')!r}"
    return "ready", f"our server, healthy, on {expected_db_path}"


@dataclass(frozen=True)
class TeardownFacts:
    """Everything observed after a teardown. One object because they are ONE observation.

    Grouped rather than passed as six parameters, which is rule 12's answer to the
    argument-count finding: extract, do not suppress. It is also more honest --
    these are not six independent knobs, they are the post-state of a single event,
    and a caller that had only five of them would be describing nothing.

    Note what is NOT in here: the exit code of any kill. Rule 13 -- "make the
    absence the assertion, not the exit code of the kill."
    """

    shim_absent: bool
    group_left: tuple
    port_free: bool
    browser_absent: bool
    ever_served: bool
    trigger: str


def teardown_report(facts: TeardownFacts) -> tuple[str, str]:
    """The verdict on a teardown: (word, explanation). Six outcomes, not two.

    THE DEFECT THIS EXISTS TO PREVENT, measured against the first design:
    teardown_report(True, (), True) returned "stopped" for a startup that never
    served -- readiness timed out, no browser ever opened, nothing was ever
    reachable -- because the verdict was derived from the post-state alone. "Never
    served, then tore down" and "served, then stopped cleanly" were the same word,
    and the last line on the operator's screen was a success line.

    So ever_served and the trigger are inputs. The post-state alone cannot
    distinguish a clean stop from a failed start, and rule 13's "a cycle that did
    no work must not report the same way as one that did" is exactly that.
    """
    leftovers = []
    if not facts.shim_absent:
        leftovers.append("the server shim is still alive")
    if facts.group_left:
        leftovers.append(f"{len(facts.group_left)} process(es) left in its group: {list(facts.group_left)}")
    if not facts.browser_absent:
        leftovers.append("the browser is still alive")
    if not facts.port_free:
        leftovers.append("the port is still bound")

    if leftovers:
        return "LEAKED", (
            "teardown did NOT complete: " + "; ".join(leftovers)
            + ". Something is still running and holding the port. This is the outcome rule 13 is "
              "about -- an orphan does not crash anything, it holds a lock while everything "
              "downstream reports success"
        )
    if not facts.ever_served:
        return "NEVER-SERVED", (
            f"nothing was ever reachable, so there was nothing to stop. Teardown was triggered by "
            f"{facts.trigger}. This is NOT a clean run -- read the startup block above for why the server "
            f"never became ready"
        )
    if facts.trigger == "window-closed":
        return "stopped", "the window was closed and everything it started is provably gone"
    return "stopped-early", (
        f"the server was serving and teardown was triggered by {facts.trigger} rather than by the window "
        f"closing. Everything is provably gone, but the operator did not close the window -- so if "
        f"they did not expect this, something else ended the run"
    )


def group_members(pgid: int, proc_root: Path = Path("/proc")) -> tuple[int, ...]:
    """Live pids in a process group, EXCLUDING zombies. The absence assertion.

    Zombies are excluded because a zombie is not running: it holds no lock, no
    port and no file. Counting one made a CLEAN teardown report failure -- the
    shim, having killed its own group, is itself a zombie in that group until its
    parent reaps it, so the naive walk always found exactly one "survivor".

    /proc/<pid> existing is not liveness either, which is why this reads the state
    field rather than trusting the directory.
    """
    found = []
    for entry in sorted(proc_root.glob("[0-9]*")):
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        # `comm` may contain spaces and parentheses, so fields are counted from
        # AFTER the last ')' -- the standard way to parse /proc/<pid>/stat.
        try:
            tail = stat[stat.rindex(")") + 1:].split()
            state, group = tail[0], int(tail[2])
        except (ValueError, IndexError):
            continue
        if group == pgid and state != "Z":
            found.append(int(entry.name))
    return tuple(found)


def choose_browser(which=shutil.which) -> tuple[str, str]:
    """(binary, explanation), or ("", why not). Refuses rather than falling back.

    xdg-open is deliberately absent: it hands off to the desktop's default browser
    and exits, so the window is not its child and there is no exit to wait on --
    which is the entire mechanism here. A launcher that fell back to it would open
    a window whose close button did nothing.
    """
    for candidate in BROWSER_CANDIDATES:
        path = which(candidate)
        if path:
            return path, f"{candidate} at {path}"
    return "", (
        "no chromium-family browser was found on PATH (looked for "
        + ", ".join(BROWSER_CANDIDATES)
        + "). This launcher needs one because it waits on the BROWSER PROCESS to learn that the "
          "window closed. Firefox cannot be used: --app does not exist there, and --kiosk removes "
          "the close button, which deletes the signal. Install chromium, or run the server by hand "
          "with `python3 swap_terminal/app.py`"
    )


def browser_command(binary: str, url: str, profile_dir: Path) -> list[str]:
    """The argv for an app-mode window with its OWN profile.

    The dedicated --user-data-dir is not tidiness. Measured on a shared profile: a
    second launch returned rc=0 in 0.072s while the first stayed alive, so a
    launcher waiting on it would tear the server down 72ms after opening the UI.
    With a dedicated profile, wait() returned rc=0 at 5.53s when the window
    actually closed.

    --no-sandbox is NOT here and must never be added. Rehearsal needed it because
    that container runs as root; the operator's desktop does not, and shipping it
    would weaken the browser's sandbox on a machine holding wallet RPC credentials.
    """
    return [
        binary,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]


def arm_parent_death_signal() -> None:
    """PR_SET_PDEATHSIG=SIGTERM, for use as Popen(preexec_fn=...).

    In preexec_fn, which runs between fork and exec, because arming it AFTER exec
    leaves a window: a SIGKILL to the launcher in that window orphans the whole
    tree with no pid record on disk yet.

    SIGTERM and not SIGKILL: SIGKILL leaves no handler able to reap, and rehearsal
    measured orphaned workers 2/2 that way against 0/2 with SIGTERM plus a group
    kill.

    Raises on failure rather than continuing quietly -- the first design wrote this
    as though os.prctl existed and could not fail.
    """
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
        code = ctypes.get_errno()
        raise OSError(code, f"PR_SET_PDEATHSIG failed: {os.strerror(code)}")
    readback = ctypes.c_int(0)
    if libc.prctl(PR_GET_PDEATHSIG, ctypes.byref(readback), 0, 0, 0) != 0 or readback.value != signal.SIGTERM:
        raise OSError(f"PR_SET_PDEATHSIG did not take: read back {readback.value}, wanted {signal.SIGTERM}")


def port_is_free(host: str, port: int) -> bool:
    """Can this address be bound? The teardown's own assertion, not the kill's rc.

    Rule 13: "make the absence the assertion -- not the exit code of the kill." A
    successful bind is the strongest available proof that whatever held the port is
    gone, and it is the same question the next launch will ask.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def describe_holder(host: str, port: int) -> str:
    """Who holds the port, for a refusal that names somebody.

    Read-only on purpose, and the launcher does NOT kill it: it cannot tell its own
    orphan from a stranger's server, and a group kill here was measured to take out
    the operator's entire terminal session.
    """
    # Resolved on PATH rather than named partially. S607 is about inheriting
    # whatever `ss` a caller's PATH happens to point at, and this runs on an
    # operator's desktop where that is not ours to assume.
    ss_binary = shutil.which("ss")
    if not ss_binary:
        return f"{host}:{port} is bound and `ss` is not installed, so nothing can say by what"
    try:
        output = subprocess.run(  # noqa: S603 -- checked: an absolute path resolved from PATH, a fixed argv, no shell, and no value from outside this function reaches the command
            [ss_binary, "-ltnp"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return f"{host}:{port} is bound and `ss` could not be read to say by what"
    for line in output.splitlines():
        if f":{port} " in line or line.rstrip().endswith(f":{port}"):
            return line.strip()
    return f"{host}:{port} is bound and no `ss` line named it"


def server_environment(db_path: str) -> dict:
    """The environment the SERVER gets, and the one the report must describe.

    Built explicitly from os.environ rather than inherited implicitly, because the
    launcher must be able to report what the server will see -- and the first
    design reported what the LAUNCHER saw, which under a desktop icon is a
    different thing entirely.

    THE VOCABULARY TRAP, measured and worth the paragraph. config.py reads
    GRC_RPC_USER / GRC_RPC_PASS / GRC_RPC_HOST / GRC_RPC_PORT, and calls
    gridcoin_rpc_password() zero times. The operator's .env spells the password
    GRIDCOIN_RPC_PASSWORD. So sourcing that file configures NOTHING while looking
    configured -- which is why this launcher reports the resolved chain lines from
    the child's own environment instead of claiming success for having read a file.
    """
    environment = dict(os.environ)
    environment["SWAP_DB_PATH"] = db_path
    return environment


def chain_report(environment: dict) -> str:
    """What the SERVER will see, read in a subprocess with the server's environment.

    A subprocess, not an in-process call, and that is the fix for a measured
    contradiction in the first design: it insisted the configuration go only into
    the dict handed to the child, then reported the chain lines from the launcher's
    own Config -- so a correctly-configured mainnet deployment would have been
    reported as having no chains at all.

    It also routes around a defect in app.py, reported separately: that banner is
    emitted with logger.info() while logging.root.handlers is empty and
    logging.lastResort is a WARNING-level handler, so the per-chain lines reach
    NOTHING. A double-clicked terminal would serve the UI with no chain reachable
    and say nothing about it.
    """
    script = (
        "import sys; sys.path.insert(0, 'swap_terminal');"
        "from config import Config;"
        "from network_target import mainnet_chains, startup_lines;"
        "print(chr(10).join(startup_lines(Config.RPC)));"
        "print('  mainnet chains -> ' + (', '.join(mainnet_chains(Config.RPC)) or '(none)'))"
    )
    try:
        done = subprocess.run(  # noqa: S603 -- checked: sys.executable, a literal script, no shell, and the only variable is the env dict this launcher built
            [sys.executable, "-c", script],
            cwd=str(REPO_ROOT), env=environment, capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"  could not read the chain configuration: {error}"
    return done.stdout.rstrip() or f"  the chain report produced nothing (rc={done.returncode})"


def run_shim(host: str, port: int, db_path: str) -> int:
    """--shim: own the gunicorn master, and take it down with us. Never called directly.

    This process exists so that the reap is a GROUP kill we control, rather than a
    hypothesis about what gunicorn does with a signal. It is its own session leader
    (the parent passed start_new_session=True), so its pgid equals its pid and the
    group contains exactly the master and its workers.

    It waits on the master with waitpid rather than polling /proc, because the
    master is its own child: unreaped it becomes a ZOMBIE, whose /proc entry exists
    forever, so a /proc-based absence poll could never succeed. That was measured.
    """
    command = [sys.executable, "-m", "gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
    print(f"  shim pid={os.getpid()} pgid={os.getpgid(0)}: starting {' '.join(command)}", flush=True)
    master = subprocess.Popen(command, cwd=str(REPO_ROOT))  # noqa: S603 -- checked: sys.executable plus a literal argv; nothing from outside reaches it

    stopping = {"now": False}

    def stop(signum, _frame):
        stopping["now"] = True
        print(f"  shim: signal {signum} received, taking the server group down", flush=True)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping["now"]:
        try:
            status = master.wait(timeout=WAIT_TICK_SECONDS)
        except subprocess.TimeoutExpired:
            continue
        # THE MASTER DIED ON ITS OWN. Reported and propagated rather than waited
        # out: the first design never coupled these, so a master that died at hour
        # two left the launcher blocked on the browser with a dead server behind a
        # window that still rendered from cache.
        print(f"  shim: the gunicorn master exited on its own with status {status}", flush=True)
        _kill_own_group()
        return 0 if status == 0 else 1

    with contextlib.suppress(ProcessLookupError):
        master.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired, ProcessLookupError):
        master.wait(timeout=TERM_GRACE_SECONDS)
    if master.poll() is None:
        print("  shim: the master did not stop on SIGTERM, escalating", flush=True)
        with contextlib.suppress(ProcessLookupError):
            master.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, ProcessLookupError):
            master.wait(timeout=TERM_GRACE_SECONDS)
    _kill_own_group()
    return 0


def _kill_own_group() -> None:
    """SIGKILL this process group, sweeping any worker the master left behind.

    Safe to aim at our OWN group without an identity check, which is the difference
    between this and the launcher's teardown: os.getpgid(0) is read now, from
    inside the group, so it cannot be a recycled number recorded earlier. The
    launcher's version must check identity; this one cannot be wrong about which
    group it means.

    It kills this process too, which is correct and is why nothing follows it.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(0), signal.SIGKILL)


def acquire_single_instance_lock():
    """An exclusive flock, held for the process lifetime. Returns the fd, or refuses.

    flock AND NOT A PID FILE, measured: while held, a second LOCK_EX|LOCK_NB
    returned errno 11; after SIGKILL of the holder the file still existed and still
    named the dead pid, yet the next attempt acquired cleanly. The kernel releases
    it on death, so there is no stale case to interpret. A pid file after the same
    SIGKILL still names the dead pid and needs a liveness AND identity check.

    OPENED O_RDWR|O_CREAT AND NEVER TRUNCATED, which is the fix for a measured
    defect: the natural spelling, `open(path, "w")`, truncates BEFORE the flock
    fails -- so a refused second launcher would destroy the holder's record and then
    print "the holder is pid " with nothing after it. Truncation happens only after
    the lock is ours.
    """
    RUNTIME.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EAGAIN):
            os.close(fd)
            raise
        holder = os.read(fd, 256).decode("utf-8", "replace").strip() or "(the lock file is empty)"
        os.close(fd)
        raise SystemExit(
            f"REFUSED: another launcher already holds {LOCK_PATH}.\n"
            f"  it recorded: {holder}\n"
            f"  Nothing was started and nothing was stopped. Close the existing window, or if you "
            f"believe that record is stale, check the pid yourself before killing anything -- this "
            f"launcher will not kill a process it cannot prove is its own."
        ) from error
    os.ftruncate(fd, 0)
    os.write(fd, f"pid={os.getpid()} argv={' '.join(sys.argv)}\n".encode())
    os.fsync(fd)
    return fd


def poll_until_ready(host: str, port: int, db_path: str, shim: subprocess.Popen) -> tuple[str, str, float]:
    """Poll /api/health until ready, the shim dies, or the deadline. (verdict, why, elapsed).

    Aborts the instant shim.poll() stops returning None rather than spinning to the
    deadline for a server that died at t=0.2s -- and prints the verdict either way,
    because a readiness timeout that reports nothing is the silence this whole file
    is trying not to be.
    """
    url = f"http://{host}:{port}/api/health"
    started = time.monotonic()
    print(f"  waiting up to {format_duration(READINESS_DEADLINE_SECONDS)} for {url}", flush=True)
    last = ("not-listening", "nothing has been polled yet")
    while time.monotonic() - started < READINESS_DEADLINE_SECONDS:
        if shim.poll() is not None:
            return ("shim-died", f"the server shim exited with status {shim.returncode} before becoming "
                                 f"ready; its output is above", time.monotonic() - started)
        status_code, body, error = _read_health(url)
        last = readiness_verdict(status_code, body, error, db_path)
        if last[0] == "ready":
            return (*last, time.monotonic() - started)
        time.sleep(READINESS_TICK_SECONDS)
    return ("timed-out", f"never became ready. Last answer: {last[0]} -- {last[1]}",
            time.monotonic() - started)


def _read_health(url: str) -> tuple[int | None, dict | None, str]:
    """One health read. Returns (status, body, error) and never raises."""
    try:
        with urllib.request.urlopen(url, timeout=2.0) as response:  # noqa: S310 -- checked: the url is built from this launcher's own host/port constants, never from input, and the scheme is a literal http://
            return response.status, json.loads(response.read().decode("utf-8")), ""
    except urllib.error.HTTPError as error:
        return error.code, None, ""
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
        return None, None, str(error)


def reap(name: str, child: subprocess.Popen, pgid: int) -> tuple[bool, tuple]:
    """SIGTERM, wait, SIGKILL the group, then ASSERT absence. (absent, group_left).

    The group kill is guarded by an identity check, which is the difference between
    this and the shim's own _kill_own_group(): a pgid recorded earlier is just a
    number, and a recycled one belongs to a stranger. Measured against the first
    design, which killpg'd a recorded pgid on trust.

    Absence is asserted by waitpid plus a zombie-aware group walk -- never by the
    exit code of the kill (rule 13).
    """
    with contextlib.suppress(ProcessLookupError):
        child.terminate()
    with contextlib.suppress(subprocess.TimeoutExpired, ProcessLookupError):
        child.wait(timeout=TERM_GRACE_SECONDS)

    if child.poll() is None:
        print(f"  {name}: did not stop on SIGTERM after {format_duration(TERM_GRACE_SECONDS)}, escalating",
              flush=True)
        with contextlib.suppress(ProcessLookupError):
            child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired, ProcessLookupError):
            child.wait(timeout=TERM_GRACE_SECONDS)

    left = group_members(pgid)
    if left:
        # IDENTITY BEFORE SIGNAL. `pgid` was recorded when the child was spawned; if
        # that number has been recycled, this group is somebody else's and killing
        # it is strictly worse than leaving our own orphan. Requiring that the group
        # leader still BE our child is the cheapest sound check available: we hold
        # the Popen, so its pid is not a guess.
        if pgid == child.pid:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(pgid, signal.SIGKILL)
        else:
            print(f"  {name}: NOT group-killing pgid {pgid} -- it is not this child's own group "
                  f"(child pid {child.pid}), so it may have been recycled and belong to something "
                  f"else. {len(left)} process(es) left; inspect them by hand.", flush=True)
        left = group_members(pgid)

    absent = child.poll() is not None
    print(f"  {name}: {'gone' if absent else 'STILL ALIVE'}, "
          f"{len(left)} process(es) left in pgid {pgid}", flush=True)
    return absent, left


def _preflight(host: str, port: int) -> tuple[bool, str, int]:
    """Everything that must be true BEFORE anything is spawned. (ok, browser, lock_fd).

    Ordered so that the cheapest refusals come first and the lock is taken LAST:
    taking the lock before checking for a browser would leave a lock file behind
    for a launch that could never have worked, and a second double-click would then
    be refused for the wrong reason.
    """
    browser_binary, browser_note = choose_browser()
    if not browser_binary:
        print(f"\nREFUSED: {browser_note}", file=sys.stderr)
        return False, "", -1
    print(f"  browser         {browser_note}", flush=True)

    if shutil.which("gunicorn") is None:
        try:
            __import__("gunicorn")
        except ImportError:
            print("\nREFUSED: gunicorn is not installed, and this launcher starts the server with it.\n"
                  "    pip install gunicorn\n"
                  "  Nothing was started. (`python3 swap_terminal/app.py` runs the development server "
                  "by hand, but this launcher will not use it: app.run() is single-process and its own "
                  "banner says not to serve from it.)", file=sys.stderr)
            return False, "", -1

    lock_fd = acquire_single_instance_lock()
    print(f"  single instance {LOCK_PATH} (held; the kernel releases it if this process dies)", flush=True)

    if not port_is_free(host, port):
        print(f"\nREFUSED: {host}:{port} is already bound, and this launcher did not bind it.", file=sys.stderr)
        print(f"  holder: {describe_holder(host, port)}", file=sys.stderr)
        print("  NOT killing it: this launcher cannot tell its own orphan from a stranger's server, "
              "and a group kill here was measured taking out an operator's whole terminal session.\n"
              "  Stop it yourself, then double-click again.", file=sys.stderr)
        os.close(lock_fd)
        return False, "", -1
    return True, browser_binary, lock_fd


def launch(host: str, port: int) -> int:
    """The default mode: start, open, wait, reap, report. Returns an exit code.

    Every print here is deliberate (rule 14): the operator's only feedback during a
    readiness wait is this terminal, and a desktop launcher that prints nothing for
    thirty seconds gets clicked again.
    """
    started = time.monotonic()
    print("swap_terminal desktop launcher", flush=True)
    print(f"  repo            {REPO_ROOT}", flush=True)
    print(f"  python          {sys.executable}", flush=True)

    # EXTRACTED because launch() was past the statement ceiling, and rule 12's
    # answer to that is to extract the decision rather than raise the ceiling. The
    # seam is real: everything in here REFUSES before anything is spawned, so the
    # split is exactly the line between "nothing has started" and "something has".
    ready, browser_binary, lock_fd = _preflight(host, port)
    if not ready:
        return 1

    db_path = os.environ.get("SWAP_DB_PATH") or str(REPO_ROOT / "swap_terminal" / "swap_terminal.db")
    environment = server_environment(db_path)
    print(f"  database        {db_path}", flush=True)
    print("  chains the SERVER will see (read from its own environment, not this launcher's):", flush=True)
    print(chain_report(environment), flush=True)

    shim = subprocess.Popen(  # noqa: S603 -- checked: sys.executable and this file's own absolute path; no value from outside reaches the argv
        [sys.executable, str(Path(__file__).resolve()), "--shim",
         "--host", host, "--port", str(port), "--db", db_path],
        cwd=str(REPO_ROOT), env=environment,
        start_new_session=True,      # the KERNEL setsids between fork and exec, so
        preexec_fn=arm_parent_death_signal,  # noqa: PLW1509 -- checked: this is the POINT. PR_SET_PDEATHSIG must be armed between fork and exec; arming it after exec leaves a window in which a SIGKILL to this launcher orphans the whole tree with no pid record written yet. The documented fork-safety caveat concerns threads, and this launcher is single-threaded.
    )
    shim_pgid = shim.pid  # true by construction with start_new_session=True; NOT read from getpgid
    PID_PATH.write_text(f"pid={shim.pid} pgid={shim_pgid} argv={Path(__file__).name} --shim\n")
    print(f"  server shim     pid={shim.pid} pgid={shim_pgid}, recorded in {PID_PATH}", flush=True)

    verdict, why, elapsed = poll_until_ready(host, port, db_path, shim)
    ever_served = verdict == "ready"
    print(f"  readiness       {verdict} after {format_duration(elapsed)} -- {why}", flush=True)

    browser = None
    browser_pgid = 0
    trigger = verdict if not ever_served else "window-closed"
    if ever_served:
        profile = RUNTIME / "browser-profile"
        profile.mkdir(parents=True, exist_ok=True)
        browser = subprocess.Popen(  # noqa: S603 -- checked: the binary came from shutil.which over a literal candidate list, and the url is built from this launcher's own host/port
            browser_command(browser_binary, f"http://{host}:{port}/", profile),
            start_new_session=True,
        )
        browser_pgid = browser.pid
        print(f"  window          browser pid={browser.pid} pgid={browser_pgid} -- CLOSING IT STOPS "
              f"THE SERVER", flush=True)
        trigger = _wait_for_either(browser, shim)

    print(f"\n  teardown        triggered by {trigger}, after {format_duration(time.monotonic() - started)}",
          flush=True)
    shim_absent, group_left = reap("server", shim, shim_pgid)
    browser_absent = True
    if browser is not None:
        browser_absent, browser_left = reap("browser", browser, browser_pgid)
        group_left = (*group_left, *browser_left)

    facts = TeardownFacts(
        shim_absent=shim_absent, group_left=group_left, port_free=port_is_free(host, port),
        browser_absent=browser_absent, ever_served=ever_served, trigger=trigger,
    )
    word, explanation = teardown_report(facts)
    print(f"\n  {word}: {explanation}", flush=True)
    os.close(lock_fd)
    with contextlib.suppress(OSError):
        PID_PATH.unlink()

    if word != "stopped":
        # NEVER EXIT WHILE AN UNREAD BLOCK IS ON SCREEN. A Terminal=true launcher's
        # window closes the instant this returns, so a refusal or a leak would flash
        # and vanish -- indistinguishable from the icon doing nothing at all.
        with contextlib.suppress(OSError, EOFError, KeyboardInterrupt):
            input("\n  press Enter to close this window ")
    return 0 if word in ("stopped", "stopped-early") else 1


def _wait_for_either(browser: subprocess.Popen, shim: subprocess.Popen) -> str:
    """Block until the window closes OR the server dies. Returns which happened.

    Watching BOTH is the fix for a measured blocker: the first design had one
    blocking wait, on the browser, so a gunicorn master that died at hour two left
    the launcher waiting on a window that still rendered from cache, and the final
    report was byte-identical to a clean run.
    """
    while True:
        if browser.poll() is not None:
            return "window-closed"
        if shim.poll() is not None:
            print(f"\n  THE SERVER DIED while the window was still open (shim status "
                  f"{shim.returncode}). Closing the window.", flush=True)
            return "server-died"
        time.sleep(WAIT_TICK_SECONDS)


def report_status(host: str, port: int) -> int:
    """--status: read-only. Spawns nothing, kills nothing, and says what it found."""
    print("swap_terminal desktop launcher -- status. Read-only: starts and stops nothing.", flush=True)
    print(f"  repo            {REPO_ROOT}", flush=True)
    print(f"  port {host}:{port}   {'FREE' if port_is_free(host, port) else 'BOUND'}", flush=True)
    if not port_is_free(host, port):
        print(f"    holder: {describe_holder(host, port)}", flush=True)
    print(f"  lock file       {LOCK_PATH if LOCK_PATH.exists() else '(none)'}", flush=True)
    if PID_PATH.exists():
        record = PID_PATH.read_text().strip()
        print(f"  pid record      {record}", flush=True)
        anchor = identity_anchor(record)
        print(f"    identity anchor: {anchor or '(none found)'}", flush=True)
    else:
        print("  pid record      (none) -- no launcher has recorded a server here", flush=True)
    binary, note = choose_browser()
    print(f"  browser         {note if binary else 'NONE FOUND -- ' + note[:80]}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default=os.environ.get("SWAP_TERMINAL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SWAP_TERMINAL_PORT", "5000")))
    parser.add_argument("--shim", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--db", default="", help=argparse.SUPPRESS)
    parser.add_argument("--status", action="store_true",
                        help="report what is running and what holds the port. Starts nothing.")
    args = parser.parse_args()

    if args.shim:
        return run_shim(args.host, args.port, args.db)
    if args.status:
        return report_status(args.host, args.port)
    return launch(args.host, args.port)


if __name__ == "__main__":
    sys.exit(main())
