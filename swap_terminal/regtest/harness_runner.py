"""One harness run at a time: spawn it, stream what it says, and prove it is gone when stopped.

Role: submodule (process lifecycle for the operator panel; it holds no knowledge of WHICH
      commands exist -- operator_panel.RUNNABLE decides that)
Reads: the child's stdout. Nothing else.
Writes: nothing on disk.
Can move funds: indirectly -- it spawns the entry point it is handed, and some of those
      broadcast. It builds and signs nothing itself.
Mainnet-safe: not its decision. The caller gates on network before it reaches here.
Live-safe: yes for the daemons. It starts and stops no daemon; the only process it kills is one
      it started itself, by pid, and it checks.

RULE 13 IS THE WHOLE DESIGN. "Every spawn needs a reaper, and stop must be proven to reach it."
A browser tab is exactly the kind of caller that goes away mid-run -- closed, reloaded,
navigated -- and a harness that keeps a Gridcoin run going after the page that started it is
gone is the orphan that rule describes, holding whatever the run holds while everything
downstream reports success.

So:

  ONE AT A TIME.        `start` refuses while a run is alive rather than queueing or racing. Two
                        concurrent runs would both take the same funding output and the second
                        would fail as a double-spend several steps later with no clue why --
                        the exact failure discover_operator_funding_txid's comment describes.
  KILLED BY PID.        Never a pgrep pattern. Rule 13 prefers a pid file to a pattern because
                        "a pattern matches what the command line happens to look like today";
                        a Popen object is the same guarantee, held in memory.
  STOP IS PROVEN.       `stop` does not report success from the return code of a kill. It waits
                        and asserts the process is GONE, escalates to SIGKILL if it is not, and
                        says which of the two it took.
  SKIPPED != DONE.      `state()` reports "no run has been started" as its own word, never as an
                        empty output that reads like a run which printed nothing (rule 14).

WHY THE OUTPUT IS A LIST AND NOT A FILE. A run's output is a mirror for a human reading a
screen, not a record anything decides from (rule 5), and it dies with the process that made it.
The authority for what a run did is the chain and the run's own exit code.
"""

from __future__ import annotations

import os
import subprocess
import threading
from typing import NamedTuple

#: How long to give a stopped run to die on its own before SIGKILL, in SECONDS -- an interface
#: to `Popen.wait`, not a report, so rule 6's boundary keeps it in seconds here and converts on
#: the way out. Five is long enough for a Python process to unwind an RPC and short enough that
#: an operator who pressed stop does not wonder whether the button worked.
GRACE_SECONDS = 5.0

#: The most output lines kept in memory. A ten-minute Gridcoin run prints a few hundred; the cap
#: is here so a runaway cannot grow without bound, and when it bites the panel SAYS it did
#: rather than silently showing a truncated run as if it were whole (rule 14, no silent caps).
MAX_LINES = 5000


class RunState(NamedTuple):
    """What the panel renders. Every field is present whether or not a run exists."""

    name: str
    alive: bool
    started: bool
    exit_code: int | None
    lines: list[str]
    dropped: int
    verdict: str


class HarnessRunner:
    """Owns at most one child process and the lines it has printed.

    NOT A SINGLETON, and not module-level state. The server holds one instance; a test holds its
    own. Module-level mutable state is what makes a test's outcome depend on which other test
    ran first, and this class exists partly to be tested without a browser.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._name = ""
        self._lines: list[str] = []
        self._dropped = 0
        self._started = False
        self._reader: threading.Thread | None = None
        #: PER INSTANCE, not read from the module constant at each use. A test that has to wait
        #: the real five seconds to prove the SIGKILL path is a test somebody eventually marks
        #: slow and stops running -- and that path is the one rule 13 is actually about.
        self.grace_seconds = GRACE_SECONDS

    def start(self, name: str, argv: list[str], cwd: str) -> str:
        """Spawn `argv`, or say why not. Returns "" on success and a REASON otherwise.

        A STRING RATHER THAN AN EXCEPTION because every caller here is an HTTP handler that has
        to turn the answer into a body either way, and a refusal to start is an ordinary
        outcome -- the operator pressed a second button while the first was running -- not an
        error in the panel.

        The environment is INHERITED whole, which is how ST_ADAPTOR_FUNDING_SEED reaches the
        child. It is never read here, never logged, and never put on the command line: rule 16's
        line about secrets, and swap_terminal/CLAUDE.md's "never move, copy, or read back a key".
        """
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                return f"{self._name} is still running. Stop it first -- one run at a time."
            self._name = name
            self._lines = []
            self._dropped = 0
            self._started = True
            self._process = subprocess.Popen(  # noqa: S603 -- argv from the caller's allowlist, never a shell, never user text
                argv,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=os.environ.copy(),
            )
            self._reader = threading.Thread(target=self._drain, args=(self._process,), daemon=True)
            self._reader.start()
            return ""

    def _drain(self, process: subprocess.Popen) -> None:
        """Read the child's output line by line until it closes its pipe.

        A THREAD AND NOT A POLL, because the point of this panel is that a ten-minute run says
        what it is doing while it does it (rule 14). Reading only when the browser asks would
        buffer the whole run in a pipe and deliver it at the end, which is the blinking cursor
        with extra steps.

        DAEMON THREAD, and that is safe here precisely because stop() reaps by pid: the thread
        holds nothing that needs unwinding, and the process it reads is killed explicitly rather
        than left to the interpreter's exit.
        """
        # NOT AN `assert`. S101 is in this repo's ruff selection and rule 19 says a suppression
        # is a claim you checked, not a way to quiet a finding -- and here the finding is right:
        # `python -O` strips asserts, so a guard written that way is absent in exactly the
        # deployment where a None pipe would be hardest to diagnose. `or []` is the same
        # protection with no interpreter flag attached: nothing to read means no lines.
        for line in process.stdout or []:
            with self._lock:
                if len(self._lines) >= MAX_LINES:
                    self._dropped += 1
                    continue
                self._lines.append(line.rstrip("\n"))

    def stop(self) -> str:
        """Kill the run and PROVE it is gone. Returns what happened, always non-empty.

        "A stop that cannot prove it worked is not a stop. Follow it with a check that the
        process is gone, and make the absence the assertion -- not the exit code of the kill."
        That is rule 13 verbatim, and this is it: terminate, wait, and if it is still there,
        SIGKILL and wait again. The sentence returned says which of the two it took, because an
        operator who reads "stopped" has no way to tell a clean exit from a process that ignored
        the first signal and may have left something half-written.
        """
        with self._lock:
            process, name = self._process, self._name
        if process is None:
            return "nothing was running"
        if process.poll() is not None:
            return f"{name} had already exited with code {process.returncode}"
        process.terminate()
        try:
            process.wait(timeout=self.grace_seconds)
            return f"{name} stopped, and the process is gone (exit {process.returncode})"
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.grace_seconds)
            return (
                f"{name} ignored the first signal and was KILLED (exit {process.returncode}). "
                f"It may have been interrupted mid-step; read the output above before re-running."
            )

    def state(self) -> RunState:
        """Everything the page needs, in one consistent snapshot taken under the lock."""
        with self._lock:
            process = self._process
            lines = list(self._lines)
            dropped = self._dropped
            name = self._name
            started = self._started
        if not started:
            return RunState("", False, False, None, [], 0,
                            "no run has been started in this panel yet")
        if process is None:
            return RunState(name, False, True, None, lines, dropped, "no process")
        code = process.poll()
        if code is None:
            return RunState(name, True, True, None, lines, dropped, f"{name} is RUNNING")
        return RunState(name, False, True, code, lines, dropped,
                        f"{name} finished with exit code {code}"
                        + ("  (0 means every check passed)" if code == 0 else
                           "  (non-zero: read the output, a refusal is not a failure)"))
