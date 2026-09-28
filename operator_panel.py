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

from regtest import adaptor_steps, daemons
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
    <button id="callrpc">Call</button>
  </div>
  <p class="what">Every method offered here READS. Nothing on this page can create a
  transaction, sign one, touch a wallet lock, or ask for a passphrase &mdash; the allowlist is
  enforced on the server, not in this dropdown.</p>
  <pre id="rpcout">(nothing called yet)</pre>
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
  XMR: '<svg viewBox="0 0 64 64"><circle cx="32" cy="32" r="31" fill="#f26822"/><path fill="#fff" d="M14 42V22l18 17 18-17v20h-9V33l-9 9-9-9v9z"/></svg>',
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
      h += '<p class="bad">NOT CONFIGURED &mdash; ' + esc((d.missing_env || []).join(", ")) +
           " is unset in the environment this panel was started with. Nothing reads a .env " +
           "here, so it has to be exported in the shell that starts the panel; a value set " +
           "only in a file, or only in another shell, does not reach this process.</p>";
    }
    h += '<p class="sub">This panel speaks a Bitcoin-style JSON-RPC and this chain does not, ' +
         'so it does not probe the endpoint itself &mdash; that chain\u2019s own read-only ' +
         'check does, and it is the button below.</p>';
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
  // XMR tab on 2026-09-28 and got "REFUSED BY THIS PANEL" -- a dropdown of twenty-five methods
  // was offered for a chain that has none of them. Offering a control that cannot work, and
  // explaining afterwards, is the shape rule 14 calls a defect in the output.
  $("rpcwhynot").textContent = d.console || "";
  $("rpcwhynot").style.display = d.console ? "" : "none";
  $("rpcrow").style.display = d.console ? "none" : "";
  $("rpcout").textContent = d.console ? "(no console on this tab)" : "(nothing called yet)";
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
      body: JSON.stringify({asset: current, method: $("method").value, args})})).json();
  } catch (e) { $("rpcout").textContent = "could not reach the panel: " + e; return; }
  // A REFUSAL AND A FAILURE READ DIFFERENTLY. One is a boundary this panel holds on purpose;
  // the other is the chain answering. Rendering both as "error" would make the deliberate one
  // look like a bug worth working around.
  if (d.ok) { $("rpcout").textContent = JSON.stringify(d.result, null, 2); }
  else if (d.refused) { $("rpcout").textContent = "REFUSED BY THIS PANEL\n\n" + d.error; }
  else { $("rpcout").textContent = "the daemon answered:\n\n" + d.error; }
};

$("checkall").onclick = checkEveryChain;
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
    if path == "/api/stop":
        return json.dumps({"said": runner.stop()}).encode(), "application/json", 200
    if path == "/api/daemon":
        answer, code = answer_a_daemon_switch(body, chains)
        return json.dumps(answer).encode(), "application/json", code
    if path == "/api/rpc":
        answer, code = answer_an_rpc(body, chains)
        return json.dumps(answer).encode(), "application/json", code
    if path != "/api/run":
        return json.dumps({"error": f"no such route: {path}"}).encode(), "application/json", 404
    answer, code = start_named_run(runner, body)
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
    run = (chains or {}).get(asset) if isinstance(asset, str) else None
    if run is None:
        # WHY THERE IS NO CONSOLE, not merely that there is none. "'XMR' has no reachable
        # daemon in this panel" was what this said on 2026-09-28 and it is not true: the
        # operator's monero-wallet-rpc may be answering perfectly well. What is true is that
        # this console speaks one protocol and XMR is not it, and only the tab knows which of
        # those two it is.
        tab = next((c for c in decisions.CHAINS if c.asset == asset), None)
        reason = decisions.refuse_an_rpc_console(tab) if tab is not None else ""
        return {"ok": False, "refused": True,
                "error": reason or f"{asset!r} has no reachable daemon in this panel"}, 403
    args = body.get("args")
    if not isinstance(args, list):
        args = []
    answer = decisions.call_read_only(run, body.get("method"), args)
    return answer, 200 if answer["ok"] else (403 if answer.get("refused") else 200)


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
                  known_spent: dict | None = None, chains: dict | None = None):
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
            body, content_type, code = guarded(answer_a_get, self.path, run, runner, page, known_spent)
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
    # ONE Run PER CHAIN, BUILT ONCE. The RPC console needs a connection per asset, and building
    # one per request would open a new session on every keystroke-driven call. GRC reuses the
    # run whose network was already gated three ways before this port was bound; the others are
    # built lazily and may be unreachable, which their tab already says.
    chains = {"GRC": run}
    for tab in decisions.CHAINS:
        if tab.asset == "GRC":
            continue
        # ASK BEFORE TRYING. `kind == "none"` stood here and matched nothing (no tab has ever
        # carried that kind), so the three foreign chains fell through to resolve_config() and
        # printed `XMR: no RPC console (KeyError: 'XMR')` at startup on 2026-09-28 -- a Python
        # exception class in an operator's terminal, for a design decision that is knowable
        # without asking anything. The refusal now says which, in words.
        refusal = decisions.refuse_an_rpc_console(tab)
        if refusal:
            console.say(f"{tab.asset}: no RPC console -- {refusal}")
            continue
        try:
            chains[tab.asset] = adaptor_steps.Run(
                console=console, config=adaptor_steps.resolve_config(tab.asset), wallet="")
        except Exception as exc:  # noqa: BLE001 -- checked: a chain with no connection parameters simply gets no console, which its tab already reports; the panel must still serve the others
            console.say(f"{tab.asset}: no RPC console -- no connection parameters "
                        f"({type(exc).__name__}: {exc})")
    server = ThreadingHTTPServer((HOST, args.port),
                                 build_handler(run, runner, PAGE, {}, chains))
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
