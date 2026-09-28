#!/usr/bin/env python3
"""The operator's control panel: see the funding before a run, start one, watch it, stop it.

Role: file (the entry point; every decision is in swap_terminal/regtest/operator_panel.py and
      swap_terminal/regtest/harness_runner.py, and this holds none of its own)
Reads: the Gridcoin TESTNET daemon over JSON-RPC, and ST_ADAPTOR_FUNDING_SEED from its own
      environment
Writes: nothing on disk
Can move funds: indirectly -- it spawns entry points from an allowlist, some of which
      broadcast. It builds, signs and sends nothing itself.
Mainnet-safe: NO, AND IT REFUSES TO BE ASKED. The daemon must SAY it is on a test network
      before the server binds a port.
Live-safe: yes. It starts and stops no daemon and never touches a wallet lock.

WHY THIS EXISTS. On 2026-09-28 it took six runs of grc_htlc_verify.py to reach step 4, and not
one of the six failed for a reason visible from the terminal before it started. Every one of
them turned on state that was knowable in advance -- which payments to the funding address were
still unspent, what the daemon could be asked, which key owned which coin -- and that state was
only ever computed INSIDE a run, as a side effect of choosing. This is the place to look at it
first.

IT IS NOT THE FLASK APP. `swap_terminal/routes/admin.py` serves an operator page whose GET-only
shape is structural and asserted by tests/test_admin_surface.py over the real URL map, and the
whole app is unauthenticated behind a loopback bind. Buttons that spend money do not belong on
a surface with that posture, so this is a separate server sharing no route table with it --
stdlib only, no Flask, no database, no configuration file.

THE GUARDS, and each is a refusal rather than a warning:

  127.0.0.1 ONLY    `--host` does not exist. The bind address is a constant. The Flask app
                    makes loopback a DEFAULT, which is right for a read-only page; a page with
                    buttons should not have an override to lose.
  TESTNET ONLY      the daemon is asked before the port is bound, three ways, and an absence of
                    evidence is treated as mainnet -- the same gate grc_htlc_verify.py uses.
  AN ALLOWLIST      the browser posts a KEY from operator_panel.RUNNABLE. No path, no argument
                    and no flag from a request ever reaches subprocess, which is called with a
                    list and never a shell string.
  NO SEED IN, EVER  ST_ADAPTOR_FUNDING_SEED is read from this process's environment and
                    inherited by children. There is no field for it on the page, and it is
                    never rendered, logged, or put on a command line.

RULE 13: the page can be closed mid-run, and a run that outlived the tab that started it is
exactly the orphan that rule is about. HarnessRunner kills by pid and PROVES the process is
gone; the panel refuses to start a second run while one is alive.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# The sys.path line must run before these imports: this repository's modules import each other
# rootlessly, which is CLAUDE.md rule 10's layout gap. E402 is ignored repo-wide for this idiom.
sys.path.insert(0, str(Path(__file__).resolve().parent / "swap_terminal"))

from regtest import adaptor_steps
from regtest import operator_panel as decisions
from regtest.console import Console
from regtest.daemons import RegtestSetupError
from regtest.harness_runner import HarnessRunner

#: THE BIND ADDRESS IS A CONSTANT AND THERE IS NO FLAG FOR IT. See the header.
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
REPO_ROOT = Path(__file__).resolve().parent


#: THE WHOLE PAGE, INLINE, WITH NO NETWORK OF ITS OWN. No CDN, no font, no framework. The panel
#: is served by a machine that may have no route to the internet and is used when something is
#: already going wrong; a page that needs a third party to render is a page that renders a blank
#: rectangle on the day it matters. It is also the reason the CSP above can be this narrow.
#:
#: SERVER-RENDERED STRUCTURE, FETCHED VALUES. Every region has its text before any script runs,
#: so a failed fetch leaves "asking..." rather than an empty box -- rule 14's "an empty result
#: must never print nothing", applied to a page instead of a terminal.
#:
#: THE `r` PREFIX IS LOAD-BEARING AND IS NOT STYLE. Without it Python reads the JavaScript's own
#: escapes: `r.lines.join("\n")` became a join on a REAL newline, which is an unterminated
#: string literal, which stops the whole inline script from parsing. The panel then served a
#: perfectly valid page whose every region sat at its placeholder text forever -- and the smoke
#: test that fetched the bytes and checked for element ids could not see it, because bytes are
#: not execution. `javascript_strings_are_closed()` below is the check that can.
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Operator Panel</title>
<style>
  :root { --bg:#faf9f7; --fg:#1a1917; --dim:#6b6763; --line:#ddd9d4; --ok:#1f7a3d; --bad:#b3261e; --warn:#8a6d00; --card:#fff; }
  @media (prefers-color-scheme: dark) { :root { --bg:#16151a; --fg:#eceaf0; --dim:#9b96a3; --line:#33313a; --ok:#6ee7a0; --bad:#ff9a92; --warn:#e8c55a; --card:#1e1d24; } }
  * { box-sizing:border-box; }
  body { margin:0; padding:16px; background:var(--bg); color:var(--fg);
         font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; }
  h1 { font-size:16px; margin:0 0 4px; letter-spacing:.02em; }
  .sub { color:var(--dim); margin:0 0 16px; }
  section { background:var(--card); border:1px solid var(--line); border-radius:8px;
            padding:12px 14px; margin:0 0 14px; }
  h2 { font-size:13px; margin:0 0 8px; text-transform:uppercase; letter-spacing:.06em; color:var(--dim); }
  table { border-collapse:collapse; width:100%; }
  td,th { text-align:left; padding:4px 8px 4px 0; border-bottom:1px solid var(--line);
          vertical-align:top; word-break:break-all; }
  th { color:var(--dim); font-weight:600; }
  .ok { color:var(--ok); } .bad { color:var(--bad); } .warn { color:var(--warn); }
  button { font:inherit; padding:7px 12px; margin:0 8px 8px 0; border:1px solid var(--line);
           border-radius:6px; background:var(--card); color:var(--fg); cursor:pointer; }
  button:hover:not(:disabled) { border-color:var(--fg); }
  button:disabled { opacity:.45; cursor:not-allowed; }
  button.stop { border-color:var(--bad); color:var(--bad); }
  nav { display:flex; flex-wrap:wrap; gap:6px; margin:0 0 14px; border-bottom:1px solid var(--line); padding-bottom:10px; }
  nav button { margin:0; }
  nav button.on { border-color:var(--fg); font-weight:700; }
  nav button .dot { font-size:11px; margin-left:6px; }
  pre { margin:0; padding:10px; background:var(--bg); border:1px solid var(--line);
        border-radius:6px; max-height:52vh; overflow:auto; white-space:pre-wrap; font-size:12.5px; }
  .what { color:var(--dim); font-size:12.5px; margin:-4px 0 10px; }
</style></head><body>
<h1>Operator Panel &mdash; swap terminal harnesses</h1>
<p class="sub">127.0.0.1 only, by construction. The GRC daemon said this is a test network before
this port was bound. The seed is never shown here and never leaves the server's environment.</p>

<nav id="tabs">loading the chain list from the server&hellip;</nav>

<section>
  <h2 id="chainname">Chain</h2>
  <div id="chain">pick a chain above&hellip;</div>
  <button id="refresh">Re-check this chain</button>
  <span class="what">reads the daemon, and on GRC walks blocks per payment &mdash; a few seconds, not on a timer</span>
</section>

<section>
  <h2>Run</h2>
  <div id="buttons">asking&hellip;</div>
  <button class="stop" id="stop">Stop the run</button>
</section>

<section>
  <h2>Output</h2>
  <div id="verdict" class="sub">nothing has been started from this panel yet</div>
  <pre id="out">(none)</pre>
</section>

<script>
const $ = id => document.getElementById(id);
let pinned = true, current = "";   // set from /api/state's chain list, never spelled here
$("out").addEventListener("scroll", () => {
  const el = $("out");
  pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
});

function esc(s) { return String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c])); }

function methodsTable(methods) {
  if (!methods || !methods.length) { return ""; }
  let h = "<table><tr><th>daemon has</th><th>&nbsp;</th><th>what its absence costs</th></tr>";
  for (const m of methods) {
    h += "<tr><td>" + esc(m.method) + '</td><td class="' + (m.present ? "ok" : "bad") + '">' +
         (m.present ? "yes" : "NO") + "</td><td>" + esc(m.matters) + "</td></tr>";
  }
  return h + "</table>";
}

function fundingBlock(f) {
  if (!f) { return ""; }
  let h = "<h2>Funding</h2>";
  if (f.address) {
    h += "<p>fund <b>" + esc(f.address) + "</b> with <b>" + esc(f.needed || "?") + " " + esc(f.asset || "") + "</b>, then wait for ONE confirmation</p>";
  }
  if (f.error) { h += '<p class="bad">' + esc(f.error) + "</p>"; }
  if (f.rows && f.rows.length) {
    h += "<table><tr><th>payment</th><th>value</th><th>conf</th><th>state</th></tr>";
    for (const r of f.rows) {
      h += "<tr><td>" + esc(r.txid) + "</td><td>" + esc(r.value) + "</td><td>" + esc(r.confirmations) +
           '</td><td class="' + (r.usable ? "ok" : "bad") + '">' + (r.usable ? "USABLE" : "spent") +
           '</td></tr><tr><td colspan="4" class="sub">' + esc(r.note) +
           (r.spender ? " &mdash; by " + esc(r.spender) : "") + "</td></tr>";
    }
    h += "</table>";
    if (!f.rows.some(r => r.usable)) {
      h += '<p class="bad">NO USABLE PAYMENT. Every one above is spent &mdash; each run consumes its funding by design. Send another and re-check.</p>';
    }
  } else if (!f.error) {
    h += '<p class="warn">(none) &mdash; the wallet remembers no payment to this address at all, which is the ordinary state before you have funded it.</p>';
  }
  return h;
}

async function loadChain(asset) {
  current = asset;
  for (const b of $("tabs").querySelectorAll("button")) {
    b.className = b.dataset.asset === asset ? "on" : "";
  }
  $("chainname").textContent = asset;
  $("chain").textContent = "asking " + asset + "…";
  let d;
  try { d = await (await fetch("/api/chain/" + encodeURIComponent(asset))).json(); }
  catch (e) { $("chain").innerHTML = '<span class="bad">could not reach the panel: ' + esc(e) + "</span>"; return; }
  let h = '<p class="sub">' + esc(d.note || "") + "</p>";
  if (d.kind === "none") {
    h += '<p class="warn">Not probed by this panel.</p>';
  } else if (d.reachable) {
    const netClass = d.network === "MAINNET" ? "bad" : (d.network === "unknown" ? "warn" : "ok");
    h += '<p><span class="ok">REACHABLE</span> at ' + esc(d.endpoint || "?") +
         ' &mdash; height ' + esc(d.height) + ', the daemon says <span class="' + netClass + '">' +
         esc(d.network) + "</span></p>";
    h += methodsTable(d.methods);
    h += fundingBlock(d.funding);
  } else {
    h += '<p class="bad">NOT REACHABLE' + (d.endpoint ? " at " + esc(d.endpoint) : "") + "</p>";
    if (d.error) { h += '<p class="sub">' + esc(d.error) + "</p>"; }
    if (d.kind === "regtest") {
      h += '<p class="sub">That is the ordinary state: this daemon only exists while regtest_htlc_verify.py is running, and this panel never starts one.</p>';
    }
  }
  $("chain").innerHTML = h;
}

async function tick() {
  let d;
  try { d = await (await fetch("/api/state")).json(); }
  catch (e) { $("verdict").innerHTML = '<span class="bad">the panel stopped answering: ' + esc(e) + "</span>"; return; }
  const r = d.run;
  $("verdict").innerHTML = '<span class="' + (r.alive ? "warn" : (r.exit_code === 0 ? "ok" : (r.exit_code === null ? "" : "bad"))) + '">' + esc(r.verdict) + "</span>" +
    (r.dropped ? ' <span class="bad">(' + r.dropped + " further lines were DROPPED at the line cap)</span>" : "");
  $("out").textContent = r.lines.length ? r.lines.join("\n") : "(none)";
  if (pinned) { $("out").scrollTop = $("out").scrollHeight; }
  if (!$("tabs").dataset.built) {
    $("tabs").innerHTML = d.chains.map(c =>
      '<button data-asset="' + esc(c.asset) + '">' + esc(c.asset) +
      '<span class="dot">' + (c.kind === "operator" ? "●" : (c.kind === "regtest" ? "○" : "·")) +
      "</span></button>").join("");
    $("tabs").dataset.built = "1";
    for (const b of $("tabs").querySelectorAll("button")) {
      b.onclick = () => loadChain(b.dataset.asset);
    }
    loadChain(current || d.chains[0].asset);
  }
  if (!$("buttons").dataset.built) {
    $("buttons").innerHTML = d.runnable.map(x =>
      '<div><button data-key="' + esc(x.key) + '">' + esc(x.key) + '</button><span class="what">' + esc(x.what) + "</span></div>").join("");
    $("buttons").dataset.built = "1";
    for (const b of $("buttons").querySelectorAll("button")) {
      b.onclick = async () => {
        const res = await (await fetch("/api/run", {method:"POST", headers:{"Content-Type":"application/json"},
                                                    body: JSON.stringify({key: b.dataset.key})})).json();
        if (res.error) { alert(res.error); }
        tick();
      };
    }
  }
  for (const b of $("buttons").querySelectorAll("button")) { b.disabled = r.alive; }
  $("stop").disabled = !r.alive;
}

$("refresh").onclick = () => loadChain(current);
$("stop").onclick = async () => {
  const res = await (await fetch("/api/stop", {method:"POST"})).json();
  alert(res.said);
  tick();
};
tick();
setInterval(tick, 1000);
</script></body></html>"""


def javascript_strings_are_closed(page: str) -> str:
    """"" if every JS string literal in the page closes on its own line, else the offending line.

    THE DEFECT THIS EXISTS FOR, 2026-09-28. `PAGE` was a plain triple-quoted string, so Python
    read the JavaScript's own escapes and turned `r.lines.join("\\n")` into a join on a REAL
    newline. That is an unterminated string literal; the browser refuses the whole inline script;
    and the panel then served a perfectly valid page whose every region sat at its placeholder
    text forever. No error anywhere -- the server logged a 200, the HTML was complete, and the
    operator saw "asking the daemon..." that never became anything.

    WHY THE EXISTING TEST COULD NOT SEE IT. It fetched the page over a real socket and asserted
    the element ids were present. They were. Bytes are not execution, and a check that reads
    bytes can only ever prove the bytes. This is the cheapest check that is about the SCRIPT.

    WHAT IT IS AND IS NOT. A line scanner, not a JavaScript parser: it tracks single and double
    quotes, honours backslash escapes, and stops at a `//` comment. It does NOT understand
    template literals, regex literals containing quotes, or a string deliberately continued with
    a trailing backslash -- so it would false-positive on those, and the page must not use them.
    That is a real constraint and it is stated rather than discovered: this page is small and
    hand-written, and a checker that is exactly right about the subset in use beats a parser
    dependency nobody would install to serve three routes.
    """
    inside = page.split("<script>", 1)[-1].split("</script>", 1)[0]
    for number, line in enumerate(inside.splitlines(), 1):
        quote = ""
        escaped = False
        for index, character in enumerate(line):
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
            elif quote:
                if character == quote:
                    quote = ""
            elif character in "\"'":
                quote = character
            elif character == "/" and line[index + 1:index + 2] == "/":
                break
        if quote:
            return f"line {number} of the page's script ends inside a {quote} string: {line.strip()}"
    return ""


def state_payload(run: adaptor_steps.Run, runner: HarnessRunner) -> dict:
    """Everything the page shows, as one JSON-able dict. The only decision here is what to ask.

    ORDERED SO THE SLOW PART IS LAST. The run state and the method probe are instant; the
    payment rows walk blocks and can take several seconds per payment. A reader watching a
    ten-minute harness cares about the output far more often than about the funding list, so
    the funding list is what the page refreshes on demand rather than on a timer.
    """
    live = runner.state()
    payload = {
        "run": {
            "name": live.name, "alive": live.alive, "started": live.started,
            "exit_code": live.exit_code, "lines": live.lines, "dropped": live.dropped,
            "verdict": live.verdict,
        },
        "runnable": [{"key": key, "what": what} for key, (what, _) in decisions.RUNNABLE.items()],
        # THE NAV IS BUILT FROM THE SERVER'S LIST, never hard-coded in the page. A chain added
        # to decisions.CHAINS and not to the page would be a chain that exists in one half of
        # this file and not the other -- rule 8's duplicate with a delay on it, in HTML.
        "chains": [{"asset": c.asset, "kind": c.kind} for c in decisions.CHAINS],
    }
    return payload


def funding_payload(run: adaptor_steps.Run, known_spent: dict | None = None) -> dict:
    """The funding picture: the address, what the daemon can be asked, and every payment.

    A REFUSAL IS A RESULT HERE, not an exception that blanks the page. A daemon that cannot be
    read leaves `error` set and the page says so where the table would be -- rule 14's "(none)
    is a result", applied to the failure as well as the empty case.
    """
    key = adaptor_steps.operator_funding_key(run)
    if key is None:
        return {"error": f"{adaptor_steps.FUNDING_SEED_VARIABLE} is not set in the environment "
                         f"of the process serving this page, so no funding address can be "
                         f"derived. Export it and restart the panel.", "rows": [], "methods": []}
    methods = [{"method": m.method, "present": m.present, "matters": m.matters}
               for m in decisions.probe_methods(run)]
    try:
        rows = decisions.payment_rows(run, key, known_spent)
    except RegtestSetupError as error:
        return {"address": key.address, "error": str(error), "rows": [], "methods": methods}
    return {
        "address": key.address,
        "needed": adaptor_steps.funding_needed_coins(run),
        "asset": run.asset,
        "methods": methods,
        "error": "",
        "rows": [{"txid": r.txid, "confirmations": r.confirmations, "value": r.value_coins,
                  "spender": r.spender, "usable": r.usable, "note": r.note} for r in rows],
    }


def guarded(answer, path: str, *rest) -> tuple[bytes, str, int]:
    """Run a route and return its answer, turning ANY failure into a readable 500.

    THE DEFECT, 2026-09-28: the funding region read "could not reach the panel: TypeError:
    Failed to fetch" while the rest of the page kept polling happily. That message is the
    browser's, and it is all a browser CAN say -- a handler that raises inside
    BaseHTTPRequestHandler never writes a status line, so the connection closes with no response
    and every reason for it stays on the server. The one thing the operator needed, which route
    and which exception, was the one thing that could not reach them.

    A BROAD CATCH, AND THIS IS THE CASE RULE 12 ALLOWS. "A broad catch is legitimate when a
    diagnostic must not die on a bad row. It is never legitimate when the caller cannot tell the
    failure from a real answer." Here the caller is told, loudly and specifically: the body IS
    the failure, carrying the route, the exception type and its message. Nothing is swallowed
    and nothing is mistaken for success -- the status is 500 and the page renders it in red.

    The exception TYPE is included because "RPCError" and "AttributeError" ask for completely
    different reactions from whoever reads it, and a bare message often names neither.
    """
    try:
        return answer(path, *rest)
    except Exception as error:  # noqa: BLE001 -- checked: the failure IS the return value, as a 500 naming the route and the exception type
        detail = f"{type(error).__name__}: {error}"
        return (
            json.dumps({"error": f"{path} failed on the server -- {detail}", "rows": [],
                        "methods": []}).encode(),
            "application/json",
            500,
        )


def chain_payload(asset: str, grc_run: adaptor_steps.Run, known_spent: dict | None = None) -> dict:
    """One tab's contents, by asset. An unknown asset is a 200 saying so, not a 404.

    NOT A 404, and that is deliberate. The browser only ever asks for an asset the page itself
    listed, so a 404 here would mean the page and the server disagree about what exists -- and
    the operator would see a fetch failure with no text. A named refusal in the tab's own body
    puts the disagreement where it can be read.

    THE GRC TAB REUSES THE RUN THIS PROCESS ALREADY GATED. Its network was asked three ways
    before the port was bound, so rebuilding it here would be a second, ungated connection to
    the one chain that matters -- and the funding walk is the expensive part, which belongs to
    the tab the operator actually funds.
    """
    tab = next((c for c in decisions.CHAINS if c.asset == asset), None)
    if tab is None:
        return {"asset": asset, "kind": "none", "reachable": False, "methods": [],
                "note": f"{asset!r} is not a chain this panel knows about.", "error": "",
                "funding": None}
    state = decisions.chain_state(tab, grc_run.console)
    if asset == "GRC" and state["reachable"]:
        state["funding"] = funding_payload(grc_run, known_spent)
    return state


def answer_a_get(path: str, run: adaptor_steps.Run, runner: HarnessRunner, page: str,
                 known_spent: dict | None = None) -> tuple[bytes, str, int]:
    """Which of the three GETs this is, and its bytes. The routing decision, out of the handler.

    THREE ROUTES AND A 404, and it is a function rather than a chain of `elif` inside
    BaseHTTPRequestHandler for the reason rule 10 gives: a decision reachable only by starting a
    server and making a request is a decision that gets tested by starting a server and making a
    request, which nobody does. This one is called with a string.

    It also keeps `build_handler` under ruff's complexity ceiling honestly -- by moving what
    decides, rather than by raising the ceiling (rule 12).
    """
    if path in ("/", "/index.html"):
        return page.encode(), "text/html; charset=utf-8", 200
    if path == "/api/state":
        return json.dumps(state_payload(run, runner)).encode(), "application/json", 200
    if path == "/api/funding":
        return json.dumps(funding_payload(run, known_spent)).encode(), "application/json", 200
    if path.startswith("/api/chain/"):
        return json.dumps(chain_payload(path.rsplit("/", 1)[-1], run, known_spent)).encode(), "application/json", 200
    return json.dumps({"error": f"no such route: {path}"}).encode(), "application/json", 404


def answer_a_post(path: str, raw: bytes, runner: HarnessRunner) -> tuple[bytes, str, int]:
    """The two POSTs, as a function of a path and a body. No socket, no handler, no server.

    SAME ARGUMENT AS answer_a_get, and the same shape so `guarded` can wrap either: a decision
    reachable only by making a request is a decision nobody tests (rule 10). Here it means a
    test can post a malformed body, or a body for a route that does not exist, by calling this.
    """
    try:
        body = json.loads(raw)
    except ValueError:
        return json.dumps({"error": "the request body was not JSON"}).encode(), "application/json", 400
    if path == "/api/stop":
        return json.dumps({"said": runner.stop()}).encode(), "application/json", 200
    if path != "/api/run":
        return json.dumps({"error": f"no such route: {path}"}).encode(), "application/json", 404
    answer, code = start_named_run(runner, body)
    return json.dumps(answer).encode(), "application/json", code


def start_named_run(runner: HarnessRunner, body: object) -> tuple[dict, int]:
    """Turn a request body into a started run, or a refusal. THE ALLOWLIST LIVES HERE.

    EXTRACTED FROM THE HANDLER because ruff put `build_handler` at 13 against a ceiling of 10,
    and rule 12 is explicit that the answer is to pull the decision out rather than raise the
    ceiling: "a main() past the ceiling is orchestration that has swallowed decisions". This is
    the decision -- may this run start, and as what -- and pulling it out means a test can hand
    it a hostile body with no HTTP server anywhere near it (rule 10).

    `key` is LOOKED UP and never used to build anything. It is not joined to a path, not split
    into arguments, and not passed to a shell; `subprocess` is called with the list stored in
    RUNNABLE. A key that is not in the table returns a refusal and spawns nothing, so the worst
    a crafted request achieves is a 400.
    """
    key = body.get("key") if isinstance(body, dict) else None
    entry = decisions.RUNNABLE.get(key) if isinstance(key, str) else None
    if entry is None:
        return {"error": f"{key!r} is not a run this panel offers"}, 400
    refusal = runner.start(key, list(entry[1]), str(REPO_ROOT))
    return ({"error": refusal}, 409) if refusal else ({"started": key}, 200)


def build_handler(run: adaptor_steps.Run, runner: HarnessRunner, page: str,
                  known_spent: dict | None = None):
    """The HTTP surface, closed over the objects it serves. Four routes and no others.

    A CLOSURE RATHER THAN CLASS ATTRIBUTES because BaseHTTPRequestHandler is instantiated per
    request by the server, so anything it needs has to be reachable without a constructor
    argument -- and module-level globals would make two panels in one process share a run.
    """

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            """Silence the default access log.

            NOT hiding anything: the panel prints what it does through Console, in the
            repository's own vocabulary. stderr access lines interleaved with a harness's
            streamed output make both unreadable, and rule 14 is about output a human can use.
            """

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # The page is served to one browser on one machine and embeds no third-party
            # anything; these say so rather than relying on it staying true.
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:
            body, content_type, code = guarded(answer_a_get, self.path, run, runner, page, known_spent)
            self._send(code, body, content_type)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) or b"{}"
            body, content_type, code = guarded(answer_a_post, self.path, raw, runner)
            self._send(code, body, content_type)

    return Handler


def assert_loopback_only(host: str) -> None:
    """Refuse anything but the loopback address. There is no flag; this guards against edits.

    A CONSTANT CAN BE CHANGED BY THE NEXT PERSON, and a page with buttons that spend money is
    exactly where "someone will set it to 0.0.0.0 to demo it" happens. This makes that an
    explicit refusal at startup rather than a quiet publication of the controls.
    """
    if host != "127.0.0.1":
        raise RegtestSetupError(
            f"this panel refuses to bind {host}. It has buttons that spend coin and nothing "
            f"authenticates a caller, so it serves the loopback address only. If you need it "
            f"from another machine, forward the port over ssh -- that way the authentication "
            f"is ssh's, which is a real one."
        )


def main(argv: list[str], console: Console | None = None) -> int:
    parser = argparse.ArgumentParser(description="The operator's control panel for the GRC testnet harnesses.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"default {DEFAULT_PORT}")
    args = parser.parse_args(argv)
    console = console or Console(2)
    console.banner("OPERATOR PANEL -- Gridcoin testnet harnesses")
    try:
        assert_loopback_only(HOST)
        config = adaptor_steps.resolve_config("GRC")
        run = adaptor_steps.Run(console=console, config=config, wallet="")
        console.say(f"GRC: endpoint {config.base_url}  datadir {config.datadir}")
        adaptor_steps.step_1_reachable(run)
        adaptor_steps.assert_test_network(run)
    except RegtestSetupError as exc:
        console.banner("REFUSED BEFORE THE PORT WAS BOUND")
        console.say(str(exc))
        return 1

    runner = HarnessRunner()
    # ONE CACHE FOR THE PROCESS, held here rather than at module level so a test builds its own
    # and two panels in one process could not share one. It only ever holds "this outpoint was
    # spent by that transaction", which is true forever once true.
    server = ThreadingHTTPServer((HOST, args.port), build_handler(run, runner, PAGE, {}))
    console.say(f"the panel is at http://{HOST}:{args.port}/ -- loopback only, by construction")
    console.say(f"it can start: {', '.join(decisions.RUNNABLE)}")
    console.say("Ctrl-C stops the panel AND reaps any run it started (rule 13)")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        # THE REAPER, and it is the reason Ctrl-C is caught rather than left to the default
        # handler. A panel that exits while its child keeps running is rule 13's orphan, and
        # the child here holds a funding output mid-spend.
        console.say("")
        console.say(f"stopping: {runner.stop()}")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
