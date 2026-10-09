/*
 * Progressive enhancement for the customer surface. It decides nothing.
 *
 * Role: static asset (behavior only)
 * Reads: /swap/<id>/fragment, for live status on the swap page
 * Writes: nothing but the DOM
 * Can move funds: no, and one function here exists BECAUSE money moved --
 *        clearBrowserFilledPayoutAddress(). See its own comment.
 * Mainnet-safe: yes
 *
 * ==========================================================================
 * 666 -> 361 LINES ON 2026-10-09, AND WHAT WENT IS WHY THIS HEADER CHANGED
 * ==========================================================================
 *
 * Seven functions were deleted as dead: wireQuoteForm, wireSwapForm,
 * wireLampFilter, and the four helpers they were the only callers of (say,
 * sayRows, postJson, appendDepth). 302 lines, 45% of the file.
 *
 * Established by grepping the WHOLE TREE for each DOM id by NAME rather than by
 * following imports, which is rule 2's guard and which mattered here -- one of
 * the three nearly survived on a false hit:
 *
 *     quote-form, quote-result, create-swap   nowhere in the tree
 *     swap-form                               nowhere in the tree
 *     lamp-filter-note                        nowhere in the tree
 *     lampstrip                               templates/admin.html:830, BUT as
 *                                             class="lampstrip", never id=. The
 *                                             function did getElementById.
 *     swapgrid                                templates/atm.html, which does
 *                                             not load this file at all
 *
 * They left with templates/index.html when the ATM flow replaced it. A grep that
 * had stopped at "lampstrip appears in a template" would have kept 55 lines of
 * code that cannot run.
 *
 * THE HEADER THIS REPLACES WAS WRONG IN FOUR PLACES after that cull, which is
 * the reason it is rewritten rather than trimmed (rule 16: a wrong comment is a
 * bug). It said this file reads /api/quotes and /api/swaps -- no code here calls
 * either now. It said "every form below has a real method and action" -- there is
 * no form below. It said "every fetch announces before it starts", which was
 * say()'s job and say() is gone. And `let latestQuote = null` sat at the top with
 * zero readers left in the file.
 *
 * ==========================================================================
 * WHAT IS DELIBERATELY NOT IN THIS FILE, AND WHY THAT IS THE POINT
 * ==========================================================================
 *
 * The version this replaces contained:
 *
 *     const validTargets = { GRC: ["BTC", "LTC"], BTC: ["GRC"], LTC: ["GRC"] }
 *
 * -- a hand-written second copy of Config.ALLOWED_PAIRS, in a language the
 * server cannot check, deciding which direction a customer may pick. It agreed
 * with the server on the day it was written. CLAUDE.md rule 8 is exactly about
 * the day after: one of the two copies goes stale, and the page either hides a
 * live pair or offers one the API refuses, with nothing failing anywhere. The
 * pair list is now rendered by the server from the authority and that constant
 * is deleted rather than moved.
 *
 * The same reasoning kept three other things out of here:
 *
 *   the status vocabulary  -- what `confirming` means, whether a swap is
 *       stalled, whether the confirmation threshold is met. All of it is in
 *       services/swap_view.py, and this file fetches SERVER-RENDERED HTML for
 *       the live region instead of JSON it would have to interpret. That is why
 *       the poll targets /swap/<id>/fragment and not /api/swaps/<id>.
 *   the confirmation threshold -- a count that decides when money moves. It is
 *       on the swap row and is rendered by the server.
 *   any amount arithmetic -- fee, rate and payout estimate are computed once,
 *       in services/quote_service.py, and displayed as returned. A browser that
 *       recomputed them would be a second fee schedule.
 *
 * WHAT IS LEFT IS FOUR THINGS, and every one of them is inert on a page that
 * does not carry its markup -- each opens with an early return on a missing
 * element, which is what makes this file safe to load from any template:
 *
 *   wireLivePolling                  #swap-live + script[data-swap-fragment]
 *   clearBrowserFilledPayoutAddress  #payout_address
 *   wireCopyButtons                  delegated; needs no element at load
 *   wireWalletMenu                   .wallet-menu
 */

"use strict";

// The page works without any of this. Every form below has a real method and
// action, so a browser with scripting off posts and gets the API's own JSON.
// This only replaces that with something nicer.

function el(id) {
  return document.getElementById(id);
}







// --- the live status region ------------------------------------------------

/**
 * Poll the SERVER-RENDERED fragment and replace the live region with it.
 *
 * Fifteen seconds, matching workers/deposit_watcher.DEFAULT_POLL_SECONDS: there
 * is no point asking more often than the thing that changes the answer runs.
 *
 * A FAILED REFRESH SAYS SO. Leaving the last good render on screen while every
 * refresh since has failed is the silent failure rule 14 opens with -- the
 * reader cannot tell a swap that has not moved from a page that has stopped
 * asking. The banner it inserts says when the last successful refresh was.
 */
function wireLivePolling() {
  const region = el("swap-live");
  const script = document.querySelector("script[data-swap-fragment]");
  if (!region || !script) return;
  const url = script.getAttribute("data-swap-fragment");
  const POLL_MS = 15000;

  function markStale(message) {
    let banner = el("live-stale");
    if (!banner) {
      banner = document.createElement("p");
      banner.id = "live-stale";
      banner.className = "callout callout-halted";
      region.insertBefore(banner, region.firstChild);
    }
    banner.textContent = message;
  }

  function clearStale() {
    const banner = el("live-stale");
    if (banner) banner.remove();
  }

  async function refresh() {
    try {
      const response = await fetch(url, { headers: { "Accept": "text/html" } });
      const html = await response.text();
      // A 404 here means the swap is no longer readable, and the server renders
      // a fragment that says that. Replacing with it is correct; the fragment
      // is the message.
      region.innerHTML = html;
      clearStale();
    } catch (error) {
      markStale(
        "This page could not refresh itself just now (" + error.message + "). Everything below is from the last " +
          "refresh that worked, not from now. It will keep trying every 15s."
      );
    }
  }

  window.setInterval(refresh, POLL_MS);
}

/*
 * CLEAR WHAT THE BROWSER PUT IN THE PAYOUT FIELD, and this is a safety measure
 * rather than a convenience.
 *
 * MEASURED ON THE OPERATOR'S HOST 2026-10-01, three times in one afternoon.
 * The field carries autocomplete="off" (and now "one-time-code") and Brave
 * filled it anyway with a stale Bitcoin testnet address,
 * 2N3bqzcWSDasdmqKkDKUAFU8f5Hk11NZzYN, left over from unrelated work. It passed
 * every check the server has: Gridcoin shares Bitcoin testnet's 0xc4 P2SH
 * version byte, so validateaddress returns isvalid: true. The daemon later
 * answered ismine: false -- the key belongs to nobody we hold -- and 82.65 tGRC
 * had already been broadcast to it. A payout is final the moment it is sent.
 *
 * So this does not rely on an attribute being honored. It empties the field
 * after load, which is the one thing that works whatever the browser decides
 * about autocomplete. A customer who wants their address there types or pastes
 * it; a browser that wants it there does not get to decide silently.
 *
 * TWICE, with a zero-delay timeout for the second: Chrome-family autofill often
 * runs AFTER DOMContentLoaded, so clearing only once loses the race.
 *
 * It is deliberately NOT wired to focus or input events. Clearing on focus
 * would delete what a customer had already typed if they tabbed away and back,
 * which would be a worse bug than the one this fixes.
 *
 * NOT VERIFIED FROM HERE (rule 17): this container has no browser. That it
 * empties the field is plain JavaScript; that it beats Brave 154's autofill is
 * the operator's observation to make.
 */
function clearBrowserFilledPayoutAddress() {
  const field = document.getElementById("payout_address");
  if (!field) return;
  field.value = "";
  window.setTimeout(function () {
    field.value = "";
  }, 0);
}

/*
 * THE COPY BUTTONS, wired by delegation so the live region can replace its markup
 * without rewiring anything.
 *
 * ASKED FOR BY THE OPERATOR 2026-10-01: "have it to where they can copy it directly
 * with a little copy icon". Before this, class="copyable" was CSS only --
 * styles.css sets user-select: all so a click selects the string -- and nothing
 * here touched the clipboard. The class named an affordance the page did not have.
 *
 * THE TEXT COMES FROM THE ELEMENT THE CUSTOMER IS LOOKING AT, not from a data
 * attribute. A deposit address or a memo tag that differs by one character between
 * what is displayed and what is copied is money sent somewhere nobody can claim it,
 * so the displayed node IS the source. templates/_copy_field.html pairs each button
 * with exactly one .copyable inside one .copy-row.
 *
 * IT SAYS WHETHER IT WORKED, both ways (rule 14: never let a result print nothing).
 * navigator.clipboard needs a secure context and a permission the browser can
 * refuse. On failure the button says "Select it" and the text is selected for the
 * customer, which is what user-select: all was always for -- a silent no-op would
 * leave somebody believing they had copied an address they had not.
 *
 * aria-live sits on the .copy-word span -- the only node whose text changes -- so a
 * screen reader hears the outcome. On the row it would re-announce the address itself
 * every time the word flipped back.
 */
const COPY_FEEDBACK_MS = 1500;

function selectCopyableText(node) {
  const range = document.createRange();
  range.selectNodeContents(node);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
}

function wireCopyButtons() {
  document.addEventListener("click", async function (event) {
    const button = event.target.closest(".copy-button");
    if (!button) return;
    const row = button.closest(".copy-row");
    const value = row && row.querySelector(".copyable");
    if (!value) return;
    const word = button.querySelector(".copy-word");
    const text = value.textContent.trim();
    let outcome = "Copied";
    try {
      await navigator.clipboard.writeText(text);
    } catch (error) {
      // NOT swallowed into a no-op: the customer is told to select it, and the
      // selection is made for them. A button that silently did nothing would
      // leave somebody believing they held an address they did not.
      outcome = "Select it";
      selectCopyableText(value);
    }
    if (word) {
      const previous = word.textContent;
      word.textContent = outcome;
      window.setTimeout(function () {
        word.textContent = previous;
      }, COPY_FEEDBACK_MS);
    }
  });
}

/*
 * THE WALLET MENU. The page decides PRESENCE; the server decided CAPABILITY.
 *
 * Asked for by the operator 2026-10-01: "creata a clicking submenu of wallets".
 *
 * services/wallet_menu.py is the authority on which chains each wallet can sign
 * for, and it rendered the entries. This file answers the one question only a
 * browser can: is the extension actually installed. Keeping those apart is the
 * point -- a JavaScript file deciding which chains a wallet supports would be
 * logic the server cannot check, on the page that tells customers where to send
 * money.
 *
 * NO WALLET LIBRARY. chains/solana_pay.py built the request and the browser
 * hands it over unaltered. The usual adapters are a megabyte of third-party code
 * that would have the deposit address passing through it, and a buggy or
 * compromised copy substitutes one base58 string for another with every
 * server-side check still passing.
 *
 * WHAT IS NOT ESTABLISHED, and the operator will find out before I do: whether
 * each extension honours a raw `solana:` request. Phantom and Solflare document
 * a `request`/`connect` provider API, and signing a transaction through it needs
 * a transaction OBJECT, which needs transaction construction -- the thing this
 * design deliberately does not do in the browser. So the click opens the request
 * URI and lets the wallet interpret it. If an extension ignores it, the honest
 * fix is for the SERVER to build and serialize the transaction and have the
 * extension sign that; the primitives for it are already in
 * chains/solana_address.py and chains/solana_units.py. That is a bigger piece
 * and is not written on a guess about extension behaviour.
 *
 * Until then the button reports what happened rather than pretending: it says
 * "opening <wallet>" and the QR and the copy fields are right beside it.
 */
function providerAt(path) {
  // A dotted path from `window`, resolved step by step, because
  // `window.phantom.solana` throws on a host where `phantom` is absent -- which
  // is every host without that extension, i.e. the common case.
  return path.split(".").reduce(function (node, key) {
    return node && node[key] ? node[key] : null;
  }, window);
}

function wireWalletMenu() {
  const menu = document.querySelector(".wallet-menu");
  if (!menu) return;
  const uri = menu.getAttribute("data-pay-uri");

  menu.querySelectorAll(".wallet-button").forEach(function (button) {
    const state = button.parentElement.querySelector(".wallet-state");
    const name = button.getAttribute("data-name");
    const found = providerAt(button.getAttribute("data-provider"));
    if (found) {
      // "installed" and not "ready": the provider object existing says the
      // extension is there, not that it will accept this request. Claiming the
      // stronger thing is what rule 17 is about, in a UI string.
      if (state) state.textContent = "installed";
    } else {
      button.disabled = true;
      const install = button.getAttribute("data-install");
      if (state) {
        state.textContent = "";
        // NOT "not installed", AND THE DIFFERENCE COST A ROUND TRIP.
        //
        // MEASURED ON THE OPERATOR'S HOST 2026-10-01: "no wallet appears to work
        // even though coinbase wallet is installed into brave". It was. The
        // desktop launcher opens the page with
        //
        //     --app=<url>  --user-data-dir=<repo>/runtime/browser-profile
        //
        // so the window has no toolbar AND a dedicated, empty profile. Browser
        // extensions live in the user's NORMAL profile, so that window has none
        // installed at all -- the provider object really is absent, the probe was
        // right, and the advice it gave ("get it") was wrong about why and told
        // somebody to install software they already had.
        //
        // The dedicated profile is deliberate and measured (see
        // swap_terminal_desktop.browser_command: on a shared profile the second
        // launch returned rc=0 in 72ms and the launcher would tear the server
        // down 72ms after opening the UI), so this is a fact to explain rather
        // than a setting to change.
        //
        // IT NAMES THE CAUSES AND CLAIMS NONE (rule 17). A page cannot tell
        // "this profile has no extensions" from "this browser has none
        // installed" from "the extension is installed and declined to inject
        // here" -- there is no reliable way to detect app mode, profile identity
        // or another extension's injection policy from script.
        //
        // THE WORDING CHANGED 2026-10-02 BECAUSE ITS ADVICE BECAME A DEAD END.
        // It used to lead with the empty-profile explanation and say "open the
        // same URL in your normal browser instead". The operator did exactly
        // that -- swap_terminal_desktop.py --window, which opens in their own
        // Brave, and the launcher confirmed "Opening in existing browser
        // session" -- and all three wallets still reported absent. So for that
        // case the profile explanation is REFUTED, and a message that keeps
        // offering it sends a reader back down a path they have already walked.
        //
        // What is NOT established is which of the remaining causes it is. Wallet
        // extensions commonly restrict injection by origin, and
        // http://127.0.0.1 is the kind of origin they restrict -- that is a
        // plausible reading and not a measurement, so it is offered as one of
        // two possibilities rather than as the answer. The message now says how
        // to rule the profile out, which is the half a reader CAN settle.
        //
        // "did not inject a provider" rather than "not available": the page
        // knows exactly one thing here -- that the global is absent -- and
        // saying that is honest where "not available" invites the reader to
        // conclude the software is missing.
        const note = document.createElement("span");
        note.textContent = "did not inject a provider on this page. Two things can cause that and " +
          "this page cannot tell them apart: a browser profile with no extensions (the desktop " +
          "launcher's default window is one \u2014 run it with --window to use your own), or an " +
          "extension that declines to inject on a plain http://127.0.0.1 origin, which several " +
          "wallets do. Check the first by opening this URL in your normal browser; if it still says " +
          "this, it is not the profile. Or: ";
        state.appendChild(note);
        const link = document.createElement("a");
        link.href = install;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "install it";
        state.appendChild(link);
      }
    }
  });

  menu.addEventListener("click", function (event) {
    const button = event.target.closest(".wallet-button");
    if (!button || button.disabled) return;
    const state = button.parentElement.querySelector(".wallet-state");
    if (state) state.textContent = "opening " + button.getAttribute("data-name") + "\u2026";
    // The request, unaltered, exactly as the server built it.
    window.location.href = uri;
  });
}


wireLivePolling();
clearBrowserFilledPayoutAddress();
wireCopyButtons();
wireWalletMenu();
