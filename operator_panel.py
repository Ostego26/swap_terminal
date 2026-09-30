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
shape is structural and asserted by tests/test_web_surfaces.py over the real URL map, and the
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

from regtest import daemons, funding_steps
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
  :root { --accent:#6b6763; --bg:#faf9f7; --fg:#1a1917; --dim:#6b6763; --line:#ddd9d4; --ok:#1f7a3d; --bad:#b3261e; --warn:#8a6d00; --card:#fff; }
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
  nav button.on { border-color:var(--accent); color:var(--accent); font-weight:700;
                  box-shadow:inset 0 -3px 0 var(--accent); }
  .chainhead { display:flex; align-items:center; gap:12px; margin:-2px 0 12px;
               padding:10px 12px; border-radius:8px; border:1px solid var(--line);
               background:linear-gradient(90deg, color-mix(in srgb, var(--accent) 14%, transparent), transparent); }
  .mark { width:40px; height:40px; flex:0 0 40px; }
  .mark svg { width:100%; height:100%; display:block; }
  .chainhead h3 { margin:0; font-size:15px; letter-spacing:.04em; }
  .chainhead .kind { color:var(--dim); font-size:12.5px; }
  .pill { display:inline-block; padding:1px 7px; border-radius:999px; font-size:11.5px;
          border:1px solid currentColor; margin-left:8px; }
  input, select { font:inherit; padding:6px 8px; border:1px solid var(--line); border-radius:6px;
                  background:var(--card); color:var(--fg); }
  input { width:min(420px, 60vw); }
  .row { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin:0 0 8px; }
  nav button .dot { font-size:11px; margin-left:6px; }
  nav button[data-up="yes"] .dot { color:var(--ok); }
  nav button[data-up="no"]  .dot { color:var(--bad); }
  nav button[data-up=""]    .dot { color:var(--dim); }
  nav button[data-up="cfg"] .dot { color:var(--warn); }
  pre { margin:0; padding:10px; background:var(--bg); border:1px solid var(--line);
        border-radius:6px; max-height:52vh; overflow:auto; white-space:pre-wrap; font-size:12.5px; }
  .what { color:var(--dim); font-size:12.5px; margin:-4px 0 10px; }
</style></head><body>
<h1>Operator Panel &mdash; swap terminal harnesses</h1>
<p class="sub">127.0.0.1 only, by construction. The GRC daemon said this is a test network before
this port was bound. The seed is never shown here and never leaves the server's environment.</p>

<nav id="tabs">loading the chain list from the server&hellip;</nav>
<p class="what" style="margin-top:-8px">A dot is <span class="ok">green</span> when that daemon
answered the LAST time this panel asked, <span class="bad">red</span> when it did not, and grey
when nobody has asked yet &mdash; which is not the same as down. <span class="warn">Amber</span>
is a chain this panel cannot speak to but which IS configured: its own check is the button.
<button id="checkall" style="margin-left:8px">Check every chain</button></p>

<section>
  <div class="chainhead"><div class="mark" id="mark"></div>
    <div><h3 id="chainname">Chain</h3><div class="kind" id="chainkind">&nbsp;</div></div></div>
  <div id="chain">pick a chain above&hellip;</div>
  <div class="row" id="switch"></div>
  <button id="refresh">Re-check this chain</button>
  <span class="what">reads the daemon, and on GRC walks blocks per payment &mdash; a few seconds, not on a timer</span>
</section>

<section id="rpcsection">
  <h2>RPC console &mdash; read-only</h2>
  <p class="warn" id="rpcwhynot" style="display:none"></p>
  <div class="row" id="rpcrow">
    <select id="method"></select>
    <input id="rpcargs" placeholder='arguments as JSON, e.g. ["txid", true] &mdash; blank for none'>
    <input id="rpcaccount" placeholder="r-address &mdash; blank uses XRP_DEPOSIT_ACCOUNT" style="display:none">
    <button id="callrpc">Call</button>
  </div>
  <p class="what">Every method offered here READS. Nothing on this page can create a
  transaction, sign one, touch a wallet lock, or ask for a passphrase &mdash; the allowlist is
  enforced on the server, not in this dropdown.</p>
  <pre id="rpcout">(nothing called yet)</pre>
</section>

<section id="swappersection">
  <h2>The swapper</h2>
  <!-- ONE OPERATOR SURFACE, 2026-09-30. These regions are the Flask app's /admin, rendered
       here instead: the SAME services/admin_view.overview() assembly, so the two cannot
       disagree. What is NOT shared is the posture -- this server binds loopback as a constant
       rather than a default, refuses to start unless the daemon says testnet, and takes no
       path, argument or flag from a request.

       SERVER-RENDERED PLACEHOLDERS, FETCHED VALUES, like every other region here: the text is
       in the markup before any script runs, so a failed fetch leaves a sentence rather than an
       empty box (rule 14). -->
  <div id="swapperstate" class="sub">asking the server for swap state&hellip;</div>

  <h3>Workers</h3>
  <p class="what">These three ARE the swapper. Nothing polls a chain, credits a deposit or pays
  anybody out while they are stopped &mdash; and every HTTP response still says 200, which is
  why each row prints what its own absence costs rather than a shared warning. They are
  <strong>supervisor.py's</strong> processes and they have pid files, which is the handle the
  daemon switches above do not have: a stop here is proven by polling for the process's
  ABSENCE, and a recycled pid is detected and NOT signalled.</p>
  <div id="workers">asking&hellip;</div>

  <h3>Swaps, payouts and inventory</h3>
  <pre id="swapperout">(nothing fetched yet)</pre>
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
const THEMES = {}, KINDS = {};

// THE COIN MARKS, AS INLINE SVG. No image file and no network: the page has to render on a
// machine with no route to the internet, which is exactly when an operator opens it.
//
// GRIDCOIN'S IS THE REAL ONE, PATH FOR PATH -- all three <path> elements of
// src/qt/res/images/gridcoin.svg in the Gridcoin wallet's own MIT-licensed source, with its
// gradient stops (#753eef -> #3c1b7b). It was a hexagon with a letter "G" set in it until the
// operator pointed out that Gridcoin's symbol is a G WITH A LINE THROUGH IT -- which is what
// that middle path draws, and which a typed letter cannot be. Drawing an approximation of a
// logo whose exact source was already checked out in this container was the lazy half of
// "recognisable rather than official". The others are DRAWN here,
// faithfully but not lifted: a disc in the published brand color carrying the currency's
// letterform, which is what each of those logos is. They are recognisable rather than official,
// and saying so beats implying this repository ships anyone's trademark files.
const MARKS = {
  GRC: '<svg viewBox="0 0 500 500"><defs><linearGradient id="grcg" x1="250" y1="4.4" x2="250" y2="501" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#753eef"/><stop offset="1" stop-color="#3c1b7b"/></linearGradient></defs><path fill="#FFF" d="M36 126L250 3 464 126 464 374 250 497 36 374z"/><path fill="url(#grcg)" d="M342.4571533,82.0932159 c-12.408783,10.3809509-26.0314331,19.7608719-40.1398315,28.4434967 c-13.3031616,8.1872635-26.9657898,15.8179779-40.2628479,23.2341766 c-4.1065063,2.2906342-8.1234894,4.545517-12.0531769,6.7746277 c-60.6791687,34.4177551-99.7171478,62.3633118-99.7171478,109.4564972 c0,39.7861938,28.00354,65.7119751,73.3935699,93.4388428c5.184845,3.1673279,10.6189728,6.3630066,16.2405548,9.6005554 c-1.9994812,1.1159973-3.9625549,2.2197876-6.0084076,3.3525391 c-13.6059265,7.5360107-27.6746826,15.3290405-41.6046295,23.8873596 c-1.3804321,0.8483582-2.7618256,1.7079773-4.1429138,2.5733948c-4.9149323-3.2008057-9.7763977-6.464447-14.4964752-9.8739929 c-39.9571991-28.8599548-71.5206833-65.9264832-71.5206833-122.9786987 c0-68.0834351,44.9352646-108.0003815,95.539772-139.4653015 c14.1238251-8.7821426,28.6885986-16.9062805,42.8417358-24.7998123 c3.2188721-1.7952652,6.3662262-3.5631638,9.4746246-5.3181763 c16.2530975-9.1776581,30.8288116-17.8519058,43.64534-26.5065079L250,28.713028l-43.6598206,25.2069168 c5.4856567,3.7084274,11.2283325,7.419426,17.3894043,11.173912 c5.1780701,3.1550903,10.6041412,6.3456116,16.2154236,9.5825119c-1.3984833,0.7842636-2.7663422,1.557579-4.1902618,2.3515015 c-14.2117615,7.9267044-28.9079285,16.1226654-43.2446594,25.0375061 c-1.3884888,0.8631744-2.7357483,1.7218323-4.0875244,2.5814667c-4.9133148-3.1815033-9.7754364-6.423233-14.5000153-9.8047485 c-5.6724701-4.059494-11.1181946-8.3292999-16.3871002-12.7446899l-99.1757202,57.2589951v221.2870636l98.5814972,56.9159851 c12.4892731-10.5368347,26.2388611-20.020813,40.4944153-28.7791138 c13.6072235-8.3598938,27.6054535-16.1217041,41.2207184-23.6631775 c3.8627014-2.1395874,7.6390839-4.2460022,11.3449402-6.3311462 c29.0390167-16.3413391,53.1170502-31.2046814,70.3386383-47.4100952H217.1192169l30.946701-53.7019653h101.2637634h48.3248291 c-1.0419312,20.3467407-6.2425537,38.0404968-14.3995361,53.7019653 c-17.2377014,33.0959167-47.750885,57.0625-80.6883545,77.4044189 c-14.0143433,8.6552429-28.4612122,16.6592407-42.5016174,24.4361877 c-3.4214478,1.8954163-6.7665863,3.7612305-10.0637054,5.6125488 c-16.5384674,9.285553-31.3435059,18.0715637-44.3159027,26.8727112L250,471.2868347l44.3294373-25.5934143 c-5.6479797-3.8363037-11.5762024-7.6700134-17.9485474-11.5430298 c-5.197052-3.1586304-10.64505-6.3488159-16.2801514-9.5834656c1.571106-0.8757324,3.1096802-1.7402039,4.7113953-2.6271973 c14.0916138-7.805603,28.663147-15.8772583,42.904541-24.6722717c1.3601379-0.8399963,2.748291-1.7134705,4.1361389-2.5875854 c4.8911133,3.1663513,9.7300415,6.3935852,14.4304504,9.7628784 c5.8161316,4.1686707,11.3942261,8.5618286,16.781311,13.1131592l98.5753784-56.9124451V139.3563995L342.4571533,82.0932159z M347.3257141,230.8228302c-7.64328-29.6464691-33.3977356-51.6412811-71.1728516-74.8993073 c-5.1552124-3.1740875-10.5493774-6.3762054-16.1303711-9.6195526 c2.2609863-1.2718811,4.4859009-2.5312195,6.8035889-3.8240509 c12.6351929-7.0467682,26.9551697-15.0340271,40.6280823-23.4483566 c1.3669128-0.8412704,2.7344666-1.6938171,4.1020203-2.5511932c4.8988342,3.2111359,9.743866,6.4847488,14.4491272,9.9010468 c35.446167,25.7380295,64.2961121,57.873848,70.5473633,104.4414139H347.3257141z"/><path fill="url(#grcg)" d="M249.9994812,500L33.4943619,374.9989624V125L249.9994812,0l216.5061646,125 v249.9989624L249.9994812,500z M43.9522095,368.9616089l206.0472717,118.9616394l206.0483093-118.9616394V131.0373535 L249.9994812,12.0767651L43.9522095,131.0373535V368.9616089z"/></svg>',
  BTC: '<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="31" fill="#f7931a"/><text x="32" y="46" font-size="40" font-weight="700" fill="#fff" text-anchor="middle" font-family="ui-monospace,monospace" transform="rotate(-14 32 32)">\u20bf</text></svg>',
  LTC: '<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="31" fill="#345d9d"/><text x="32" y="45" font-size="38" font-weight="700" fill="#fff" text-anchor="middle" font-family="ui-monospace,monospace">\u0141</text></svg>',
  XRP: '<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="31" fill="#23292f"/><path fill="#fff" d="M20 20h7l5 6 5-6h7l-8.5 10L44 40h-7l-5-6-5 6h-7l8.5-10z"/></svg>',
  SOL: '<svg viewBox="0 0 64 64"><defs><linearGradient id="s" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#9945ff"/><stop offset="1" stop-color="#14f195"/></linearGradient></defs><circle cx="32" cy="32" r="31" fill="#131316"/><g fill="url(#s)"><path d="M18 24l4-4h24l-4 4z"/><path d="M18 34l4-4h24l-4 4z"/><path d="M18 44l4-4h24l-4 4z"/></g></svg>',
  _: '<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="31" fill="#6b6763"/><text x="32" y="45" font-size="34" font-weight="700" fill="#fff" text-anchor="middle" font-family="ui-monospace,monospace">?</text></svg>',
};
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

function applyTheme(t) {
  if (!t) { return; }
  // DARK GETS ITS OWN COLOR. Several brand colors are unreadable on a dark background -- XRP's
  // near-black is invisible on it -- and a theme that is unreadable half the time is worse than
  // none, because the reader stops looking at it.
  const dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.style.setProperty("--accent", dark ? t.dark : t.accent);
  $("mark").innerHTML = MARKS[t.asset] || MARKS._;
}

async function loadChain(asset) {
  current = asset;
  for (const b of $("tabs").querySelectorAll("button")) {
    b.className = b.dataset.asset === asset ? "on" : "";
    if (b.dataset.asset === asset) { applyTheme(THEMES[asset]); }
  }
  $("chainname").textContent = asset;
  $("chainkind").textContent = KINDS[asset] === "operator" ? "your own daemon \u2014 read only"
    : (KINDS[asset] === "regtest" ? "a regtest daemon another harness starts" : "no adapter here");
  $("chain").textContent = "asking " + asset + "…";
  let d;
  try { d = await (await fetch("/api/chain/" + encodeURIComponent(asset))).json(); }
  catch (e) { $("chain").innerHTML = '<span class="bad">could not reach the panel: ' + esc(e) + "</span>"; return; }
  let h = '<p class="sub">' + esc(d.note || "") + "</p>";
  if (d.kind === "foreign") {
    // CONFIGURED OR NOT IS THE QUESTION AN OPERATOR CAN ACT ON. "Not probed by this panel" was
    // accurate and useless -- a statement about this panel dressed as one about the chain.
    if (d.configured) {
      h += '<p class="ok">CONFIGURED &mdash; ' + esc((d.env || []).join(", ")) +
           " is set in the environment this panel was started with, so the button below has " +
           "somewhere to connect to. Whether it ANSWERS is what pressing it finds out.</p>";
    } else {
      // THE SENTENCE IS THE SERVER'S (decisions.why_foreign_is_unconfigured). It was
      // assembled here until 2026-09-30, when the XRP console needed the same words in
      // Python -- and two copies of one sentence is rule 8's defect at its quietest, because
      // nothing ever compares two pieces of prose.
      h += '<p class="bad">NOT CONFIGURED &mdash; ' + esc(d.unconfigured_why || "") + "</p>";
    }
    // THE SENTENCE THAT USED TO BE HERE SAID WHAT `d.note` ALREADY SAYS, four lines above it
    // on the same tab, and both ended by pointing at the same button. The operator pasted the
    // tab of a foreign chain on 2026-09-28 with the protocol statement in it twice -- once
    // per chain and accurate, once generic
    // and redundant. Rule 8 is about two copies of a RULE; this is two copies of a sentence,
    // and the cheaper half of the same defect: the generic one is deleted and the per-chain
    // one, which is the one that tells the operator something specific, survives.
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
  // A CONSOLE THAT CAN ONLY REFUSE IS WORSE THAN NO CONSOLE. The operator pressed Call on the
  // tab of a foreign chain on 2026-09-28 and got "REFUSED BY THIS PANEL" -- a dropdown of
  // twenty-five methods
  // was offered for a chain that has none of them. Offering a control that cannot work, and
  // explaining afterwards, is the shape rule 14 calls a defect in the output.
  $("rpcwhynot").textContent = d.console || "";
  $("rpcwhynot").style.display = d.console ? "" : "none";
  $("rpcrow").style.display = d.console ? "none" : "";
  $("rpcout").textContent = d.console ? "(no console on this tab)" : "(nothing called yet)";
  // THE ACCOUNT BOX EXISTS ONLY WHERE AN ACCOUNT IS A THING. rippled has no wallet, so five of
  // the mapped reads are ABOUT AN ACCOUNT and need one named; bitcoind's equivalents read a
  // wallet the daemon already holds. A box shown on a bitcoin tab would be a control that
  // cannot do anything, which is the defect the comment above this one is about.
  const xrpl = d.protocol === "xrpl";
  $("rpcaccount").style.display = xrpl ? "" : "none";
  if (xrpl) {
    $("rpcargs").placeholder = "one argument, e.g. a ledger index or a tx hash \u2014 blank for none";
  } else {
    $("rpcargs").placeholder = 'arguments as JSON, e.g. ["txid", true] \u2014 blank for none';
  }
  // THE SWITCH SAYS WHY IT IS OFF, rather than being absent or greyed with no reason. Three
  // different refusals live behind these buttons and they are not interchangeable: one says
  // this panel does not know your command line, one says an environment variable arms it, one
  // says there is no lifecycle here at all. A disabled button with no text teaches none of them.
  const c = d.control || {};
  $("switch").innerHTML = ["start", "stop"].map(a => {
    const why = c[a];
    const label = a === "start" ? "Start daemon" : "Stop daemon";
    return '<button data-action="' + a + '"' + (why ? " disabled" : "") + ' class="' +
           (a === "stop" ? "stop" : "") + '">' + label + "</button>" +
           (why ? '<span class="what">' + esc(why) + "</span>" : "");
  }).join("");
  for (const b of $("switch").querySelectorAll("button:not([disabled])")) {
    b.onclick = async () => {
      // A STOP IS CONFIRMED IN THE PAGE AS WELL AS ARMED IN THE SHELL. The environment
      // variable says this operator meant to have the button; this says they meant to press
      // it now. Neither replaces the other -- one defends against a page they did not open,
      // the other against a click they did not mean.
      if (b.dataset.action === "stop" &&
          !confirm("Stop the " + current + " daemon?\n\nOn GRC that stops staking until you " +
                   "start it again, and this panel cannot start it for you.")) { return; }
      const res = await (await fetch("/api/daemon", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({asset: current, action: b.dataset.action})})).json();
      alert(res.ok ? res.said : res.error);
      loadChain(current);
    };
  }
  // THE NAV REMEMBERS WHAT THIS TAB FOUND, so "which daemons are up" stops being a question
  // you answer by clicking six tabs. The dot says LAST TIME WE ASKED, never "is up" -- a stale
  // green is a lie an operator acts on, and grey for "nobody has asked" is its own answer
  // rather than an optimistic guess (rule 14).
  const tab = $("tabs").querySelector('[data-asset="' + asset + '"]');
  // THE SERVER DECIDES WHAT THE DOT SAYS (decisions.dot_state). This read `d.kind === "none"`,
  // a kind no tab has ever carried, so every foreign chain went RED for a probe this panel
  // never makes -- and the amber the legend promises had a CSS rule and no writer.
  if (tab) { tab.dataset.up = d.dot || ""; }
}

async function checkEveryChain() {
  const was = current;
  for (const b of $("tabs").querySelectorAll("button")) {
    await loadChain(b.dataset.asset);
  }
  await loadChain(was);
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
    for (const c of d.chains) { THEMES[c.asset] = c; KINDS[c.asset] = c.kind; }
    $("method").innerHTML = (d.rpcs || []).map(m => '<option>' + esc(m) + '</option>').join("");
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
  RUNS = d.runnable || {};
  // REBUILT ON EVERY TAB SWITCH, not once. This was guarded by a `built` flag, so the first
  // chain's buttons stayed on screen for all six tabs -- the operator's "it's all GRC controls
  // too, it doesn't switch per chain". A cached render of chain-specific controls is worse than
  // no render: the buttons under BTC would have spent GRC.
  const want = current + "|" + JSON.stringify(RUNS[current] || []);
  if ($("buttons").dataset.showing !== want) {
    const mine = RUNS[current] || [];
    $("buttons").innerHTML = mine.length
      ? mine.map(x => '<div><button data-key="' + esc(x.key) + '">' + esc(x.key) +
          '</button><span class="what">' + esc(x.what) + "</span></div>").join("")
      : '<p class="warn">(none) &mdash; this panel has no harness for ' + esc(current) +
        ". That is a statement about this panel, not about the chain.</p>";
    $("buttons").dataset.showing = want;
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

$("callrpc").onclick = async () => {
  let args = [];
  const typed = $("rpcargs").value.trim();
  if (typed) {
    try { args = JSON.parse(typed); }
    catch (e) { $("rpcout").textContent = "the arguments are not JSON: " + e; return; }
    if (!Array.isArray(args)) { args = [args]; }
  }
  $("rpcout").textContent = "calling " + $("method").value + " on " + current + "\u2026";
  let d;
  try {
    d = await (await fetch("/api/rpc", {method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({asset: current, method: $("method").value, args,
                            account: $("rpcaccount").value.trim()})})).json();
  } catch (e) { $("rpcout").textContent = "could not reach the panel: " + e; return; }
  // A REFUSAL AND A FAILURE READ DIFFERENTLY. One is a boundary this panel holds on purpose;
  // the other is the chain answering. Rendering both as "error" would make the deliberate one
  // look like a bug worth working around.
  //
  // AND ON THE XRP TAB THE TRANSLATION IS PART OF THE ANSWER. The operator asked for
  // getblockcount and rippled answered about ledger_closed; a figure whose method is not shown
  // cannot be checked, and the whole point of the map is that using it teaches what it maps to
  // (rule 14: echo the parameters that decide the answer).
  const head = [];
  if (d.translated) {
    head.push("asked " + $("method").value + "  \u2192  sent " + d.translated +
              (d.params && Object.keys(d.params).length ? " " + JSON.stringify(d.params) : ""));
  }
  if (d.answers) { head.push("the figure getblockcount-style callers want is at: " + d.answers); }
  if (d.account_used) { head.push("account: " + d.account_used); }
  if (d.extra_args_ignored) { head.push("IGNORED, this call takes one argument: " + JSON.stringify(d.extra_args_ignored)); }
  if (d.note) { head.push("", "HOW THE TWO DIFFER: " + d.note); }
  const prefix = head.length ? head.join("\n") + "\n\n" : "";
  if (d.ok) { $("rpcout").textContent = prefix + JSON.stringify(d.result, null, 2); }
  else if (d.needs) { $("rpcout").textContent = prefix + "THIS CALL NEEDS SOMETHING\n\n" + d.error; }
  else if (d.refused) { $("rpcout").textContent = prefix + "REFUSED BY THIS PANEL\n\n" + d.error; }
  else { $("rpcout").textContent = prefix + "the daemon answered:\n\n" + d.error; }
};

// THE SWAPPER REGION. One fetch, no chain and no price feed touched: overview() reads the
// database, the configuration and supervisor's pid files, and its pricing panel reads the price
// CACHE rather than fetching. That is what makes this safe to poll.
async function loadSwapper() {
  let d;
  try { d = await (await fetch("/api/swapper")).json(); }
  catch (e) { $("swapperstate").innerHTML = '<span class="bad">the panel stopped answering: ' + esc(e) + "</span>"; return; }
  if (!d.ok) {
    // ok=false is a REPORT, not an empty table. The region says it could not read and why,
    // because a blank area cannot be told apart from zero swaps (rule 14).
    $("swapperstate").innerHTML = '<span class="bad">swap state could not be read: ' + esc(d.error) + "</span>";
    $("workers").innerHTML = '<span class="bad">unknown -- the same read failed</span>';
    $("swapperout").textContent = "(not read)";
    return;
  }
  const o = d.overview;
  // status_counts IS A LIST OF ROWS, not a mapping. It is a GROUP BY's result --
  // {status, swaps} per row -- and reading it with Object.entries() printed
  // "0=[object Object]" on the operator's screen on 2026-09-30. templates/admin.html
  // reads row.status and row.swaps; so does this now, because they render one payload.
  const counts = (o.status_counts || []).map(sc => sc.status + "=" + sc.swaps);
  $("swapperstate").innerHTML =
    "database <code>" + esc(o.database) + "</code><br>read at " + esc(o.generated_at) +
    "<br>" + (counts.length ? esc(counts.join("  ")) : "<span class=\"what\">(none) no swap of any status is in the database</span>") +
    "<br>in flight: " + (o.in_flight || []).length +
    " &middot; payouts never reported sent: " +
    ((o.unresolved_payouts || []).length
      ? '<span class="bad">' + o.unresolved_payouts.length + "</span>"
      : '<span class="ok">0</span>');

  // ONE BUTTON PAIR PER WORKER, WITH ITS OWN COST BESIDE IT. The consequence text is
  // services/admin_view.worker_stopped_consequence()'s, carried on the row -- not written
  // here, and not one shared warning for all three, because what a stopped deposit_watcher
  // costs and what a stopped payout_worker costs are different facts.
  $("workers").innerHTML = (o.workers || []).map(w => {
    const running = w.state === "running";
    return '<div class="row"><strong>' + esc(w.worker) + "</strong> " +
      '<span class="' + (running ? "ok" : "bad") + '">' + esc((w.state || "unknown").toUpperCase()) + "</span> " +
      '<span class="what">pid ' + esc(w.pid === null || w.pid === undefined ? "(none)" : w.pid) + "</span> " +
      '<button data-worker="' + esc(w.worker) + '" data-action="start"' + (running ? " disabled" : "") + ">Start</button>" +
      '<button class="stop" data-worker="' + esc(w.worker) + '" data-action="stop"' + (running ? "" : " disabled") + ">Stop</button>" +
      '<div class="what">' + esc(w.stopped_consequence || "") + "</div></div>";
  }).join("") || '<span class="what">(none) supervisor.worker_commands() named no worker</span>';

  for (const b of $("workers").querySelectorAll("button:not([disabled])")) {
    b.onclick = async () => {
      // CONFIRMED IN THE PAGE for a stop, like the daemon switches. No environment variable
      // arms this one -- a worker holds no wallet lock and stakes nothing -- so the click is
      // the only deliberate act, and it should be one.
      if (b.dataset.action === "stop" &&
          !confirm("Stop " + b.dataset.worker + "?\n\n" +
                   (b.dataset.worker === "payout_worker"
                     ? "A stop can land between a broadcast and the row that records it, which is the " +
                       "window settle_payout.py exists for.\n\n" : "") +
                   "Nothing restarts it but this button.")) { return; }
      b.disabled = true;
      const res = await (await fetch("/api/worker", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({worker: b.dataset.worker, action: b.dataset.action})})).json();
      // SUPERVISOR'S OWN WORD, never a thumbs-up. already-running, not-running and
      // stale-pidfile are outcomes, not quiet successes, and `failed` means the operator asked
      // for something and did not get it (rule 13).
      alert(res.refused ? "REFUSED BY THIS PANEL\n\n" + res.error
            : res.error ? "the switch failed\n\n" + res.error
            : (res.ok ? "" : "THIS DID NOT HAPPEN: ") + res.result.worker + ": " + res.result.outcome +
              (res.result.pid ? " (pid " + res.result.pid + ")" : "") +
              (res.result.note ? "\n" + res.result.note : ""));
      loadSwapper();
    };
  }

  // THE FIELD NAMES ARE admin_view's, and all four of these were WRONG until
  // 2026-09-30: confirmed/available/freshness do not exist, and the panel printed
  // "confirmed undefined available undefined" beside a real balance. A second
  // renderer guessing at a payload's keys is rule 8 at its least visible -- nothing
  // fails, the page just says undefined. test_operator_panel.py now walks every
  // o.<field> this script reads against a real overview() payload.
  //
  // EIGHT DECIMALS, like templates/admin.html. A balance rendered as
  // 55.52645238097888 is a float's repr, not an amount of money, and the two
  // surfaces must not disagree about how much was paid.
  const amount = n => (typeof n === "number" ? n.toFixed(8) : String(n));
  const inv = (o.inventory || []).map(iv =>
    "  " + iv.asset + "  confirmed " + amount(iv.hot_confirmed) +
    "  reserved " + amount(iv.hot_reserved) + "  available " + amount(iv.hot_available) +
    "  " + (iv.fresh ? iv.fresh.state.toUpperCase() + " " + iv.fresh.age_display : "(no reading)")).join("\n");
  const pay = (o.payouts || []).slice(0, 8).map(po =>
    "  " + po.swap_id + "  " + po.asset + " " + amount(po.amount) + "  " + po.status +
    "  " + (po.txid || "(no txid)")).join("\n");
  const price = o.pricing && o.pricing.cached
    ? (o.pricing.assets || []).map(pa => "  " + pa.asset + "  $" + pa.price_usd + "  " + pa.turnover_verdict).join("\n") +
      "\n  priced by " + o.pricing.source + ", " + o.pricing.age + " ago"
    : "  (not fetched) nothing has been priced since this process started";
  $("swapperout").textContent =
    "HOT-WALLET INVENTORY\n" + (inv || "  (none) the reconcile worker has written no row") +
    "\n\nPAYOUTS (most recent 8)\n" + (pay || "  (none) no payout row exists") +
    "\n\nPRICING, out of the cache -- this page fetched nothing\n" + price;
}

$("checkall").onclick = checkEveryChain;
$("refresh").onclick = () => loadChain(current);
$("stop").onclick = async () => {
  const res = await (await fetch("/api/stop", {method:"POST"})).json();
  alert(res.said);
  tick();
};
tick();
setInterval(tick, 1000);
// THE SWAPPER REGION POLLS SLOWER THAN THE RUN OUTPUT, on purpose. /api/state is a run's
// stdout and wants to look live; this is a database read plus three pid-file reads, and once
// every five seconds is the difference between a freshness reading and a busy loop.
loadSwapper();
setInterval(loadSwapper, 5000);
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


def state_payload(run: funding_steps.Run, runner: HarnessRunner) -> dict:
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
        # PER CHAIN, not the whole table. See decisions.runs_for(): every tab rendering every
        # run is what made six tabs into one tab and five decorations.
        "runnable": {tab.asset: decisions.runs_for(tab.asset) for tab in decisions.CHAINS},
        # THE NAV IS BUILT FROM THE SERVER'S LIST, never hard-coded in the page. A chain added
        # to decisions.CHAINS and not to the page would be a chain that exists in one half of
        # this file and not the other -- rule 8's duplicate with a delay on it, in HTML.
        "chains": [{"asset": c.asset, "kind": c.kind, **decisions.theme_for(c.asset)}
                   for c in decisions.CHAINS],
        # THE DROPDOWN COMES FROM THE SERVER'S ALLOWLIST, so the page cannot offer a method the
        # server would refuse, and cannot fail to offer one it would allow. Spelled in the page
        # as well, the two would drift and the drift would look like a broken panel.
        "rpcs": list(decisions.READ_ONLY_RPCS),
    }
    return payload


def funding_memory() -> dict:
    """A fresh memory for the funding view: what is spent, and what was unspent when.

    TWO DICTS IN ONE OBJECT because they are two halves of one answer and a parallel
    parameter through five signatures is the shape that drifts -- somebody threads one and
    forgets the other, and the symptom is not a failure, it is a silent return to walking
    1092 blocks per page draw.

      spent           (txid, vout) -> the spender's txid. MONOTONE: a spend cannot be
                      undone, so this only ever turns a slow correct answer into a fast one.
      unspent_as_of   (txid, vout) -> the chain tip when a FULL-depth walk found no spender.
                      NOT an answer, a WATERMARK: see regtest/operator_panel.scan_depth().

    A function rather than a literal so the two key names exist in exactly one place; a
    dict spelled at the call site is two chances to typo a key that would read as an empty
    cache and never fail.
    """
    return {"spent": {}, "unspent_as_of": {}}


def funding_payload(run: funding_steps.Run, memory: dict | None = None) -> dict:
    """The funding picture: the address, what the daemon can be asked, and every payment.

    A REFUSAL IS A RESULT HERE, not an exception that blanks the page. A daemon that cannot be
    read leaves `error` set and the page says so where the table would be -- rule 14's "(none)
    is a result", applied to the failure as well as the empty case.
    """
    key = funding_steps.operator_funding_key(run)
    if key is None:
        return {"error": f"{funding_steps.FUNDING_SEED_VARIABLE} is not set in the environment "
                         f"of the process serving this page, so no funding address can be "
                         f"derived. Export it and restart the panel.", "rows": [], "methods": []}
    methods = [{"method": m.method, "present": m.present, "matters": m.matters}
               for m in decisions.probe_methods(run)]
    try:
        held = memory or funding_memory()
        rows = decisions.payment_rows(run, key, held["spent"], held["unspent_as_of"])
    except RegtestSetupError as error:
        return {"address": key.address, "error": str(error), "rows": [], "methods": methods}
    return {
        "address": key.address,
        "needed": funding_steps.funding_needed_coins(run),
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


def chain_payload(asset: str, grc_run: funding_steps.Run, memory: dict | None = None) -> dict:
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
        return {"asset": asset, "kind": "unknown", "reachable": False, "methods": [],
                "note": f"{asset!r} is not a chain this panel knows about.", "error": "",
                "funding": None, "theme": decisions.theme_for(asset),
                "dot": decisions.DOT_UNASKED,
                "console": f"{asset!r} is not a chain this panel knows about."}
    state = decisions.chain_state(tab, grc_run.console)
    state["theme"] = decisions.theme_for(asset)
    state["control"] = {a: decisions.refuse_daemon_control(tab, a) for a in ("start", "stop")}
    # BOTH OF THESE ARE DECISIONS AND NEITHER IS THE PAGE'S. What colour the nav dot is, and
    # whether this tab gets an RPC console, were both computed in JavaScript against
    # `kind === "none"` -- a value no tab carries -- so both branches were dead and both told
    # the operator something false on 2026-09-28. A decision a test can call is rule 10's whole
    # argument, and these two are now called with a dict in tests/test_operator_panel.py.
    state["dot"] = decisions.dot_state(state)
    state["console"] = decisions.refuse_an_rpc_console(tab)
    if asset == "GRC" and state["reachable"]:
        state["funding"] = funding_payload(grc_run, memory)
    return state


def answer_a_get(path: str, run: funding_steps.Run, runner: HarnessRunner, page: str,
                 memory: dict | None = None) -> tuple[bytes, str, int]:
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
        return json.dumps(funding_payload(run, memory)).encode(), "application/json", 200
    if path.startswith("/api/chain/"):
        return json.dumps(chain_payload(path.rsplit("/", 1)[-1], run, memory)).encode(), "application/json", 200
    if path == "/api/swapper":
        return json.dumps(swapper_payload()).encode(), "application/json", 200
    return json.dumps({"error": f"no such route: {path}"}).encode(), "application/json", 404


def answer_a_post(path: str, raw: bytes, runner: HarnessRunner,
                  chains: dict | None = None) -> tuple[bytes, str, int]:
    """The two POSTs, as a function of a path and a body. No socket, no handler, no server.

    SAME ARGUMENT AS answer_a_get, and the same shape so `guarded` can wrap either: a decision
    reachable only by making a request is a decision nobody tests (rule 10). Here it means a
    test can post a malformed body, or a body for a route that does not exist, by calling this.
    """
    try:
        body = json.loads(raw)
    except ValueError:
        return json.dumps({"error": "the request body was not JSON"}).encode(), "application/json", 400
    # A TABLE, NOT A CHAIN OF `if`. It was a chain until the worker route made it six deep and
    # ruff's PLR0911 fired, which is rule 12's reading of that code exactly: a dispatch that has
    # swallowed a decision per branch. Extracting it is the fix; raising the ceiling would not
    # be. Every value takes the parsed body and returns (payload, status), so a new route cannot
    # invent its own calling convention, and the 404 below is the only path not in the table.
    routes = {
        "/api/stop": lambda _body: ({"said": runner.stop()}, 200),
        "/api/daemon": lambda one: answer_a_daemon_switch(one, chains),
        "/api/rpc": lambda one: answer_an_rpc(one, chains),
        "/api/worker": answer_a_worker_switch,
        "/api/run": lambda one: start_named_run(runner, one),
    }
    handler = routes.get(path)
    if handler is None:
        return json.dumps({"error": f"no such route: {path}"}).encode(), "application/json", 404
    answer, code = handler(body)
    return json.dumps(answer).encode(), "application/json", code


#: The only host names a browser on this machine will put in an Origin for this page. HOSTS,
#: not URL prefixes -- see host_of() for the vulnerability that distinction closes.
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")


def host_of(url_or_authority: str) -> str:
    """The bare host from an Origin or a Host header, with any scheme, port and brackets gone.

    WRITTEN BECAUSE MATCHING A PREFIX IS A VULNERABILITY, and this one was live for about four
    minutes before its own test caught it. The first version of the guard asked whether the
    Origin STARTED WITH "http://127.0.0.1" -- and

        http://127.0.0.1.attacker.com

    starts with exactly that. An attacker who controls any domain can register that subdomain,
    serve a page from it, and pass an Origin check that looks obviously correct. The same shape
    defeats endswith against a suffix (`evil-localhost`), which is why this parses instead of
    pattern-matching at either end.

    IPv6 brackets are stripped because a Host header writes the loopback address as `[::1]:8765`
    while an Origin writes `http://[::1]`, and a comparison that handled one and not the other
    would refuse the operator's own browser on a v6-preferring machine.
    """
    authority = url_or_authority.split("://", 1)[-1]
    authority = authority.split("/", 1)[0]
    if authority.startswith("["):
        return authority[1:].split("]", 1)[0]
    return authority.rsplit(":", 1)[0] if ":" in authority else authority


def refuse_a_cross_origin_post(headers, port: int) -> str:
    """"" if this POST may proceed, else why not. THE defense against a web page driving this.

    THE HOLE THIS CLOSES, and it is a real one rather than a formality. This panel binds
    loopback so nothing on the network can reach it -- but the operator's own BROWSER can, and
    any page they visit can issue requests to 127.0.0.1. Nothing here authenticates a caller, so
    without this, a page on any site could POST /api/run and start a harness that SPENDS COIN,
    and the operator would see only a run they did not start.

    TWO CHECKS, AND THEY FAIL DIFFERENTLY ON PURPOSE:

      Origin      a browser sends it on every cross-origin POST. Anything that is not this
                  machine is refused. A MISSING Origin is allowed, because curl and the tests
                  send none and a page-driven request always carries one -- the check is aimed
                  at browsers, which is where the threat is.
      Host        must be loopback. This is the DNS-rebinding defense: an attacker's domain can
                  be made to resolve to 127.0.0.1, which makes their page SAME-ORIGIN with this
                  one and silences the Origin check entirely. The Host header still carries
                  their domain, and that is what gives them away.

    THE CONTENT TYPE IS THE THIRD LOCK and it lives at the call site rather than here: requiring
    `application/json` means a browser cannot reach these routes with a simple form POST at all,
    because that content type forces a CORS preflight this server never answers.

    A FUNCTION, CALLED WITH A HEADERS MAPPING, so the tests hand it hostile values without a
    socket (rule 10). It is the single decision that says whether a stranger's web page can
    spend this operator's coin.
    """
    origin = (headers.get("Origin") or "").strip()
    if origin and host_of(origin) not in LOOPBACK_HOSTS:
        return (f"refused: this request came from {origin}, which is not this machine. The "
                f"panel has buttons that spend coin and nothing authenticates a caller, so a "
                f"page you merely VISITED must not be able to drive it.")
    host = (headers.get("Host") or "").strip()
    if host and host_of(host) not in LOOPBACK_HOSTS:
        return (f"refused: this request names host {host!r}. The panel serves 127.0.0.1 only, "
                f"and a name that resolves here is how a DNS-rebinding attack makes its own "
                f"page same-origin with this one.")
    return ""


def answer_a_daemon_switch(body: object, chains: dict | None) -> tuple[dict, int]:
    """Start or stop one chain's daemon, or say why this panel will not.

    IT REUSES daemons.start_daemon AND daemons.stop_daemon rather than spawning anything of its
    own, and that is the whole reason this is safe to offer at all. Those two are a spawn and
    its named reaper living in one file so neither can be edited without the other in view
    (rule 13), and stop_daemon PROVES the process is gone rather than trusting an exit code --
    it reads the pid BEFORE asking, because the daemon deletes its pid file on the way out.
    A second implementation here would be a spawn whose reaper is somewhere else.

    `we_started_it=True` IS PASSED ON PURPOSE AND IS NOT A LIE ABOUT HISTORY. That flag exists
    so a harness leaves an ADOPTED daemon alone on its way out; here the operator has pressed a
    button that says stop, which is the case the flag is meant to except, not enforce. The
    refusal above is what decides whether they may, and it has already run by this point.
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "the request body was not an object"}, 400
    asset, action = body.get("asset"), body.get("action")
    tab = next((c for c in decisions.CHAINS if c.asset == asset), None)
    if tab is None:
        return {"ok": False, "refused": True, "error": f"{asset!r} is not a chain here"}, 403
    refusal = decisions.refuse_daemon_control(tab, action)
    if refusal:
        return {"ok": False, "refused": True, "error": refusal}, 403

    run = (chains or {}).get(asset)
    if run is None:
        return {"ok": False, "refused": True,
                "error": f"{asset} has no connection parameters in this panel"}, 403
    try:
        if action == "start":
            # THE SAME PREPARATION THE HARNESS DOES, and this call is the fix for a defect this
            # button CAUSED on 2026-09-28. Pressing it on the LTC tab ran
            #     litecoind -datadir=... -regtest -daemon
            # with no -vbparams, because the MWEB override was a step of the nine-step harness
            # and this panel runs no steps. ltc_htlc_verify then adopted that daemon -- rightly,
            # it was answering -- and died at height 288 on bad-txns-vin-empty, the exact
            # failure the flag exists to prevent. Two ways to start one daemon and only one of
            # them knew the rule (rule 8).
            daemons.apply_mweb_override(run.console, run.config)
            spawned = daemons.start_daemon(run.console, run.config)
            said = (f"{asset}: started" if spawned
                    else f"{asset}: already answering -- nothing was started")
        else:
            daemons.stop_daemon(run.console, run.config, we_started_it=True)
            said = f"{asset}: stop requested, and daemons.stop_daemon PROVED the process is gone"
    except Exception as error:  # noqa: BLE001 -- checked: the failure IS the return value; a panel that dies on a daemon that will not start is a panel that cannot report it
        return {"ok": False, "refused": False,
                "error": f"{type(error).__name__}: {error}"}, 200
    return {"ok": True, "said": said}, 200


def swapper_payload(run_dir=None) -> dict:
    """The swap terminal's own state: swaps, payouts, workers, inventory, pricing.

    ONE OPERATOR SURFACE, 2026-09-30, at the operator's instruction. Until today this panel and
    the Flask app's /admin were deliberately separate, and four docstrings in this repo argued
    for it at length -- the argument being that buttons which spend money do not belong on an
    unauthenticated GET-only page. That argument still holds and is not what changed. What
    changed is the direction: the read-only PICTURE moves onto the surface that already has the
    stricter guards, rather than the buttons moving onto the looser one.

    NOT A SECOND IMPLEMENTATION OF IT. Every row here is services/admin_view.overview(), the
    same pure function routes/admin.py renders and /api/admin/overview serializes. It takes its
    database, config and adapters as arguments precisely so a caller outside Flask can supply
    them, and workers/common.py already owns both of those constructions -- so this function
    builds nothing of its own and holds no query, no threshold and no freshness rule. If it
    ever grows one, that is the bug (rule 8).

    NEVER RAISES. This panel is opened when something is already wrong, and a swap-state region
    that takes the whole page down with it is worse than one that says it could not read. The
    failure is IN the return value -- `ok` is False with the reason -- which is the shape rule 12
    requires of a broad catch: the caller can tell it from an answer.

    IT CONTACTS NO CHAIN AND NO PRICE FEED. overview() reads the database, the configuration and
    supervisor's pid files; its pricing panel reads the price CACHE and does not fetch. That is
    why this is a GET the page may poll.
    """
    try:
        from db import (  # noqa: PLC0415 -- checked: deferred so this stdlib-only server still imports on a host where the Flask app's dependencies are absent, which is the same reason the regtest modules defer theirs.
            db_session,
        )
        from services.admin_view import overview  # noqa: PLC0415 -- checked: same.
        from workers.common import (  # noqa: PLC0415 -- checked: same, and these two are the CLI's own constructions rather than new ones.
            build_adapters_from_config,
            get_config_dict,
        )

        config = get_config_dict()
        with db_session(config["DB_PATH"]) as db:
            return {"ok": True, "overview": overview(db, config, build_adapters_from_config(), run_dir=run_dir)}
    except Exception as error:  # noqa: BLE001 -- checked: a missing database file, an unreadable schema, a chain adapter that will not construct and an import failure all mean the same thing to this region -- it cannot show swap state right now -- and every one of them is reported as ok=False with the reason, never as an empty table. A panel that dies here is a panel that cannot tell the operator why.
        return {"ok": False, "error": f"{type(error).__name__}: {error}"}


def answer_a_worker_switch(body: object, run_dir=None) -> tuple[dict, int]:
    """Start or stop one of the swapper's workers, or say why not.

    IT REUSES supervisor.start_worker AND supervisor.stop_worker and adds no lifecycle of its
    own -- the same reason answer_a_daemon_switch() calls daemons.start_daemon: a spawn and its
    reaper belong in one file so neither can be edited without the other in view (rule 13).
    stop_worker() polls for the process's ABSENCE after SIGTERM and again after SIGKILL and
    returns `failed` if it is still there, so the assertion is the absence and not the exit code
    of the kill.

    THE OUTCOME IS PASSED THROUGH VERBATIM, and there are five of them:
    started, already-running, stopped, not-running, stale-pidfile, failed. "already-running"
    and "not-running" are NOT quiet successes -- rule 13 calls "skipped" printed beside "ok" a
    defect in the output -- so the page renders the word supervisor returned rather than a
    thumbs-up. `failed` is the one that means the operator asked for something and did not get
    it, and it must not read like `stopped`.
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "the request body was not an object"}, 400
    name, action = body.get("worker"), body.get("action")
    refusal = decisions.refuse_worker_control(name, action)
    if refusal:
        return {"ok": False, "refused": True, "error": refusal}, 403

    from supervisor import (  # noqa: PLC0415 -- checked: deferred for the same reason as swapper_payload()'s imports, and worker_commands() is read here rather than cached so the argv used is the table's own.
        DEFAULT_RUN_DIR,
        start_worker,
        stop_worker,
        worker_commands,
    )

    directory = DEFAULT_RUN_DIR if run_dir is None else run_dir
    try:
        if action == "start":
            result = start_worker(name, worker_commands()[name], directory)
        else:
            result = stop_worker(name, directory)
    except Exception as error:  # noqa: BLE001 -- checked: a worker that will not start and a signal that cannot be sent are reported rather than raised, because a panel that dies on a failed start is a panel that cannot say which worker failed. The reason is in the return value and `ok` is False.
        return {"ok": False, "refused": False, "error": f"{type(error).__name__}: {error}"}, 200
    # `failed` means the thing the operator asked for did not happen. It is the one outcome that
    # must not be reported as ok=True, because a stop that could not prove the process is gone
    # is not a stop (rule 13).
    return {"ok": result.get("outcome") != "failed", "result": result}, 200


def answer_an_rpc(body: object, chains: dict | None) -> tuple[dict, int]:
    """One read-only RPC against one chain's daemon, or a named refusal.

    TWO GATES, AND BOTH ARE HERE RATHER THAN IN THE HANDLER. The asset must be one this panel
    already serves a tab for, and the method must be on the read-only allowlist. Neither is
    checked in the browser: the dropdown the page renders is a convenience, and a request
    naming anything else is refused whatever the page sends (rule 10 -- the decision is a
    function, called with hostile input in the tests).

    403 FOR A REFUSAL AND 400 FOR A MALFORMED REQUEST, because they mean different things to
    whoever is reading: one is a boundary this panel holds on purpose and the other is a
    mistake in the request. A single status for both would make the deliberate one look like a
    bug worth working around.
    """
    if not isinstance(body, dict):
        return {"ok": False, "error": "the request body was not an object"}, 400
    asset = body.get("asset")
    tab = next((c for c in decisions.CHAINS if c.asset == asset), None)
    # THE XRP TAB SPEAKS A DIFFERENT PROTOCOL AND TAKES A DIFFERENT ROUTE, decided by the tab
    # rather than by whether a bitcoin-style connection happens to exist. `chains` holds
    # funding_steps.Run objects for the bitcoind-family tabs only, so before the map existed an
    # XRP request fell into the run-is-None branch below and was refused -- correctly then, and
    # wrongly now that chains/xrp_rpc_map.py can translate the question.
    if tab is not None and decisions.console_protocol(tab) == "xrpl":
        answer = answer_an_xrp_rpc(body, tab)
        return answer, 200 if answer["ok"] else (403 if answer.get("refused") else 200)
    run = (chains or {}).get(asset) if isinstance(asset, str) else None
    if run is None:
        # WHY THERE IS NO CONSOLE, not merely that there is none. "this chain has no
        # reachable daemon in this panel" was what this said on 2026-09-28 and it is not
        # true: the operator's daemon for that chain may be answering perfectly well. What
        # is true is that this console speaks one protocol and the chain does not, and only
        # the tab knows which of those two it is.
        reason = decisions.refuse_an_rpc_console(tab) if tab is not None else ""
        return {"ok": False, "refused": True,
                "error": reason or f"{asset!r} has no reachable daemon in this panel"}, 403
    args = body.get("args")
    if not isinstance(args, list):
        args = []
    answer = decisions.call_read_only(run, body.get("method"), args)
    return answer, 200 if answer["ok"] else (403 if answer.get("refused") else 200)


def answer_an_xrp_rpc(body: dict, tab) -> dict:
    """One translated XRP Ledger read, or the reason there is none. Never raises.

    THE ADAPTER IS BUILT FROM CONFIGURATION, not held by this panel, and that is the honest
    shape: the XRP tab has no funding_steps.Run because this panel does not and cannot drive a
    rippled daemon's lifecycle -- see refuse_daemon_control(). What it CAN do is read, through
    the same chains/xrp.XRPAdapter every worker uses, which already owns rippled's params shape
    and its 200-with-error behavior.

    AN UNSET XRP_RPC_URL IS A REFUSAL AND NAMES THE VARIABLE. It is the ordinary reading on a
    host that has not exported it, it is not a fault, and it must not read like one -- the tab
    above already says the same thing about the same variable, in the same words, from the same
    place (regtest/operator_panel.FOREIGN_ENV).

    THE ACCOUNT COMES FROM THE REQUEST OR FROM XRP_DEPOSIT_ACCOUNT, in that order, and NEITHER
    is invented. An account_info with a silently defaulted account answers confidently about
    the wrong account, which is the shape rule 17 forbids; `account_used` rides on the answer so
    the operator can see which one it was.
    """
    from workers.common import (  # noqa: PLC0415 -- checked: deferred like swapper_payload()'s imports, so this stdlib-only server imports on a host without the chains package's dependencies.
        build_adapters_from_config,
        get_config_dict,
    )

    try:
        config = get_config_dict()
        adapter = build_adapters_from_config().get(tab.asset)
    except Exception as error:  # noqa: BLE001 -- checked: a configuration that will not construct is reported rather than raised; the console is an exploration tool and a panel that dies building an adapter cannot say which setting broke it.
        return {"ok": False, "refused": True,
                "error": f"the {tab.asset} adapter could not be built: {type(error).__name__}: {error}"}
    if adapter is None:
        return {"ok": False, "refused": True,
                "error": decisions.why_foreign_is_unconfigured(tab) or
                         f"no {tab.asset} adapter was built, and every endpoint variable it needs IS set -- "
                         f"which is a fault rather than a configuration gap"}

    account = body.get("account")
    if not isinstance(account, str) or not account.strip():
        account = str(config.get("XRP_DEPOSIT_ACCOUNT") or "")
    # ONE NAMED ARGUMENT, NOT THE WHOLE LIST. The console's box takes a JSON array because
    # bitcoind's params are positional; rippled's are named, and every entry in the map that
    # needs an argument needs exactly one. Passing the list through would have asked `ledger`
    # for ledger_index [90000000] -- a list where an integer goes -- which rippled reports as
    # an invalid params error that says nothing about the real mistake.
    args = body.get("args")
    argument = args[0] if isinstance(args, list) and args else (None if isinstance(args, list) else args)
    answer = decisions.call_xrp_read_only(adapter, body.get("method"), argument, account)
    if isinstance(args, list) and len(args) > 1:
        # SAID RATHER THAN IGNORED (rule 14). An operator who typed three arguments and got an
        # answer computed from one must be told which one was used.
        answer["extra_args_ignored"] = args[1:]
    if account:
        answer["account_used"] = account
    return answer


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


def build_handler(run: funding_steps.Run, runner: HarnessRunner, page: str,
                  memory: dict | None = None, chains: dict | None = None):
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
            # NOTHING HERE MAY BE CACHED OR REFERRED ONWARDS. The bodies carry addresses, txids
            # and balances; a cached copy outlives the run and a Referer leaks the panel's
            # existence and port to anything a link ever reaches.
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, code: int = 200) -> None:
            self._send(code, json.dumps(payload).encode(), "application/json")

        def do_GET(self) -> None:
            body, content_type, code = guarded(answer_a_get, self.path, run, runner, page, memory)
            self._send(code, body, content_type)

        def do_POST(self) -> None:
            refusal = refuse_a_cross_origin_post(self.headers, self.server.server_address[1])
            if not refusal and "json" not in (self.headers.get("Content-Type") or ""):
                # THE THIRD LOCK. `application/json` cannot be sent by a browser's simple form
                # POST -- it forces a CORS preflight, which this server never answers -- so a
                # page cannot reach a state-changing route even without script access.
                refusal = ("refused: POSTs here must be application/json, which a browser form "
                           "cannot send without a preflight this server does not answer.")
            if refusal:
                self._json({"ok": False, "refused": True, "error": refusal}, 403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) or b"{}"
            body, content_type, code = guarded(answer_a_post, self.path, raw, runner, chains)
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
        config = funding_steps.resolve_config("GRC")
        run = funding_steps.Run(console=console, config=config, wallet="")
        console.say(f"GRC: endpoint {config.base_url}  datadir {config.datadir}")
        funding_steps.step_1_reachable(run)
        funding_steps.assert_test_network(run)
    except RegtestSetupError as exc:
        console.banner("REFUSED BEFORE THE PORT WAS BOUND")
        console.say(str(exc))
        return 1

    # FROM HERE ON THE PANEL IS SERVING PAGES, and a page draw re-reads the funding. The
    # startup gate above ran against the real console -- every OK line of it is printed in full
    # -- and this wraps the console the request handlers use so the same five payment lines
    # stop being restated on every draw. See decisions.SaysEachLineOnce for the measurement.
    run.console = decisions.SaysEachLineOnce(console)
    runner = HarnessRunner()
    # ONE CACHE FOR THE PROCESS, held here rather than at module level so a test builds its own
    # and two panels in one process could not share one. It only ever holds "this outpoint was
    # spent by that transaction", which is true forever once true.
    # ONE Run PER CHAIN, BUILT ONCE. The RPC console needs a connection per asset, and building
    # one per request would open a new session on every keystroke-driven call. GRC reuses the
    # run whose network was already gated three ways before this port was bound; the others are
    # built lazily and may be unreachable, which their tab already says.
    chains = {"GRC": run}
    for tab in decisions.CHAINS:
        if tab.asset == "GRC":
            continue
        # ASK BEFORE TRYING. `kind == "none"` stood here and matched nothing (no tab has ever
        # carried that kind), so every foreign chain fell through to resolve_config() and
        # printed `no RPC console (KeyError: ...)` at startup on 2026-09-28 -- a Python
        # exception class in an operator's terminal, for a design decision that is knowable
        # without asking anything. The refusal now says which, in words.
        refusal = decisions.refuse_an_rpc_console(tab)
        if refusal:
            console.say(f"{tab.asset}: no RPC console -- {refusal}")
            continue
        try:
            chains[tab.asset] = funding_steps.Run(
                console=console, config=funding_steps.resolve_config(tab.asset), wallet="")
        except Exception as exc:  # noqa: BLE001 -- checked: a chain with no connection parameters simply gets no console, which its tab already reports; the panel must still serve the others
            console.say(f"{tab.asset}: no RPC console -- no connection parameters "
                        f"({type(exc).__name__}: {exc})")
    server = ThreadingHTTPServer((HOST, args.port),
                                 build_handler(run, runner, PAGE, funding_memory(), chains))
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
