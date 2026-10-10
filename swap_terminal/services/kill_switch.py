"""The start and stop switch: one lock, three workers, and the proof that they are gone.

Role: module (every decision the kill switch makes; routes/kill_switch.py holds
      none of its own and services/admin_view.py is not involved)
Reads: supervisor.py's pid files under the run directory, /proc (to confirm
      absence and to find orphans), /proc/net/tcp via loopback.py (to establish
      what this server is bound to), the request's own headers and peer address,
      SWAP_TERMINAL_HOST, and config.Config.DB_PATH to echo it
Writes: the single-flight lock file under the run directory, pid files (through
      supervisor.start_worker / stop_worker), worker log files, and SIGNALS to
      the processes those pid files name
Can move funds: **YES, INDIRECTLY, AND THIS IS THE ONLY MODULE ON THE WEB
      SURFACE THAT CAN.** `start` spawns workers/payout_worker.py, which
      BROADCASTS payouts on whatever chain the configuration points at. A click
      on the start button on a host pointed at a funded mainnet wallet begins
      paying customers. Nothing here signs or sends anything itself; it is the
      spawn that arms the thing that does. `stop` moves no funds in either
      direction -- it can only stop a process that would have.
Mainnet-safe: `stop` yes, at any time, and it is the reason this exists. `start`
      is exactly as mainnet-safe as the workers it spawns, which is not very --
      which is why supervisor.spawn_warning() has to be shown and acknowledged
      BEFORE the spawn rather than printed after it.

=============================================================================
WHAT THE OPERATOR ASKED FOR, AND WHAT "EVERYTHING" TURNED OUT TO BE
=============================================================================

    "there should be a start and stop button that turns EVERYTHING off all
     agents, workers, daemons, etc."

"EVERYTHING" was established against the tree rather than against the word,
because a button that claims to stop everything and reaches three things out of
six is worse than no button: it is rule 13's orphan with a green tick over it.
Every spawn site in this repository, grepped 2026-10-02 for `Popen`, `os.fork`,
`threading.Thread`, `multiprocessing`, `nohup`, `systemd` and `crontab`:

  REACHABLE FROM HERE -- the three supervised workers
    workers/deposit_watcher.py    spawned by supervisor.start_worker(), pid file
    workers/payout_worker.py      in the run directory, reaped by
    workers/reconcile_worker.py   supervisor.stop_worker(). These are the
                                  "agents, workers, daemons" of the request:
                                  they are the only processes in this repository
                                  that poll on a loop and act on a database
                                  without an operator present.

  NOT REACHABLE FROM HERE, AND THE PANEL SAYS SO RATHER THAN IMPLYING OTHERWISE
    gunicorn master + 2 workers   THE PROCESS RENDERING THE ANSWER. A stop that
                                  killed it could not report that it had -- the
                                  response dies with the worker, the operator
                                  sees a connection reset, and "it worked" and
                                  "it crashed before doing anything" look
                                  identical. Rule 13's "a stop that cannot prove
                                  it worked is not a stop" forbids it outright,
                                  so it is not attempted.
    swap_terminal_desktop.py      the launcher holding runtime/launcher.lock and
                                  runtime/launcher.pid. It is gunicorn's PARENT:
                                  stopping it tears down the server, which is the
                                  case above with one more process in it. It has
                                  its own reaper -- closing the window -- and
                                  that reaper is proven by its own absence walk.
    the browser window            spawned and reaped by the launcher, same
                                  reasoning.
    regtest/harness_runner.py     spawned by operator_panel.py, a separate root
                                  entry point an operator runs by hand on port
                                  8765. Its reaper is its own Ctrl-C handler, and
                                  it holds a funding output mid-spend, so it is
                                  emphatically not something a web page should
                                  signal.
    transactions.py's Tk thread   a thread inside a desktop GUI, in a process
                                  this one cannot see.
    anything started by hand      `python3 workers/payout_worker.py` in a shell
                                  has no pid file, so there is nothing to signal
                                  by pid. It is DETECTED -- the orphan scan
                                  reports it, with its pid, and never signals it.
                                  Killing a process this process did not start is
                                  not a reporting decision (rule 16).

So the honest claim, and it is the one the page makes: this stops the three
processes that act on the database on a timer, proves each one is gone, and
names anything still alive that it did not stop. It does not stop the web
server, and it says which it is.

=============================================================================
WHY THERE IS A LOCK AND WHY IT IS THE MECHANISM
=============================================================================

gunicorn.conf.py runs TWO sync workers, and either can serve a POST. Without a
lock, two operators -- or one operator and one double-click, or one page with a
retry -- produce two concurrent starts, each of which reads "no pid file", each
of which spawns three workers, and the second of which overwrites the first's
pid files. What is left is three supervised workers and three ORPHANS with no
pid file naming them: two payout workers polling one database, which CLAUDE.md's
own record says produced "2 sends, 1 swap_id, 2 broadcast rows, on-chain and
final".

start_worker()'s own already-running check cannot close that window, and it does
not claim to: it reads the pid file and then spawns, which is a check-then-act
across two processes. The flock is the mechanism instead -- the kernel decides,
exactly once, which process may proceed -- and it is held across the WHOLE
sequence (spawn, settle, confirm), not just the read.

STOP TAKES THE SAME LOCK, for a reason f702d5e measured an hour before this was
written: a stop racing a start reads an empty /proc/<pid>/cmdline 38% of the
time, because a process between fork and exec has none, and the guard that
decides whether a pid is still ours then has to answer without evidence.
Serializing the two removes the race rather than handling it.

flock AND NOT A PID FILE, and the reasoning is swap_terminal_desktop.py's,
measured there: the kernel releases an flock when the holder dies, so there is
no stale case to interpret, where a pid file after a SIGKILL still names a dead
pid and needs a liveness and an identity check of its own.

NOT swap_terminal_desktop.acquire_single_instance_lock(), though it is the same
mechanism, and the difference is the point (rule 8): that one refuses by raising
SystemExit, which inside a gunicorn worker kills the worker that was about to
answer. A web request must come back with a sentence instead. The lock FILE is
also deliberately a different one -- a start must not be refused because a
launcher window is open.

=============================================================================
WHY NONE OF THIS IS SQL (rule 20 asks the question, so it gets an answer)
=============================================================================

Rule 20 says SQL wins unless you can say why not. Here is why not: every
decision in this module is about PROCESSES -- send a signal, ask the kernel
whether a pid still exists, take a lock the kernel releases on death, read
/proc/net/tcp. SQLite cannot express a kill, cannot be asked whether pid 3998319
is alive, and cannot hold a lock that survives the holder being SIGKILLed. A
table recording what we BELIEVE is running is exactly the stale pid file that
rule 13's incident turned on. The authority for "is this process alive" is the
kernel, it is read at the moment the question is asked, and nothing is cached.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import os
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import supervisor
from config import Config
from loopback import (
    LOOPBACK_HOSTS,
    default_gateway,
    host_of,
    is_loopback_host,
    listening_addresses,
)
from microfortnights import format_duration
from services.admin_view import worker_stopped_consequence

# The lock every control action takes, in the same directory as the WORKER PID
# FILES -- supervisor.DEFAULT_RUN_DIR -- so an operator looking for "what is
# holding what" finds the lock beside the pid files it serializes.
#
# THAT IS NOT WHERE launcher.lock IS, and this comment said it was until the paths
# were printed instead of assumed (rule 17):
#
#     supervisor.DEFAULT_RUN_DIR   /home/user/swap_terminal/swap_terminal/runtime
#     swap_terminal_desktop.RUNTIME    /home/user/swap_terminal/runtime
#
# Two `runtime/` directories, one level apart, because supervisor.py computes
# BASE_DIR from its own file (which lives in swap_terminal/) and
# swap_terminal_desktop.py computes REPO_ROOT from its own (which lives at the
# repository root). An operator told the locks are in one place would look in one
# of the two and find half of them. The divergence is NOT fixed here: moving
# either directory moves live pid files out from under a running launcher, which
# is armed state and the operator's call (rule 16). It is reported instead, and
# this comment now names both paths so the next reader does not have to find out.
CONTROL_LOCK_NAME = "kill_switch.lock"

# Bytes of the holder record read back when the lock is busy. The record is one
# short line (pid, action, time), so this is generous; it is bounded at all
# because a lock file is a file and a file can contain anything.
_HOLDER_RECORD_BYTES = 512

# How long SIGTERM gets before SIGKILL, for a stop driven from the page. Seconds,
# because it is passed to supervisor's monotonic arithmetic -- an interface, not a
# report (rule 6), and the figure is converted on the way out for display.
#
# Shorter than supervisor.DEFAULT_GRACE_SECONDS (10.0) ON PURPOSE, and this is the
# one number here that is a judgment rather than a measurement: a browser request
# that takes 30s to come back is rule 14's blinking cursor, and three workers at
# 10s each is 30s in the worst case. 5s each caps the worst case at 15s, which is
# inside gunicorn.conf.py's 60s worker timeout with room for the orphan scan. The
# workers' own loops sleep in 30-60s cycles and handle SIGTERM at the top of a
# cycle, so a worker mid-sleep exits immediately; 5s is for one mid-RPC.
WEB_GRACE_SECONDS = 5.0

#: What a START arms, per worker. The inverse of
#: admin_view.WORKER_STOPPED_CONSEQUENCES, which says what a STOPPED worker costs,
#: and the two are deliberately both present: the page that offers both buttons has
#: to say what each one does, and "what stopping costs" is not the answer to "what
#: does starting do". The payout line is the whole reason the start button needs a
#: ceremony that the stop button does not.
WORKER_ARMS = {
    "deposit_watcher": (
        "polls each configured chain for deposits and CREDITS the swaps it finds. It moves no "
        "funds itself, but a credited swap is what the payout worker then pays."
    ),
    "payout_worker": (
        "*** BROADCASTS PAYOUTS. *** This is the process that signs and sends. On a database "
        "pointed at a funded mainnet wallet, starting it begins paying customers within one "
        "poll interval, and a broadcast transaction cannot be recalled."
    ),
    "reconcile_worker": (
        # "balance rows" rather than "inventory", 2026-10-03, for the reason
        # db.py's wallet_inventory DDL now carries at length: what this worker
        # refreshes is `getbalance` against the whole wallet each endpoint serves,
        # not a measure of coins committed to the desk. The two surfaces that
        # RENDER it say the same word (services/admin_view.py and
        # templates/admin.html), and a spawn warning that called it inventory
        # while the page called it a balance would be one process using two names
        # for one number.
        "refreshes the hot-wallet balance rows and the reconciliation rows. Read-only against every "
        "chain; it sends nothing."
    ),
}

# The verdict vocabulary, and every member is a DIFFERENT WORD because rule 13's
# fourth bullet is that "did nothing" must not render like "did work". These are
# the `data-verdict` values the template keys off and a test asserts are distinct.
#
# The ones that are not an action at all:
VERDICT_REFUSED = "refused"
VERDICT_IN_FLIGHT = "another-action-in-flight"
VERDICT_NOT_ACKNOWLEDGED = "warning-not-acknowledged"
# Stop:
VERDICT_STOPPED = "stopped-and-proven"
VERDICT_NOTHING_RUNNING = "nothing-was-running"
VERDICT_NOT_PROVEN = "stop-not-proven"
# Start:
VERDICT_STARTED = "started"
VERDICT_ALREADY_RUNNING = "already-running"
VERDICT_PARTLY_STARTED = "some-started-some-already-running"
VERDICT_DIED = "spawned-and-died"
VERDICT_ORPHAN_BLOCKED = "refused-an-orphan-is-already-polling"

ALL_VERDICTS = (
    VERDICT_REFUSED,
    VERDICT_IN_FLIGHT,
    VERDICT_NOT_ACKNOWLEDGED,
    VERDICT_STOPPED,
    VERDICT_NOTHING_RUNNING,
    VERDICT_NOT_PROVEN,
    VERDICT_STARTED,
    VERDICT_ALREADY_RUNNING,
    VERDICT_PARTLY_STARTED,
    VERDICT_DIED,
    VERDICT_ORPHAN_BLOCKED,
)

#: The keys EVERY result dict carries, whatever happened, so the template reads
#: them without a `default` filter. A stop adds `grace`, a start adds `settle`, and
#: those two are the only differences -- tests/test_kill_switch.py asserts this set
#: is present in all three of stop_everything(), start_everything() and
#: refusal_result() rather than taking a docstring's word for it.
#:
#: A TEMPLATE THAT TOLERATES A MISSING KEY IS THE ONE THAT RENDERS A BLANK WHEN A
#: KEY IS MISSPELLED, which is rule 14's blank gap with nothing to notice it by.
RESULT_CORE_KEYS = frozenset(
    {
        "action",
        "verdict",
        "headline",
        "glyph",
        "refusals",
        "workers",
        "orphans",
        "orphan_count",
        "proof_established",
        "spawn_warning",
        "counts",
        "elapsed",
    }
)

# The headline sentence per verdict. One table rather than a chain of `if`s in the
# template, so the page and the JSON cannot come to disagree about what happened
# (rule 8), and so a reader can see all ten readings side by side and check that no
# two of them could be mistaken for each other.
VERDICT_HEADLINES = {
    VERDICT_REFUSED: "REFUSED. Nothing was started and nothing was stopped.",
    VERDICT_IN_FLIGHT: (
        "REFUSED: another start or stop is in flight right now. Nothing was started and nothing "
        "was stopped by THIS request -- the other one is still going."
    ),
    VERDICT_NOT_ACKNOWLEDGED: (
        "REFUSED: the warning below was not acknowledged, or it has CHANGED since the page you "
        "clicked on was rendered. Nothing was spawned. Read it again and resubmit."
    ),
    VERDICT_STOPPED: (
        "STOPPED, and proven: every pid signaled is now absent, and the /proc scan found nothing "
        "else alive."
    ),
    VERDICT_NOTHING_RUNNING: (
        "NOTHING WAS RUNNING. No signal was sent to anything, because there was nothing to send "
        "one to -- and the /proc scan confirms nothing is alive under another pid."
    ),
    VERDICT_NOT_PROVEN: (
        "*** NOT PROVEN. *** Something is still alive, or absence could not be established. Read "
        "the rows below before believing this desk is stopped."
    ),
    VERDICT_STARTED: "STARTED. Every worker was spawned and was still running when the OS was asked again.",
    VERDICT_ALREADY_RUNNING: (
        "ALREADY RUNNING. Nothing was spawned, because every worker was already up -- this request "
        "changed nothing."
    ),
    VERDICT_PARTLY_STARTED: (
        "PARTLY STARTED. Some workers were spawned and some were already running; the rows below "
        "say which is which."
    ),
    VERDICT_DIED: (
        "*** A WORKER DIED THE INSTANT IT WAS SPAWNED. *** It is NOT polling. Its pid file has been "
        "removed and the last line of its log is below."
    ),
    VERDICT_ORPHAN_BLOCKED: (
        "REFUSED, AND NOTHING WAS SPAWNED: a worker process is ALREADY polling that no pid file "
        "accounts for. Starting would have given you two of it. Deal with the pid named below -- at "
        "a shell, because this page will not signal a process it cannot prove is ours -- then start."
    ),
}

#: The glyph beside each verdict, because COLOR IS NEVER THE ONLY CHANNEL. This
#: operator reads these pages by pasting them back -- a plain-text copy carries no
#: CSS at all, and two defects were caught that way on 2026-10-02 -- and roughly
#: 8% of men cannot separate the red from the green. So each verdict carries a
#: GLYPH and a WORD as well as a class: take the color away and `*` beside "NOT
#: PROVEN" still says it, take the glyph away and the headline still does.
#:
#: THREE SHAPES AND NOT TWO, deliberately. A refusal is not a failure: "I would
#: not do this" and "I tried and cannot prove it worked" are different facts with
#: different responses, and rendering both as a cross would undo in typography the
#: distinction the verdict vocabulary exists to preserve.
VERDICT_GLYPHS = {
    VERDICT_REFUSED: "\u2298",             # circle with a slash -- refused, nothing attempted
    VERDICT_IN_FLIGHT: "\u2298",
    VERDICT_NOT_ACKNOWLEDGED: "\u2298",
    VERDICT_ORPHAN_BLOCKED: "\u2298",
    VERDICT_STOPPED: "\u2713",             # check -- it happened and it was proven
    VERDICT_STARTED: "\u2713",
    VERDICT_NOTHING_RUNNING: "\u25cb",     # hollow circle -- nothing to do, nothing done
    VERDICT_ALREADY_RUNNING: "\u25cb",
    VERDICT_PARTLY_STARTED: "\u25d0",      # half-filled -- some of each
    VERDICT_NOT_PROVEN: "*",                # the one that must survive a grayscale paste
    VERDICT_DIED: "*",
}


class CaseInsensitiveHeaders(Protocol):
    """A header container whose `get` folds case. WHAT `headers` HAS TO BE.

    `Mapping[str, str]` is what this field used to say, and it was wrong in both
    directions at once. Measured 2026-10-09 against the installed werkzeug:

        isinstance(Headers([...]), Mapping)   False
        list(iter(Headers([...])))            [('X-Forwarded-For', '10.0.0.1'), ...]

    A werkzeug `Headers` is a list of PAIRS with case-insensitive lookup, not a
    Mapping, and iterating one yields tuples rather than keys -- so the annotation
    refused the only object that is ever actually passed here
    (`routes/kill_switch._facts()` hands over `request.headers`) while promising
    `__getitem__`, `__len__` and key iteration that none of this file uses. Three
    `.get()` calls in refuse_cross_origin() are the entire use, which is why this
    names one method and nothing else.

    =====================================================================
    A PLAIN dict DOES NOT MEET THIS CONTRACT -- AND NO CHECKER CAN SAY SO.
    `dict(request.headers)` IS THE SECURITY REGRESSION THIS WARNS ABOUT
    =====================================================================

    `dict(request.headers)` is the obvious way to make a `Headers` fit a Mapping
    annotation, it type-checks, every existing test stays green -- and it silently
    disables the forwarding-header refusal below. HTTP header names are
    case-insensitive on the wire and HTTP/2 lowercases every one of them. Measured,
    same day, on the real container:

        Headers([("x-forwarded-for", "10.0.0.1")]).get("X-Forwarded-For")  '10.0.0.1'
        dict(Headers([("x-forwarded-for", "10.0.0.1")])).get("X-Forwarded-For")  None

    refuse_cross_origin() looks the four forwarding headers up by their canonical
    spelling. Through a `Headers` an HTTP/2 request is refused; through a dict of
    the same request it is waved past, because the dict is keyed by whatever the
    wire happened to send. That is a proxied caller reaching the one surface in
    this application that can spawn the payout worker, with the guard still
    present in the source and answering [] -- rule 13's "skipped plus success",
    one layer down.

    AND A CHECKER CANNOT HOLD THAT LINE, which is stated here because the
    alternative is a reader trusting it to. Measured against pyright 1.1.414: a
    `dict[str, str]` SATISFIES this Protocol. Case folding lives in the body of
    `get`, and structural typing sees only its signature -- `dict.get`'s first
    overload is exactly `(key, /) -> _VT | None`. Two shapes were tried that do
    refuse a dict and both were rejected for being dishonest rather than
    expressive: naming the parameter keyword-callable (`get(self, key: str)`)
    refuses a dict only because typeshed makes `dict.get`'s key positional-only,
    which is incidental to case and contradicts the positional call sites; adding
    `getlist` refuses it only by naming a method this file never calls. A third,
    declaring the `default` parameter, refuses `Headers` as well.

    Either dict-refusing shape also costs more than it buys, measured rather than
    guessed: the `getlist` variant produces SEVEN errors where there was one --
    the `headers` field's own `field(default_factory=dict)`, plus six
    `RequestFacts(headers={...})` seed sites in tests/test_kill_switch.py. Those
    seeds are right (rule 10: the decision is callable with no socket and no
    server), so a Protocol that refuses them would be refusing the correct caller
    to catch a hypothetical one.

    So the enforcement is this docstring and the suite, not the annotation. What
    the annotation DOES buy is that the type is now true -- `Headers` satisfies it,
    a Mapping was a claim about an object werkzeug does not provide -- and that the
    promise shrinks from ten members a caller could reach for (`__getitem__`,
    `__iter__`, `__len__`, `__contains__`, `keys`, `items`, `values`, `get`, and
    equality both ways) to the one that is used, so the next reader can see that a
    single case-folding `get` is the whole contract.

    The `field(default_factory=dict)` below is a dict on purpose and is not the
    hazard above: an EMPTY container answers None to every lookup at every casing,
    so there is no case for it to fold, and a seeded-facts test says what it means
    to send no headers at all.
    """

    def get(self, name: str, /) -> str | None:
        """One header by name, case-insensitively, or None. The only method used here.

        POSITIONAL-ONLY because all three call sites are positional, which makes
        this the truer claim as well as the looser one: `werkzeug.Headers.get` names
        the parameter `key` and a keyword-callable declaration would pin a spelling
        nothing depends on.
        """
        ...


#: Files whose mere existence means a container runtime created this filesystem.
#:
#: `/.dockerenv` is written by Docker itself. `/run/.containerenv` is Podman's. Both
#: are presence-only -- their CONTENTS are not read and not required to be anything --
#: so this is a check somebody can verify by looking rather than by trusting a parser.
#:
#: NOT A SECURITY CONTROL AND IT MUST NEVER BECOME ONE. This changes the WORDING of a
#: refusal and never the verdict, which is the only reason a presence check is good
#: enough: a missing file costs an operator a less specific sentence, and a planted one
#: buys an attacker a differently worded refusal. If this ever gates whether the
#: buttons render, it needs to be something an attacker cannot create.
_CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))

#: The environment variable carrying the HOST side of a container's port publish.
#: Named like operator_panel.MAY_STOP_VARIABLE and for the same reason: the decision
#: is made outside the browser, by somebody editing a file in a shell, and cannot be
#: made by anything that merely reaches the port.
PUBLISH_HOST_VARIABLE = "SWAP_TERMINAL_PUBLISH_HOST"

#: What is known about the publish mapping. Four outcomes, and the two that are not
#: "it is loopback" are different reasons rather than one -- an operator who set the
#: variable to the wrong thing and an operator who never set it need different
#: sentences, which is the distinction `bech32_prefix_status()` and `sync_verdict()`
#: are both shaped around (chains/daemon_network.py).
PUBLISH_LOOPBACK = "publish-loopback"
PUBLISH_EXPOSED = "publish-exposed"
PUBLISH_NOT_DECLARED = "publish-not-declared"
PUBLISH_NO_MAPPING = "publish-no-mapping"

#: Every outcome, so a reader and a test have one list rather than four literals.
PUBLISH_VERDICTS = (PUBLISH_LOOPBACK, PUBLISH_EXPOSED, PUBLISH_NOT_DECLARED, PUBLISH_NO_MAPPING)


def publish_verdict(facts: RequestFacts) -> str:
    """What is known about the HOST side of this container's port publish.

    WHY THIS EXISTS AT ALL, and the operator made the call on 2026-10-10 after a
    session in which /admin's controls refused on the only deployment they have.
    Inside a container `SWAP_TERMINAL_HOST` must be 0.0.0.0 -- 127.0.0.1 there is the
    CONTAINER's loopback and the published port then reaches nothing, measured
    2026-10-05 and recorded in docker-compose.web.yml beside the port it publishes.
    So the socket scan sees 0.0.0.0, correctly, and refuse_off_box() refused -- on a
    deployment whose publish is `127.0.0.1:5100:5000` and is therefore private.

    THE PUBLISH IS NOT VISIBLE FROM IN HERE. `ports:` maps at the docker proxy, on
    the host; nothing in the container's /proc, environment or socket table
    distinguishes `127.0.0.1:5100:5000` from a bare `5100:5000`. That is why this
    reads a DECLARATION rather than measuring anything, and why the name says
    PUBLISH_HOST rather than anything implying evidence.

    WHAT MAKES THE DECLARATION HONEST IS THE COUPLING, not the variable. The compose
    file interpolates ONE token into both sides:

        ports:        "${SWAP_TERMINAL_PUBLISH_HOST:-127.0.0.1}:${...PORT:-5100}:5000"
        environment:  SWAP_TERMINAL_PUBLISH_HOST: "${SWAP_TERMINAL_PUBLISH_HOST:-127.0.0.1}"

    so the value this function reads IS the host that docker bound, and changing the
    publish changes the declaration in the same edit because they are the same edit.
    A separate boolean -- PUBLISH_IS_LOOPBACK=1 -- would have been two copies of one
    fact, agreeing on the day it was written and drifting after (rule 8), and the
    drift would have been invisible in exactly the direction that matters.

    IT CAN STILL BE WRONG AND THE LIMIT IS NAMED RATHER THAN GLOSSED: `docker run -p
    0.0.0.0:5100:5000` by hand, or a further -f file that overrides `ports:` and not
    `environment:`, breaks the coupling and nothing here can tell. What the coupling
    buys is that the ORDINARY path cannot lie.

    NOT A CONTAINER MEANS NO MAPPING, AND THAT IS THE IMPORTANT BRANCH. On a host
    there is no publish: the bind IS the reachability, and a host process that
    exported this variable would otherwise hand itself a pass. So the variable is
    read only when a container marker is present, and `in_a_container()` is
    presence-only (see its docstring) -- which means this is the one place that flag
    does change a verdict, and it changes it only in the direction where a publish
    mapping exists to be declared.

    ABSENT AND EMPTY BOTH MEAN NOT DECLARED. `os.getenv` returns "" for a variable
    that is set and empty, and "" is not a host; refuse_off_box() already carries
    that measurement for SWAP_TERMINAL_HOST, where an empty value makes gunicorn bind
    ALL interfaces. Fail closed on both.
    """
    if not facts.in_container:
        return PUBLISH_NO_MAPPING
    raw = facts.env.get(PUBLISH_HOST_VARIABLE)
    if raw is None or not raw.strip():
        return PUBLISH_NOT_DECLARED
    return PUBLISH_LOOPBACK if raw.strip() in LOOPBACK_HOSTS else PUBLISH_EXPOSED


def in_a_container() -> bool:
    """Is this process inside a container? Presence of a runtime's own marker file.

    Deliberately NOT a cgroup parse. /proc/1/cgroup has had three formats across
    cgroup v1, v2 and rootless podman, and a regex over it is a thing that silently
    stops matching -- which here would silently restore the wrong remedy. A missing
    marker file degrades to the host wording, which is the safe direction: it tells a
    container operator something slightly less specific, where the old behavior told
    them to make a change that breaks the page.
    """
    return any(marker.exists() for marker in _CONTAINER_MARKERS)


@dataclass(frozen=True)
class RequestFacts:
    """The four things about a request that decide whether it may operate the switch.

    ONE OBJECT RATHER THAN FOUR PARAMETERS THREADED THROUGH FIVE FUNCTIONS, and
    the reason is not argument count: these four travel together or the decision
    is made on a subset of them. The failure that shape invites is a caller that
    passes the headers and forgets the socket scan, which refuses nothing and
    renders the buttons -- a check present in the code and absent from the answer.

    `observed()` is what a request handler calls and it fills in the two the
    handler has no business knowing about; the constructor is what a test calls,
    with every field seeded, which is how the refusal is tested without a socket
    and without a server (rule 10).
    """

    headers: CaseInsensitiveHeaders = field(default_factory=dict)
    remote_addr: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    #: None means "could not look". See loopback.listening_addresses(): the
    #: distinction from an empty set is the whole reason it returns None at all,
    #: and a control surface must refuse on it.
    listening: set[str] | None = None
    #: Is this process inside a container? It changes NOTHING about the verdict and
    #: everything about whether the refusal's remedy is correct.
    #:
    #: MEASURED ON THE OPERATOR'S HOST 2026-10-10 and the remedy was actively
    #: harmful. The panel refused with "This server is listening on 0.0.0.0 ... Set
    #: SWAP_TERMINAL_HOST=127.0.0.1 and restart the server." That deployment is a
    #: CONTAINER, where 0.0.0.0 is the only bind that works -- and
    #: docker-compose.web.yml:102 carries the measurement in a comment one line
    #: below the port it publishes: "127.0.0.1 INSIDE A CONTAINER IS THE CONTAINER,
    #: AND THAT IS WHY THE UI CAME UP EMPTY. Measured on the operator's host
    #: 2026-10-05". So the remedy printed by a security refusal was the change the
    #: repo had already recorded as breaking the page.
    #:
    #: THE VERDICT DOES NOT CHANGE, AND THAT IS DELIBERATE. From inside a container
    #: the PUBLISH MAPPING IS NOT VISIBLE: `ports: 127.0.0.1:5100:5000` restricts
    #: reachability at the docker proxy on the host, and nothing the container can
    #: read says so. So "0.0.0.0 inside a loopback-published container" and
    #: "0.0.0.0 on a host, open to the LAN" are indistinguishable from here, and a
    #: control surface that arms a payout worker must refuse the pair. What this
    #: flag buys is an honest explanation instead of a wrong instruction.
    in_container: bool = False
    #: What this container's default route points at, read from /proc/net/route by
    #: loopback.default_gateway(). None on a host, or when the table cannot be read.
    #:
    #: IT IS A FACT ABOUT THIS PROCESS, NOT ABOUT THE REQUEST, which is the only reason
    #: it may be used in a refusal at all: every other peer signal is something a caller
    #: writes. See the peer branch in refuse_cross_origin() and default_gateway()'s own
    #: docstring for the measurement that put it there.
    gateway: str | None = None

    @classmethod
    def observed(cls, headers: CaseInsensitiveHeaders, remote_addr: str | None) -> RequestFacts:
        """The facts as the operating system and this process's environment report them.

        The socket scan happens HERE, once per request, rather than inside each
        decision function -- so the panel and the endpoint that guards it cannot
        read two different answers from two reads a few milliseconds apart.
        """
        return cls(
            headers=headers,
            remote_addr=remote_addr,
            env=os.environ,
            listening=listening_addresses(),
            in_container=in_a_container(),
            gateway=default_gateway(),
        )


def lock_path(run_dir: Path) -> Path:
    """Where the single-flight lock lives. One definition, so a test and the route agree."""
    return run_dir / CONTROL_LOCK_NAME


@contextlib.contextmanager
def single_flight(path: Path) -> Iterator[str | None]:
    """Hold an exclusive flock for the block, or yield the holder's record instead.

    Yields None when the lock is OURS -- the block may act -- and the holder's
    recorded line when it is not. Yielding rather than raising is deliberate: this
    runs inside a request, and the caller has to return a sentence rather than
    die (see the module header on why
    swap_terminal_desktop.acquire_single_instance_lock() is not reused).

    OPENED O_RDWR|O_CREAT AND TRUNCATED ONLY AFTER THE LOCK IS OURS. That is not
    style, it is a defect swap_terminal_desktop.py measured and recorded: the
    natural spelling, `open(path, "w")`, truncates BEFORE the flock fails, so the
    refused caller destroys the holder's record and then reports a holder of "".

    THE RECORD IS WRITTEN FOR THE OTHER PROCESS TO READ, which is why it carries
    the pid and the wall clock rather than being empty. A refusal that cannot say
    who holds the lock sends the operator looking for a process with no name.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in (errno.EACCES, errno.EAGAIN):
                # NOT a blind except: errno is checked, and anything that is not
                # "somebody else holds it" is a real failure that must not be
                # reported as contention.
                raise
            holder = os.read(fd, _HOLDER_RECORD_BYTES).decode("utf-8", "replace").strip()
            yield holder or "(the lock file is empty -- the holder had not written its record yet)"
            return
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} held_since={time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n".encode())
        yield None
    finally:
        # Closing the descriptor releases the flock. No explicit LOCK_UN, and no
        # unlink: removing the file would let a second process create a NEW file
        # and lock that one instead, which is a lock that does not lock.
        os.close(fd)


def acknowledgment_token(warning: str) -> str:
    """A fingerprint of the exact warning text that was shown.

    NOT A SECURITY TOKEN AND NOT A CSRF DEFENSE. It is printed on the page, so
    anyone who can render the page can echo it; the defense against a web page
    driving these controls is refuse_cross_origin() below, and the defense
    against the network is the loopback refusal. Mistaking this for either would
    be worse than not having it.

    WHAT IT IS FOR: binding the acknowledgment to the warning that was READ.
    supervisor.spawn_warning() is not a constant -- it says "a payout worker CAN
    broadcast on GRC, LTC" or "CANNOT BROADCAST ANYTHING" or "will refuse because
    the passphrase is unset", derived from the live configuration. So a form
    rendered an hour ago may carry an acknowledgment of a warning that is no
    longer true: the operator repointed GRC_RPC_PORT at mainnet in between, and
    the sentence they actually read said testnet. Fingerprinting the text means
    that stale submission is refused and the current warning is re-rendered,
    which is the only way "it was shown before the spawn" can be a fact rather
    than a hope.

    sha256 truncated to 12 hex characters, which is a collision risk of nothing
    at all for a set of possible warnings this size, and short enough to appear
    in a hidden field without obscuring the form in a view-source.
    """
    return hashlib.sha256(warning.encode("utf-8")).hexdigest()[:12]


def refuse_off_box(facts: RequestFacts) -> list[str]:
    """Why this server is not provably loopback-only, or [] if it is.

    THE CONSTRAINT THIS IMPLEMENTS, and it is the one the brief would not accept
    a workaround on: a kill switch reachable off-box on an unauthenticated port is
    a different product. /admin's own banner already says "anyone who can reach
    this port can read every swap id, deposit address, payout address and
    balance" -- read-only, that is a disclosure problem; with a button on it, the
    same sentence means anyone who can reach the port can stop the desk mid-payout
    or arm one.

    TWO SIGNALS, AND THEY ARE NOT REDUNDANT:

      the listening sockets   asked of the kernel, filtered to this process's own
          socket inodes. This is the evidence (loopback.listening_addresses()'s
          docstring says what the alternatives can and cannot establish). None
          means "could not look", and a control surface MUST refuse on it: a
          platform where the bind cannot be established is a platform where this
          page cannot know whether it is private.
      SWAP_TERMINAL_HOST      what both servers read to build the bind. It is
          intent rather than evidence -- `gunicorn -b` on the command line
          overrides it -- so it is checked IN ADDITION, never instead. It catches
          the case the socket scan cannot: a listener on 127.0.0.1 in a
          deployment whose operator believes they published the API, where the
          disagreement itself is the thing worth refusing on.

    An EMPTY SWAP_TERMINAL_HOST is refused rather than treated as the default,
    because gunicorn.conf.py builds `bind = f"{os.getenv(...,'127.0.0.1')}:..."`
    and os.getenv returns "" for a variable that is set and empty -- so an empty
    value binds ALL INTERFACES under gunicorn while app.py's development server
    reads it as loopback. The two servers disagree; refusing is the only answer
    that is correct under both.
    """
    refusals: list[str] = []
    # READ ONCE, so the two branches below cannot disagree -- they are the same
    # question asked of the bind and of the intent, and a publish that is loopback
    # answers both. NOT consulted for the two NOT-ESTABLISHED branches: a declared
    # publish says who can reach the port, not what this process is bound to, and a
    # control surface that cannot establish its own bind must refuse regardless.
    publish = publish_verdict(facts)

    if facts.listening is None:
        refusals.append(
            "Could not establish what this server is bound to: /proc/self/fd or /proc/net/tcp "
            "could not be read, so whether this port is reachable from off this machine is NOT "
            "KNOWN. These controls refuse when the answer is unknown rather than assuming the "
            "safe one."
        )
    else:
        exposed = sorted(host for host in facts.listening if not is_loopback_host(host))
        if exposed and publish != PUBLISH_LOOPBACK:
            refusals.append(
                f"This server is listening on {', '.join(exposed)}, and nothing on this surface "
                f"authenticates a caller -- so a start or stop button here would let anyone who "
                f"can reach the port stop the desk or arm a payout worker. "
                + _remedy_for(publish)
            )
        elif not facts.listening:
            refusals.append(
                "This process holds no listening TCP socket, so the bind could not be "
                "established from the kernel. Under gunicorn every worker inherits the listening "
                "socket, so this should be impossible -- it is the normal state inside a test "
                "harness, and these controls refuse rather than guess."
            )

    raw = facts.env.get("SWAP_TERMINAL_HOST")
    if raw is not None:
        host = raw.strip()
        # THE SAME RELAXATION, AND IT HAS TO BE THE SAME OR THE CHANGE DOES NOTHING.
        # Inside a container SWAP_TERMINAL_HOST is 0.0.0.0 *by design* -- compose sets
        # it, because the alternative binds the container's own loopback and the
        # published port reaches nothing. So this branch fired on exactly the
        # deployment the socket branch fired on, for the same reason, and relaxing one
        # without the other would have left the controls refused with the reason
        # changed. Both are gated on the publish, once, read above.
        if host not in LOOPBACK_HOSTS and publish != PUBLISH_LOOPBACK:
            # "CONFIGURED TO BE REACHABLE OFF-BOX" IS NOT TRUE OF A CONTAINER and the
            # sentence used to say it flatly. Inside one, 0.0.0.0 is what reaches the
            # docker proxy and says nothing about who can reach THAT.
            reachable = (
                "so this process accepts from any interface it can see. Inside a container that "
                "is the only bind that works and says NOTHING about who can reach the published "
                "port"
                if facts.in_container
                else "so this deployment was configured to be reachable off-box whatever the "
                "socket table currently says"
            )
            refusals.append(
                f"SWAP_TERMINAL_HOST is set to {raw!r}, which is not one of "
                f"{', '.join(sorted(LOOPBACK_HOSTS))}. That is what gunicorn.conf.py and app.py "
                f"both read to build the bind, {reachable}. (An empty value is refused "
                f"too: os.getenv returns '' rather than the default, and gunicorn binds an empty "
                f"host on ALL interfaces.)"
            )
    return refusals


#: WHAT TO DO ABOUT A NON-LOOPBACK BIND, per deployment. THREE sentences since
#: 2026-10-10, where there were two: a host, a container that never declared its
#: publish, and a container whose declared publish is not loopback. Three, because the
#: right instruction differs in each and the wrong one breaks the page -- which is not
#: hypothetical here, it is what this block was last rewritten for.
#:
#: The host remedy is the original and is correct there: gunicorn binds what
#: SWAP_TERMINAL_HOST says, so setting it to loopback makes the deployment private.
#:
#: NEITHER CONTAINER REMEDY MAY BE "SET IT TO LOOPBACK", because that binds the
#: CONTAINER's loopback and the published port then reaches nothing --
#: docker-compose.web.yml records exactly that outcome, measured 2026-10-05, and a
#: security refusal printed that instruction on the operator's host. What makes a
#: container private is the PUBLISH, on the host, which the container cannot see.
_HOST_REMEDY = (
    "Set SWAP_TERMINAL_HOST=127.0.0.1 and restart the server."
)

#: THE CONTAINER FACTS, IN ONE PLACE, because both container remedies need all three
#: and they were one string until the verdict stopped being the same for every
#: container. Splitting the tail off and sharing the preamble is rule 8's answer to
#: "two messages that agree about most of it"; keeping two full copies is how they
#: drift. Every clause is load-bearing and two are measurements:
#:
#:   do NOT set the loopback bind   measured 2026-10-05, the UI came up empty.
#:   the PUBLISH is what decides    it maps on the host, so that is the thing to read.
#:   how to read it                 `docker compose port web 5000`. A remedy this
#:                                  process cannot carry out has to say where it CAN
#:                                  be carried out (rule 14).
_CONTAINER_REMEDY = (
    "THIS PROCESS IS IN A CONTAINER, so do NOT set SWAP_TERMINAL_HOST=127.0.0.1 -- inside a "
    "container that binds the CONTAINER's loopback and the published port then reaches nothing "
    "(docker-compose.web.yml records that outcome, measured 2026-10-05: the UI came up empty). "
    "0.0.0.0 is the correct bind here. What decides whether this is private is the PUBLISH on "
    "the host, which this process CANNOT SEE: `ports: 127.0.0.1:5100:5000` restricts it to the "
    "host's loopback, a bare `5100:5000` does not. Check it with `docker compose port web 5000` "
    "or by reading the compose ports line."
)

#: The tail for a container whose publish was never declared, and it is the half that
#: is NEW. Until 2026-10-10 the only honest tail was "these controls still refuse,
#: because from in here those two are indistinguishable and this surface arms a payout
#: worker" -- true, and nothing an operator could act on. Now there is something to
#: do, so the sentence says it instead.
_PUBLISH_NOT_DECLARED_REMEDY = _CONTAINER_REMEDY + (
    f" NOTHING HERE DECLARED IT, which is why these controls are refusing. {PUBLISH_HOST_VARIABLE} "
    "carries the host side of that publish, and docker-compose.web.yml already sets it from the "
    "SAME ${} token it builds the ports line from -- so a stack brought up with `swapterm up` "
    "carries it and cannot disagree with its own publish. A deployment that reaches this image "
    "another way (a hand-written `docker run -p`, a further -f file overriding `ports:` and not "
    "`environment:`) has to declare it deliberately, which is the point: it is an assertion about "
    "the host, made in a file, and not something a browser request can set."
)

#: And the tail for a container that DID declare a publish, and declared one that is
#: not loopback. Nothing is ambiguous about this one: it was declared, and what it
#: says is "anyone who can route to this host can reach this port."
_PUBLISH_EXPOSED_REMEDY = _CONTAINER_REMEDY + (
    f" {PUBLISH_HOST_VARIABLE} DECLARES A NON-LOOPBACK PUBLISH, so the refusal above is not a "
    "false positive -- it is the configuration. Publish on 127.0.0.1 (the default in "
    "docker-compose.web.yml) and bring the stack back up, or put an authenticating proxy in front "
    "of this port. Nothing on this surface authenticates a caller, so there is no third option "
    "that keeps the buttons."
)

#: Remedy per verdict, TOTAL over every verdict that can reach _remedy_for().
#: PUBLISH_LOOPBACK is absent on purpose and a test asserts the other three are
#: present -- the completeness-asserted-map shape this repo already uses for
#: chains/daemon_network.bech32_prefix_status(), where the alternative was a renderer
#: that invented a fact about XRP for a case nobody had enumerated.
_REMEDY_FOR: dict[str, str] = {
    PUBLISH_NO_MAPPING: _HOST_REMEDY,
    PUBLISH_NOT_DECLARED: _PUBLISH_NOT_DECLARED_REMEDY,
    PUBLISH_EXPOSED: _PUBLISH_EXPOSED_REMEDY,
}


def _remedy_for(publish: str) -> str:
    """Which remedy a non-loopback bind gets, given what is known about the publish.

    A MAP PLUS ONE FUNCTION rather than a conditional chain, for rule 10's reason:
    this is the decision, and it is callable with each verdict in a test that needs no
    socket, no container and no server.

    PUBLISH_LOOPBACK NEVER REACHES THIS, because every caller checks for it before
    building a refusal -- a remedy for a refusal that did not happen is a sentence
    with no reader. It is therefore ABSENT from the map rather than mapped to
    something harmless, so a test can assert the map is total over the three that DO
    arrive, which is the check that catches a fifth verdict added without a remedy.

    THE FALLBACK IS A SENTENCE AND NOT A KeyError, and that is not belt-and-braces: a
    KeyError here is a 500 on a page that was in the middle of refusing, which turns a
    working security refusal into an outage. The completeness test is what makes the
    fallback unreachable; the fallback is what makes an unreachable case harmless.
    """
    return _REMEDY_FOR.get(publish, _CONTAINER_REMEDY)


def refuse_cross_origin(facts: RequestFacts) -> list[str]:
    """Why this request may not drive the controls, or [] if it may.

    THE HOLE THIS CLOSES IS NOT THE NETWORK, IT IS THE BROWSER. Binding loopback
    keeps the network out; it does not keep out a page the operator merely VISITS.
    A cross-origin form POST needs no CORS preflight -- the attacker cannot read
    the response, but the ACTION HAPPENS -- so without this, any page in any tab
    could POST to 127.0.0.1:5000 and stop a desk mid-payout, or start a payout
    worker, and the operator would see only workers they did not start.

    THE SAME DEFENSE operator_panel.py:refuse_a_cross_origin_post() makes for the
    regtest panel on port 8765, and the two are NOT merged, which rule 8 permits
    only if the difference is named at both sites -- it is, in both files. The
    differences are real: that one guards a JSON API and leans on a content type
    that forces a preflight, so a form POST cannot reach it at all; this one
    guards a FORM POST, because a control surface that needs JavaScript to stop
    the desk is a control surface that does nothing when a script fails to load.
    The shared parse, host_of(), is imported from loopback.py by both -- writing
    that parser twice is how the same prefix-matching vulnerability gets fixed in
    one copy and left in the other.

    THREE CHECKS, FAILING DIFFERENTLY ON PURPOSE:

      Origin    a browser sends it on every cross-origin POST. Anything that is
                not this machine is refused. A MISSING Origin is allowed, because
                curl and the tests send none and a page-driven request always
                carries one -- the check is aimed at browsers, which is where the
                threat is.
      Host      must be loopback. This is the DNS-rebinding defense: an
                attacker's domain can be made to resolve to 127.0.0.1, which
                makes their page same-origin with this one and silences the Origin
                check entirely. The Host header still carries their domain, and
                that is what gives them away.
      peer      REMOTE_ADDR must be a loopback address. This proves nothing about
                the LISTENER (a 0.0.0.0 server reached from its own host reports
                127.0.0.1, which is why refuse_off_box() exists separately) but it
                does refuse a request that provably arrived from somewhere else.
                An unknown peer is refused: in a request there is always one, so
                its absence means something is between us that we cannot see.

    A FORWARDING HEADER IS AN IMMEDIATE REFUSAL, and that is the honest handling
    of a reverse proxy rather than a hole left under it. Behind a proxy,
    REMOTE_ADDR is the proxy -- always loopback if it runs here -- so the peer
    check above silently becomes meaningless for every off-box client it forwards.
    The headers are not trusted to say who the client is, because a client can
    send them itself; their PRESENCE is taken as "this request did not come
    straight from a browser on this machine", which is enough to stop.
    """
    refusals: list[str] = []
    headers = facts.headers

    origin = (headers.get("Origin") or "").strip()
    if origin and host_of(origin) not in LOOPBACK_HOSTS:
        refusals.append(
            f"This request came from {origin}, which is not this machine. These controls stop and "
            f"start processes that move funds and nothing here authenticates a caller, so a page "
            f"you merely visited must not be able to drive them."
        )

    host = (headers.get("Host") or "").strip()
    if host and host_of(host) not in LOOPBACK_HOSTS:
        refusals.append(
            f"This request names host {host!r}. These controls answer to 127.0.0.1 only, and a "
            f"name that resolves here is how a DNS-rebinding attack makes its own page "
            f"same-origin with this one."
        )

    forwarded = [name for name in ("X-Forwarded-For", "X-Forwarded-Host", "X-Real-IP", "Forwarded") if headers.get(name)]
    if forwarded:
        refusals.append(
            f"This request carries {', '.join(forwarded)}, so it reached us through a proxy. The "
            f"peer address then belongs to the proxy and not to the caller, which makes the "
            f"loopback check below meaningless -- so these controls refuse rather than trust a "
            f"header the caller could have written."
        )

    if not facts.remote_addr:
        refusals.append(
            "This request has no peer address, so where it came from could not be established. "
            "A real request always has one."
        )
    elif not _peer_is_this_machine(facts):
        refusals.append(
            f"This request arrived from {facts.remote_addr}, which is not this machine."
            + (
                f" This container's default route points at {facts.gateway}, which WOULD be "
                f"accepted as the host -- but the publish is not declared loopback, so who can "
                f"reach the published port is unknown."
                if facts.in_container and facts.remote_addr == facts.gateway
                else ""
            )
        )
    return refusals


def _peer_is_this_machine(facts: RequestFacts) -> bool:
    """Is the caller the machine running this desk? The peer half of the control guard.

    A LOOPBACK PEER ALWAYS QUALIFIES and that is the original check, unchanged.

    THE BRIDGE GATEWAY ALSO QUALIFIES, INSIDE A CONTAINER WHOSE PUBLISH IS DECLARED
    LOOPBACK, and this is the 2026-10-10 fix. Measured off the operator's own rendered
    page:

        THESE CONTROLS ARE REFUSING. No button is rendered below.
        This request arrived from 172.18.0.1, which is not this machine.
        bind  SWAP_TERMINAL_PUBLISH_HOST: '127.0.0.1' -- declared loopback

    Read those two lines together: the BIND guard passed -- the publish is loopback, so
    refuse_off_box() was satisfied -- and the PEER check refused on 172.18.0.1, which is
    the docker bridge gateway. That is the operator's own machine, reaching the published
    port from the host side. A bridged container never sees a loopback peer from outside
    itself, so on the only deployment this repository ships -- compose, bridge, published
    port -- the controls could not be operated by anybody, ever. The operator said so five
    times ("no controls. no buttons. nothing.") and this line was the cause.

    A RECOGNITION, NOT A RELAXATION. The check's question is "is the caller the machine
    running the desk". On a bridged container the gateway IS that machine, so admitting it
    answers the question correctly rather than lowering the bar. And the comparison value
    comes from the KERNEL -- this container's routing table -- not from anything the
    caller sent, so it stays evidential where the Host header and Origin are not.

    THREE CONDITIONS, ALL REQUIRED, and each closes a hole the others do not:

      in_container        on a host there is no bridge and no reason to admit a gateway;
                          a host's default route points at its router, which is emphatically
                          not this machine.
      publish is loopback the one fact that says who can reach the published port. It is a
                          DECLARATION the container cannot verify -- publish_verdict()'s
                          own docstring says so -- but it is not one a CALLER can write,
                          which is what matters here.
      peer == gateway     and nothing else. Every OTHER container on the same bridge
                          arrives from its own 172.18.0.x, not from .1, so a sibling
                          container is still refused. That is OPEN_FINDINGS finding 4 and
                          this leaves it exactly where it was.

    A gateway of None -- unreadable routing table -- qualifies nothing: `facts.gateway ==
    facts.remote_addr` is False against a real peer address, so an unestablished fact
    refuses, which is the posture every other branch of this guard takes.
    """
    if is_loopback_host(facts.remote_addr or ""):
        return True
    return bool(
        facts.in_container
        and facts.gateway
        and facts.remote_addr == facts.gateway
        and publish_verdict(facts) == PUBLISH_LOOPBACK
    )


def control_refusals(facts: RequestFacts) -> list[str]:
    """Every reason this request may not operate the switch, in one list.

    ONE function, called by the GET that decides whether to render the buttons and
    by each POST that decides whether to act. Two implementations of "may this
    caller act" is the shape where a page offers a control the endpoint refuses,
    or worse renders a refusal over an endpoint that accepts -- and only the
    second of those is discovered by anybody using the page.

    Every input arrives in the RequestFacts, which is how the decision is callable
    with seeded values and no socket (rule 10). The route builds one per request
    with RequestFacts.observed() and hands the SAME object to the panel and to the
    guard, so the two cannot read two different answers.
    """
    return [*refuse_off_box(facts), *refuse_cross_origin(facts)]


def bind_evidence(facts: RequestFacts) -> list[str]:
    """What was established about the bind, and HOW -- printed whether or not it refused.

    Rule 14: echo the parameters that decide the answer, and never let a result
    print nothing. An operator reading a refusal has to be able to see which
    signal produced it, and an operator reading an ALLOWED panel has to be able to
    see that the question was asked at all -- a page that silently permits looks
    exactly like a page with no check in it.
    """
    raw = facts.env.get("SWAP_TERMINAL_HOST")
    if facts.listening is None:
        sockets = "NOT ESTABLISHED -- /proc could not be read, so the kernel was not asked"
    elif not facts.listening:
        sockets = "none -- this process holds no listening TCP socket"
    else:
        sockets = ", ".join(sorted(facts.listening))
    return [
        f"listening sockets (this process, from /proc/net/tcp): {sockets}",
        f"SWAP_TERMINAL_HOST: {raw!r} ({'the 127.0.0.1 default applies' if raw is None else 'set explicitly'})",
        *_publish_evidence(facts),
        "the Host header and the request's peer address are NOT treated as evidence of the bind -- "
        "a caller writes both. They are checked separately, and only to refuse.",
    ]


#: WHAT THE PUBLISH LINE SAYS, per verdict. Written out rather than assembled,
#: because the one an operator is most likely to be reading -- PUBLISH_LOOPBACK, the
#: case where the buttons WORK -- is the one that has to carry what it does not
#: prove. A page that permits an action should say what it accepted, not only what
#: it checked (rule 14), and this is the sentence that would otherwise exist only in
#: a docstring nobody reading the page can see.
_PUBLISH_EVIDENCE = {
    PUBLISH_LOOPBACK: (
        "declared loopback -- this is a DECLARATION and not a measurement: the publish maps on the "
        "HOST and no container can read it. It is the same ${} token docker-compose.web.yml builds "
        "the ports line from, so the ordinary path cannot disagree with itself; a hand-written "
        "`docker run -p` can. WHAT IT DOES NOT COVER: every other container on this compose bridge "
        "reaches this port DIRECTLY, without going through the publish at all -- that is "
        "OPEN_FINDINGS finding 4, it is not addressed by a loopback publish, and the services "
        "sharing this bridge are the ones in the -f files (abstergo, harness, icp-replica, web)."
    ),
    PUBLISH_EXPOSED: "declared NON-loopback -- the refusal above is the declared configuration, not a false positive",
    PUBLISH_NOT_DECLARED: "NOT DECLARED -- in a container, and nothing says which host address the port is published on",
    PUBLISH_NO_MAPPING: "not applicable -- no container marker, so there is no publish mapping and the bind IS the reachability",
}


def _publish_evidence(facts: RequestFacts) -> list[str]:
    """The publish line for bind_evidence(), or nothing at all on a bare host.

    SILENT ON A HOST, DELIBERATELY. `PUBLISH_NO_MAPPING` is the overwhelmingly common
    case for anyone running this outside compose, and a line saying "not applicable"
    on every render is the noise rule 14 warns trains a reader to skip the block that
    matters -- the same argument `trust_note()` makes for not printing GNOME advice at
    an LXQt operator (C43). It is printed when the variable IS set on a host, because
    then somebody set it expecting it to do something and it does not.
    """
    verdict = publish_verdict(facts)
    if verdict == PUBLISH_NO_MAPPING and facts.env.get(PUBLISH_HOST_VARIABLE) is None:
        return []
    raw = facts.env.get(PUBLISH_HOST_VARIABLE)
    line = f"{PUBLISH_HOST_VARIABLE}: {raw!r} -- {_PUBLISH_EVIDENCE[verdict]}"
    if verdict == PUBLISH_NO_MAPPING and raw is not None:
        line += (
            " (SET BUT IGNORED: it is read only inside a container, because on a host there is no "
            "publish to declare and a process could otherwise hand itself a pass)"
        )
    return [line]


def stop_proof(outcome: str, pid: int | None, gone: bool, signals: list[str]) -> str:
    """The sentence that says what was ESTABLISHED about one worker's absence.

    A function at the bottom rather than a three-deep conditional expression
    inside the row builder, for the reason rule 10 gives: this is the decision --
    which of four different things happened to this worker -- and it is now
    callable with seeded values, which is how all four are checked without
    spawning four processes in four states.

    STALE-PIDFILE IS ITS OWN SENTENCE, AND IT USED TO BE RENDERED AS A FAILED
    KILL. The row builder asked only "is the pid gone", and for a stale pid file
    the answer is no -- the pid is alive, it is simply not ours any more, and
    nothing was signaled. The page therefore read

        pid 4021 is STILL ALIVE after no signal

    which an operator reads as "the worker refused to die". The true reading is
    "that number now belongs to something else and we deliberately left it alone;
    whether our worker is running is a question only the orphan scan can answer".
    Those are different problems with different responses, and rule 13's fourth
    bullet is that they must not render alike.

    It is also not hypothetical on this surface: f702d5e measured an empty
    /proc/<pid>/cmdline for a just-spawned live process 152 times in 400, so a
    stop racing a start reports exactly this outcome 38% of the time.
    """
    sent = "+".join(signals) or "nothing"
    if outcome == "stale-pidfile":
        return (
            f"pid {pid} was NOT signaled, deliberately. It is alive, but /proc says it is no longer the "
            f"process this pid file named, so signaling it would have killed a stranger. This row does NOT "
            f"say our worker is still running -- the scan below is the only thing that can answer that, and "
            f"this is also what a stop racing a start looks like."
        )
    if outcome == "stopped" and gone and pid is not None:
        return (
            f"pid {pid} was signaled with {sent} and the operating system now reports it ABSENT -- asked "
            f"again after the stop returned, not taken from the kill's exit code"
        )
    if pid is not None and signals and not gone:
        return f"pid {pid} is STILL ALIVE after {sent}"
    return "nothing was signaled, so there is no absence to prove here -- the scan below is the evidence"


def _worker_rows_now(names: list[str], run_dir: Path) -> list[dict]:
    """Each worker's state right now, with what stopping it costs. Changes nothing.

    supervisor.worker_status() is the one implementation of "is this worker
    running" and services/admin_view.worker_stopped_consequence() is the one
    implementation of "what does it cost that it is not". Both are imported rather
    than re-derived: a second answer to either question is rule 8's bug with a
    delay on it, and the consequence table in particular was already shipped wrong
    once -- three cards rendering one worker's consequence for all three.

    (worker_stopped_consequence() lives in services/admin_view.py, which is a VIEW
    builder, and the vocabulary would sit better in supervisor.py next to the
    workers it describes. Moving it was not possible in this change: admin_view.py
    was being restructured concurrently. It is a one-function move whenever that
    file is editable again, and this import is the only thing outside it that
    would need to follow.)
    """
    rows = []
    for name in names:
        row = dict(supervisor.worker_status(name, run_dir))
        row["stopped_consequence"] = worker_stopped_consequence(name)
        row["arms"] = WORKER_ARMS.get(
            name,
            f"UNKNOWN -- {name} is not in kill_switch.WORKER_ARMS, so what starting it does is not recorded here",
        )
        rows.append(row)
    return rows


def stop_everything(
    names: list[str] | None = None,
    run_dir: Path | None = None,
    grace_seconds: float = WEB_GRACE_SECONDS,
) -> dict:
    """Stop every supervised worker and PROVE each one is gone. Returns the proof.

    THE VERDICT IS DERIVED FROM ABSENCE AND FROM THE ORPHAN SCAN, never from the
    fact that a signal was sent. That is rule 13's central sentence -- "a stop
    that cannot prove it worked is not a stop" -- and the measurement behind it is
    in supervisor.unaccounted_workers()'s docstring: on 2026-10-01 a stop printed
    `not running` three times and `untouched=3`, every line true of the pid in
    each file, while whether a worker was alive under some OTHER pid had not been
    asked. `untouched=3` reads as "nothing needed doing".

    So three separate facts go into the verdict, and all three can refuse it:

      per-worker absence   asked of the operating system AFTER stop_worker()
          returned, not taken from its return value. The field is `gone`, and it
          is a fresh os.kill(pid, 0) rather than a restatement of the outcome
          string.
      the orphan scan      supervisor.unaccounted_workers(), which finds a worker
          process alive that no pid file accounts for. An orphan makes the verdict
          NOT PROVEN even when every pid file's process died cleanly, because the
          thing the operator asked for -- nothing is polling the database -- is
          false.
      whether /proc could be read at all   on a platform without it the scan
          cannot look, and "could not look" is not "nothing found". The verdict is
          NOT PROVEN, and the row says which of the two it is.

    Nothing here signals a process this supervisor did not start. An orphan is
    REPORTED with its pid and left alone: killing a process we cannot prove is
    ours is how a recycled pid gets killed, and what to do about one is the
    operator's (rule 16).
    """
    started_at = time.monotonic()
    run_dir = supervisor.DEFAULT_RUN_DIR if run_dir is None else run_dir
    names = list(supervisor.worker_commands()) if names is None else names

    rows = []
    for name in names:
        result = supervisor.stop_worker(name, run_dir, grace_seconds)
        pid = result.get("pid")
        # THE ABSENCE IS ASKED AGAIN, HERE, OF THE OS. stop_worker() already polls
        # for it and only returns "stopped" when the process is gone -- this is not
        # distrust of that function, it is the difference between a report that
        # repeats a claim and a report that makes one. The page says "pid 1234 is
        # gone", so the page asks.
        gone = True if pid is None else not supervisor.process_alive(pid)
        rows.append(
            {
                "worker": name,
                "outcome": result["outcome"],
                "pid": pid,
                "signals": result.get("signals", []),
                "note": result.get("note", ""),
                "seconds": result.get("seconds"),
                "elapsed": format_duration(result["seconds"]) if result.get("seconds") is not None else "",
                "gone": gone,
                "proof": stop_proof(result["outcome"], pid, gone, result.get("signals", [])),
                "stopped_consequence": worker_stopped_consequence(name),
            }
        )

    orphans = supervisor.unaccounted_workers(names, run_dir)
    orphan_count = sum(len(pids) for pids in orphans.values())
    # The same block supervisor.py's CLI prints, from the same function, so the
    # page and the terminal cannot describe one scan two ways.
    orphan_lines = supervisor.unaccounted_lines(names, run_dir)
    proof_established = supervisor.PROC_DIR.is_dir()

    signaled = sum(1 for row in rows if row["signals"])
    stopped = sum(1 for row in rows if row["outcome"] == "stopped" and row["gone"])
    # A KILL THAT DID NOT WORK, and nothing else. The test was `pid is not None and
    # not gone`, which swept in every stale-pidfile row: that pid is alive and was
    # never signaled, so counting it as a failed kill reported a kill that never
    # happened as one that failed. The signal is what makes "still alive" a
    # failure, so the signal is what the count asks about.
    failed = sum(1 for row in rows if row["outcome"] == "failed" or (row["signals"] and not row["gone"]))
    # COUNTED SEPARATELY AND IT STILL REFUSES THE VERDICT. Separating it from
    # `failed` is about naming the right problem, NOT about downgrading it: a stale
    # pid file means the pid file stopped being evidence, so this stop cannot say
    # our worker is gone. Anything else would be "skipped" rendered as "success".
    stale = sum(1 for row in rows if row["outcome"] == "stale-pidfile")

    if failed or stale or orphan_count or not proof_established:
        verdict = VERDICT_NOT_PROVEN
    elif stopped:
        verdict = VERDICT_STOPPED
    else:
        verdict = VERDICT_NOTHING_RUNNING

    return {
        "action": "stop",
        "verdict": verdict,
        "headline": VERDICT_HEADLINES[verdict],
        "glyph": VERDICT_GLYPHS[verdict],
        "refusals": [],
        # A STOP SHOWS NO SPAWN WARNING, and the key is present and empty rather
        # than absent. The two buttons do not deserve the same ceremony -- stop is
        # defensive and reversible, start arms a process that broadcasts -- so the
        # warning belongs to one of them only. Present-and-empty is what lets the
        # template read one key set for both (RESULT_CORE_KEYS).
        "spawn_warning": "",
        "workers": rows,
        "orphans": orphan_lines,
        "orphan_count": orphan_count,
        "proof_established": proof_established,
        "counts": {
            "signaled": signaled,
            "stopped": stopped,
            "failed": failed,
            "stale_pidfile": stale,
            "untouched": len(rows) - stopped - failed - stale,
            "orphans": orphan_count,
        },
        "grace": format_duration(grace_seconds),
        "elapsed": format_duration(time.monotonic() - started_at),
    }


def start_everything(
    names: list[str] | None = None,
    run_dir: Path | None = None,
    commands: dict[str, list[str]] | None = None,
    settle_seconds: float = supervisor.IMMEDIATE_DEATH_SETTLE_SECONDS,
    spawn_warning: str = "",
) -> dict:
    """Spawn every supervised worker that is not already running, then ask again.

    THE ACKNOWLEDGMENT IS NOT CHECKED HERE and the lock is not taken here. Both
    belong to operate() below, which is the ONE composition of the two and the only
    thing routes/kill_switch.py calls. The split is deliberate: this function is
    the part that spawns, and a test that wants to prove the lock works has to be
    able to call the spawning part while another process holds the lock.

    THE COMPOSITION IS NOT THE ROUTE'S, and an earlier draft of this paragraph said
    it was ("the route is the only caller that does both"). That would have put the
    order of four checks -- refuse, lock, acknowledge, act -- inside an HTTP
    handler, where it is reachable only by serving a request and where this file's
    own header says no decision lives. Rule 10: the decision is the smallest
    testable piece at the bottom. operate() is that piece, and the handler calls
    it and renders what comes back.

    "ALREADY RUNNING" IS NOT A QUIET SUCCESS. It is its own verdict with its own
    sentence, because rule 13 treats "skipped" printed beside "ok" as a defect in
    the output -- and on this page the two readings are "I have just started the
    desk" and "the desk was already running and I changed nothing", which an
    operator is entitled to tell apart.

    DIED IS THE OUTCOME THAT MATTERS MOST. supervisor.confirm_spawned() asks the
    operating system a second time, after a settle, whether each freshly spawned
    worker is still a process -- because Popen() returning is not evidence that
    the thing it spawned survived its own imports. A worker that died is reported
    with its exit code and the last line of its log, and its pid file is removed
    so a later stop does not have to report an absence it did not cause.
    """
    started_at = time.monotonic()
    run_dir = supervisor.DEFAULT_RUN_DIR if run_dir is None else run_dir
    table = supervisor.worker_commands() if commands is None else commands
    names = list(table) if names is None else names

    # THE ORPHAN SCAN RUNS BEFORE THE SPAWN, AND IT BLOCKS IT.
    #
    # start_worker()'s already-running check reads the PID FILE, which is the
    # right question for the process it started and a question with a hole in it:
    # a worker started by hand in a shell, or one left behind by a gunicorn worker
    # that was recycled, has no pid file at all. So the check says "not running",
    # the spawn proceeds, and the host now runs TWO payout workers against one
    # database -- which this repository has already produced once, recorded as
    # "2 sends, 1 swap_id, 2 broadcast rows, on-chain and final".
    #
    # The flock closes the two-gunicorn-workers race. It cannot close this one:
    # the other copy was never under this lock, and no lock taken today can be.
    # The scan is the only thing that can see it, so the scan decides.
    #
    # REFUSING IS THE DEFENSIVE DIRECTION, which is why it is done here rather
    # than handed back as a proposal (rule 16's line is money, and both sides of
    # this were weighed): declining to spawn arms nothing, costs the operator one
    # shell command, and the thing it prevents is a second process broadcasting
    # payouts. Nothing is signaled -- killing a process this page cannot prove is
    # ours is still the operator's, and the pid is printed so they can.
    #
    # ON A PLATFORM WITHOUT /proc the scan returns {} and the spawn proceeds. That
    # is "could not look", not "nothing found", and it is said in the result
    # (`proof_established`) rather than being read as an all-clear -- the same
    # distinction supervisor.unaccounted_lines() draws for the stop path.
    orphans_before = supervisor.unaccounted_workers(names, run_dir)
    if orphans_before:
        blocked = refusal_result(
            "start",
            [
                "A worker process is already polling that no pid file accounts for, so starting would "
                "have run a second copy of it against the same database. Nothing was spawned.",
                *supervisor.unaccounted_lines(names, run_dir),
            ],
            VERDICT_ORPHAN_BLOCKED,
        )
        blocked["orphans"] = supervisor.unaccounted_lines(names, run_dir)
        blocked["orphan_count"] = sum(len(pids) for pids in orphans_before.values())
        blocked["proof_established"] = supervisor.PROC_DIR.is_dir()
        blocked["elapsed"] = format_duration(time.monotonic() - started_at)
        return blocked

    results = [supervisor.start_worker(name, table[name], run_dir) for name in names]
    # Spawn everything first, THEN settle once, exactly as supervisor's CLI does:
    # the wait is paid once for the command rather than once per worker.
    results = supervisor.confirm_spawned(results, settle_seconds)

    rows = []
    for result in results:
        outcome = result["outcome"]
        # THE PID FILE OF A WORKER THAT DIED IS REMOVED BY confirm_spawned(), and
        # this loop used to remove it a second time. supervisor.command_start() had
        # the identical two lines, so one cleanup was spelled twice and a third
        # caller would have had to know to spell it again -- rule 8, in the one
        # place a missed cleanup leaves rule 13's stale pid file behind. The removal
        # moved into confirm_spawned() (2026-10-02), whose docstring had claimed it
        # all along, and start_worker() now carries run_dir out in the result so
        # the cleanup cannot be separated from the finding that triggers it.
        rows.append(
            {
                "worker": result["worker"],
                "outcome": outcome,
                "pid": result["pid"],
                "log": result.get("log", ""),
                "exit_code": result.get("exit_code"),
                "last_log_line": result.get("last_log_line", ""),
                "arms": WORKER_ARMS.get(result["worker"], ""),
                "proof": (
                    f"pid {result['pid']} was still running {format_duration(settle_seconds)} after the spawn, "
                    f"asked of the OS rather than assumed from Popen() returning"
                    if outcome == "started"
                    else f"pid {result['pid']} exited {result.get('exit_code')} within "
                    f"{format_duration(settle_seconds)} of being spawned, so it is NOT polling"
                    if outcome == "DIED"
                    else f"nothing was spawned for this one; pid {result['pid']} was already running"
                ),
            }
        )

    spawned = sum(1 for row in rows if row["outcome"] == "started")
    died = sum(1 for row in rows if row["outcome"] == "DIED")
    already = sum(1 for row in rows if row["outcome"] == "already-running")

    if died:
        verdict = VERDICT_DIED
    elif spawned and already:
        verdict = VERDICT_PARTLY_STARTED
    elif spawned:
        verdict = VERDICT_STARTED
    else:
        verdict = VERDICT_ALREADY_RUNNING

    return {
        "action": "start",
        "verdict": verdict,
        "headline": VERDICT_HEADLINES[verdict],
        "glyph": VERDICT_GLYPHS[verdict],
        "refusals": [],
        "workers": rows,
        # SCANNED AGAIN, AFTER THE SPAWN, and it is not the same question as the
        # scan above. That one asked "is something already polling"; this one asks
        # "is everything that is now polling accounted for by a pid file" -- and a
        # start is precisely when the answer can change, because a spawn that wrote
        # its pid file and then had the file removed under it is the orphan this
        # whole module is about. The two reads are separated by the spawn, which is
        # the event between them, so caching the first would be reporting a fact
        # from before the thing that could have changed it.
        "orphans": supervisor.unaccounted_lines(names, run_dir),
        "orphan_count": sum(len(pids) for pids in supervisor.unaccounted_workers(names, run_dir).values()),
        "proof_established": supervisor.PROC_DIR.is_dir(),
        # The warning that was acknowledged, carried into the result so the page
        # shows what the operator agreed to rather than re-deriving it from a
        # configuration that may have moved since (see acknowledgment_token()).
        "spawn_warning": spawn_warning,
        "counts": {"spawned": spawned, "already_running": already, "died": died},
        "settle": format_duration(settle_seconds),
        "elapsed": format_duration(time.monotonic() - started_at),
    }


def refusal_result(
    action: str,
    refusals: list[str],
    verdict: str = VERDICT_REFUSED,
    spawn_warning: str = "",
) -> dict:
    """The same shape stop_everything()/start_everything() return, for a refusal.

    ONE SHAPE, so the template renders a refusal through the same block as a
    result and cannot grow a second rendering path that nobody looks at. The empty
    `workers` list is what makes it obvious on the page that nothing was touched:
    the worker table renders `(none -- nothing was signaled or spawned)` rather
    than disappearing, because a missing region and an empty one are the ambiguity
    rule 14 is about.

    IT WAS NOT ACTUALLY ONE SHAPE, and the docstring claimed it was. A stop result
    carries `orphans`, `orphan_count` and `proof_established`; a refusal carried
    none of the three, so every reader of a result had to reach for a Jinja
    `default` or a `.get` -- and a template that silently tolerates a missing key
    is the one that silently renders nothing when a key is misspelled, which is
    rule 14's blank gap arriving through the back door. The keys are present and
    empty here now, and test_kill_switch.py asserts the three functions return the
    same key set rather than trusting this paragraph.

    `orphans` IS EMPTY AND THAT IS NOT A CLAIM THAT THERE ARE NONE. A refused
    request runs no /proc scan at all -- see switch_panel() on why a refusing
    render discloses nothing and does no work -- so the honest value is "this was
    not asked", and `proof_established: None` is the field that says which. The
    template prints the distinction; it does not print an empty orphan list as
    "no orphans".
    """
    return {
        "action": action,
        "verdict": verdict,
        "headline": VERDICT_HEADLINES[verdict],
        "glyph": VERDICT_GLYPHS[verdict],
        "refusals": refusals,
        "workers": [],
        "orphans": [],
        "orphan_count": 0,
        "proof_established": None,
        "spawn_warning": spawn_warning,
        "counts": {},
        "elapsed": format_duration(0.0),
    }


ACTIONS = ("start", "stop")


@dataclass(frozen=True)
class ControlTarget:
    """WHAT a control action operates on, as one value rather than five parameters.

    The same argument RequestFacts makes for the four facts about a request, for
    the same reason: these five travel together, and a caller that passes three of
    them is operating on a target it did not fully name. Every default is the live
    one, so `ControlTarget()` is what the route uses and a test substitutes only
    the fields it needs -- most often `run_dir` (a tmp_path, so the real pid files
    and the real lock are never touched) and `commands` (a harmless `sleep` instead
    of a payout worker that broadcasts).

    IT IS ALSO WHAT KEPT operate() UNDER ruff's PLR0913 WITHOUT A noqa. The first
    draft took nine parameters and the answer rule 19 asks for is to fix the code,
    not to suppress the finding -- and the fix is the better shape anyway, because
    "the thing being operated on" is a real noun that was being spelled as five
    loose arguments at every call site.

    `grace_seconds` and `settle_seconds` STAY SECONDS, and the names say so (rule
    6): they are passed into monotonic arithmetic and into time.sleep(), which is
    an interface. The microfortnight figures are produced on the way out, at the
    row write and at the print.
    """

    run_dir: Path | None = None
    names: list[str] | None = None
    commands: dict[str, list[str]] | None = None
    grace_seconds: float = WEB_GRACE_SECONDS
    settle_seconds: float = supervisor.IMMEDIATE_DEATH_SETTLE_SECONDS


def operate(
    action: str,
    facts: RequestFacts,
    acknowledgment: str = "",
    target: ControlTarget | None = None,
    warning: str | None = None,
) -> dict:
    """THE one entry point for doing anything. Four checks in one order, then act.

    routes/kill_switch.py calls this and renders what comes back. It makes no
    decision of its own, which is this module's header's claim and is only true
    because the ORDER of the four checks lives here rather than in a handler:

      1. is `action` one of the two?   A typo, or a crafted form field, must not
         fall through to a default. There is no default.
      2. may this caller act at all?   control_refusals(facts) -- the loopback
         bind and the cross-origin checks, the SAME function the GET uses to
         decide whether to render the buttons, so the page and the endpoint cannot
         disagree about who may act.
      3. is anybody else mid-action?   The flock. See below.
      4. for a START ONLY, was the warning acknowledged?

    WHY THE LOCK IS TAKEN BEFORE THE ACKNOWLEDGMENT IS CHECKED, which is the one
    ordering decision here that could sensibly go either way. Checking the
    acknowledgment first would keep a refused start from touching the lock at all,
    and that is the only thing it would buy. Taking the lock first buys something
    larger: the warning that is VERIFIED and the configuration that is read by the
    spawn are separated by no window at all, so an acknowledgment cannot be
    validated against a configuration that the other gunicorn worker is in the
    middle of acting on. The refusal path holds the lock for the microseconds a
    sha256 of one sentence takes.

    WHY THE LOCK IS THE MECHANISM AND NOT A CHECK. gunicorn.conf.py runs two sync
    workers and either can serve this POST. `if not already_running(): start()` is
    two steps in two processes, and both can pass the first before either reaches
    the second -- which is three supervised workers plus three orphans with no pid
    file naming them, the failure rule 13 is written from. flock is decided once,
    by the kernel, and it is held across the WHOLE sequence: the spawn, the settle
    and the confirm. A start that loses the race is told so in a sentence that
    names the holder; it does not wait, and it does not spawn.

    STOP TAKES THE SAME LOCK, which is not symmetry for its own sake. f702d5e
    measured /proc/<pid>/cmdline reading EMPTY for a just-spawned live process 152
    times in 400 -- 38% -- so a stop racing a start cannot tell whether the pid it
    is about to signal is ours, and the honest answer in that window is "stale pid
    file" rather than a stop. Serializing the two removes the window instead of
    reporting it.

    `target` NAMES WHAT IS OPERATED ON -- see ControlTarget. `warning` IS INJECTABLE
    AND DEFAULTS TO ASKING. supervisor.spawn_warning()
    builds the chain adapters and reads the payout unlock state; a test that wants
    to prove the acknowledgment gate works should not need a funded wallet's worth
    of configuration to do it. Passing None -- the default, and what the route
    passes -- reads the live configuration, which is the whole point of
    fingerprinting it (see acknowledgment_token()).
    """
    target = ControlTarget() if target is None else target
    run_dir = supervisor.DEFAULT_RUN_DIR if target.run_dir is None else target.run_dir

    if action not in ACTIONS:
        return refusal_result(
            action,
            [
                f"{action!r} is not an action. This surface does exactly two things, "
                f"{' and '.join(ACTIONS)}, and an unrecognized one is refused rather than "
                f"falling through to either of them."
            ],
        )

    refusals = control_refusals(facts)
    if refusals:
        return refusal_result(action, refusals)

    path = lock_path(run_dir)
    with single_flight(path) as holder:
        if holder is not None:
            return refusal_result(
                action,
                [
                    f"{path} is held by another start or stop: {holder}. Nothing was started and "
                    f"nothing was stopped by THIS request. The other one is still running -- wait "
                    f"for it and reload, rather than clicking again.",
                    "The lock is why that is a refusal and not a second spawn: two of these "
                    "controls running at once is how a host ends up with two payout workers and "
                    "pid files naming only one of them.",
                ],
                VERDICT_IN_FLIGHT,
            )

        if action == "stop":
            return stop_everything(target.names, run_dir, target.grace_seconds)

        current = supervisor.spawn_warning() if warning is None else warning
        expected = acknowledgment_token(current)
        if acknowledgment != expected:
            return refusal_result(
                "start",
                [
                    "The warning below was not acknowledged, or it has CHANGED since the page you "
                    "clicked on was rendered. NOTHING WAS SPAWNED.",
                    f"The form carried {acknowledgment or '(nothing)'}; the warning showing right now "
                    f"fingerprints as {expected}. A mismatch means the configuration moved between "
                    f"the render and the click -- a database repointed at mainnet, a passphrase "
                    f"exported -- so the sentence you read is not the sentence that would apply.",
                    "Read it again and resubmit. This is the ONLY thing standing between a click and "
                    "a process that broadcasts payouts, which is why it refuses rather than warning.",
                ],
                VERDICT_NOT_ACKNOWLEDGED,
                spawn_warning=current,
            )
        return start_everything(
            target.names, run_dir, target.commands, target.settle_seconds, spawn_warning=current
        )


def switch_panel(
    facts: RequestFacts,
    run_dir: Path | None = None,
    names: list[str] | None = None,
    result: dict | None = None,
) -> dict:
    """Everything the page renders, decided here so the template decides nothing.

    `result` is the outcome of a POST, or None for a plain GET. It is carried
    THROUGH rather than rebuilt, which is the defect this codebase keeps
    rediscovering in the other direction: a correct decision function whose caller
    throws the answer away, which looks exactly like a working feature until
    somebody checks whether the page changes.

    THE WARNING IS BUILT ON EVERY RENDER, from supervisor.spawn_warning(), because
    it is derived from the live configuration and a cached one is a sentence about
    a host that may since have been repointed. Its fingerprint goes into the form,
    so the acknowledgment is of the warning that was actually shown -- see
    acknowledgment_token().
    """
    run_dir = supervisor.DEFAULT_RUN_DIR if run_dir is None else run_dir
    names = list(supervisor.worker_commands()) if names is None else names

    refusals = control_refusals(facts)
    if refusals:
        # A REFUSED RENDER CARRIES THE REFUSAL AND NOTHING ELSE, and the omission
        # is deliberate rather than tidy. If this server is reachable off-box then
        # the reader may be a stranger, and the panel's own operational detail --
        # the pid of every worker, the database path, the run directory, the orphan
        # scan -- is exactly what /admin's banner warns about disclosing. The
        # refusal has to SAY WHY (a control that silently vanishes is a control an
        # operator debugs for an hour), and it has to say it without handing over
        # the inventory. The static sentences below disclose nothing: they describe
        # what this page would reach, not what is running.
        #
        # It also means a stranger's GET runs no /proc scan and spawns no adapter
        # probe, so an exposed deployment cannot be made to do work by being
        # fetched in a loop.
        return {
            "controls_enabled": False,
            "refusals": refusals,
            "bind_evidence": bind_evidence(facts),
            "workers": [],
            "orphans": [],
            "proof_established": None,
            "spawn_warning": "",
            "acknowledgment": "",
            "result": result,
            "database": "(withheld while these controls are refusing -- see above)",
            "run_dir": "(withheld)",
            "lock": "(withheld)",
            "grace": format_duration(WEB_GRACE_SECONDS),
            "reaches": REACHES,
            "cannot_reach": CANNOT_REACH,
        }

    # NOT CACHED AND NOT PASSED IN. spawn_warning() reads the adapters and the
    # payout readiness every time; that is the point of it.
    warning = supervisor.spawn_warning()

    return {
        "controls_enabled": True,
        "refusals": [],
        "bind_evidence": bind_evidence(facts),
        "workers": _worker_rows_now(names, run_dir),
        "orphans": supervisor.unaccounted_lines(names, run_dir),
        "proof_established": supervisor.PROC_DIR.is_dir(),
        "spawn_warning": warning,
        "acknowledgment": acknowledgment_token(warning),
        "result": result,
        # Rule 14: echo the parameters that decide the answer, because a pasted
        # page is read a day later and has to say which host and which database it
        # was describing.
        "database": str(Config.DB_PATH),
        "run_dir": str(run_dir),
        "lock": str(lock_path(run_dir)),
        "grace": format_duration(WEB_GRACE_SECONDS),
        "reaches": REACHES,
        "cannot_reach": CANNOT_REACH,
    }


#: What the two buttons reach, and what they do not, as data rather than as prose
#: in a template -- so the page cannot drift from the module header's inventory and
#: a test can assert that every unreachable spawn site is still named. The header
#: above says how this list was established.
REACHES = [
    (
        "deposit_watcher, payout_worker, reconcile_worker",
        "the three polling workers. Signaled by pid file, absence confirmed against /proc, and "
        "anything alive that no pid file names is reported as an orphan.",
    ),
]

CANNOT_REACH = [
    (
        "this web server (gunicorn master and 2 workers)",
        "A stop that killed it could not report that it had -- the response dies with the worker, "
        "so 'it worked' and 'it crashed first' look identical. Rule 13 forbids a stop that cannot "
        "prove it worked, so it is not attempted. Close the launcher window, or stop gunicorn at a "
        "shell.",
    ),
    (
        "swap_terminal_desktop.py (the launcher) and its browser window",
        "The launcher is this server's PARENT and holds runtime/launcher.lock. Its reaper is "
        "closing the window, which walks its own process group and asserts absence.",
    ),
    (
        "operator_panel.py and any regtest harness it started",
        "A separate root entry point on port 8765, run by hand, whose child can be holding a "
        "funding output mid-spend. Its reaper is its own Ctrl-C handler.",
    ),
    (
        "a worker started by hand in a shell",
        "It has no pid file, so there is no pid to signal. It IS detected: the scan below names it "
        "with its pid and NEVER signals it, because killing a process this page cannot prove is "
        "ours is how a recycled pid gets killed.",
    ),
]
