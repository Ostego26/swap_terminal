/*
 * Progressive enhancement for the customer surface. It decides nothing.
 *
 * Role: static asset (behavior only)
 * Reads: /api/quotes and /api/swaps (the same endpoints the forms POST to
 *        without it), and /swap/<id>/fragment for live status
 * Writes: nothing but the DOM
 * Can move funds: no
 * Mainnet-safe: yes
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
 * WHAT IT DOES DO is rule 14: say what is happening WHILE it happens. Every
 * fetch announces before it starts, names what it is contacting, and reports an
 * empty or failed result as a statement rather than by leaving the previous
 * text on screen.
 */

"use strict";

// The page works without any of this. Every form below has a real method and
// action, so a browser with scripting off posts and gets the API's own JSON.
// This only replaces that with something nicer.

/** The quote most recently returned by the server, or null. */
let latestQuote = null;

function el(id) {
  return document.getElementById(id);
}

/** Replace a result region's contents with one line of text and a class. */
function say(region, text, className) {
  if (!region) return;
  region.innerHTML = "";
  const p = document.createElement("p");
  p.className = className || "";
  p.textContent = text;
  region.appendChild(p);
}

/** Render a list of label/value rows into a result region. */
function sayRows(region, heading, rows, headingClass) {
  if (!region) return;
  region.innerHTML = "";
  const title = document.createElement("p");
  title.className = headingClass || "";
  title.textContent = heading;
  region.appendChild(title);
  const list = document.createElement("dl");
  list.className = "kv";
  rows.forEach(function (row) {
    const wrap = document.createElement("div");
    const dt = document.createElement("dt");
    dt.textContent = row[0];
    const dd = document.createElement("dd");
    dd.textContent = row[1];
    wrap.appendChild(dt);
    wrap.appendChild(dd);
    list.appendChild(wrap);
  });
  region.appendChild(list);
}

/**
 * POST JSON and return the parsed body, raising with the server's own message.
 *
 * A non-JSON response is reported AS a non-JSON response rather than as a
 * generic failure: "the server answered with something that is not JSON" and
 * "the server refused this quote" are different facts, and collapsing them is
 * how an outage gets read as a bad input.
 */
async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let body;
  try {
    body = await response.json();
  } catch (parseError) {
    throw new Error(
      "the server answered " + response.status + " with a body that is not JSON; nothing was created"
    );
  }
  if (!response.ok) {
    throw new Error(body.error || "the server refused this with status " + response.status);
  }
  return body;
}

// --- quotes ----------------------------------------------------------------

function wireQuoteForm() {
  const form = el("quote-form");
  const region = el("quote-result");
  const createButton = el("create-swap");
  if (!form) return;

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    const pair = el("pair");
    const amountField = el("input_amount");
    if (!pair || !pair.value) {
      say(region, "No direction is selected. Nothing was sent.", "result-error");
      return;
    }
    const parts = pair.value.split(":");
    const amount = Number(amountField.value);
    if (!(amount > 0)) {
      say(region, "Enter an amount greater than zero. Nothing was sent.", "result-error");
      return;
    }

    // Announce BEFORE (rule 14), naming what is being contacted and what for.
    say(
      region,
      // TWO FEEDS SINCE 2026-09-30, and this line said "One outbound call." until
      // then. CoinGecko is tried first and CoinPaprika second, because CoinGecko
      // returns 403 from some hosts at the CloudFront edge -- so the number of
      // calls is one or many, and the answer names which feed it came from.
      "Pricing " + amount + " " + parts[0] + " to " + parts[1] + ": asking the server, which fetches a live USD " +
        "price for each asset. Two feeds are tried in order and the answer says which one priced it.",
      "result-working"
    );

    try {
      latestQuote = await postJson("/api/quotes", {
        from_asset: parts[0],
        to_asset: parts[1],
        input_amount: amount,
      });
      sayRows(
        region,
        "Quote " + latestQuote.id,
        [
          ["You send", latestQuote.input_amount + " " + latestQuote.from_asset],
          ["Estimated payout", latestQuote.output_amount_estimate + " " + latestQuote.to_asset],
          ["Rate used", latestQuote.quoted_rate + " " + latestQuote.to_asset + " per " + latestQuote.from_asset],
          ["Fee", latestQuote.fee_bps + " bps"],
          ["Network fee reserved", latestQuote.network_fee_reserve + " " + latestQuote.to_asset],
          ["Valid until", latestQuote.expires_at],
          // RULE 14: echo the parameter that decides the answer. A rate with no
          // source is a number nobody can check, and this terminal now has two
          // feeds that could have produced it.
          ["Priced by", latestQuote.price_source || "(the server did not say)"],
        ]
      );
      appendDepth(region, latestQuote.confidence);
      if (createButton) createButton.disabled = false;
    } catch (error) {
      latestQuote = null;
      if (createButton) createButton.disabled = true;
      say(region, "No quote: " + error.message, "result-error");
    }
  });
}

/**
 * The market-depth reading for both legs of a quote, appended under its rows.
 *
 * WHY A CUSTOMER SEES THIS AT ALL. A spot price is only as good as the money
 * behind it, and one of this terminal's assets is thin enough for that to matter:
 * measured 2026-09-29, GRC turned over 0.0039% of its market cap in a day
 * against Litecoin's 7.5%, and a $100 swap was a third of GRC's entire 24h
 * volume. At that depth the swap is not priced BY the market, it IS the market --
 * and a quote that shows a confident rate with no mention of that is telling the
 * customer something the server knows to be shaky.
 *
 * IT CHANGES NO NUMBER. services/market_context.price_confidence() lists four
 * ways it could be wired; three of them move money (refuse, widen the fee, cap
 * the size) and are the operator's. This is the fourth: display.
 *
 * AND AN UNAVAILABLE READING SAYS SO. Silence would read as "the market is
 * fine", which is the one thing it cannot mean -- no reading is no evidence, not
 * good evidence.
 */
function appendDepth(region, confidence) {
  if (!region || !confidence) return;
  const note = document.createElement("p");
  note.className = "panel-note";
  if (confidence.available !== true) {
    note.textContent =
      "Market depth: NOT MEASURED for this quote (" + (confidence.why || "no reason given") +
      "). The rate above stands; how much money set it is unknown.";
    region.appendChild(note);
    return;
  }
  const legs = confidence.legs || {};
  const parts = [];
  ["from", "to"].forEach(function (role) {
    const leg = legs[role];
    if (!leg) return;
    parts.push(leg.asset + ": " + leg.verdict + " -- " + (leg.reason || "no reason given"));
  });
  note.textContent = parts.length
    ? "Market depth. " + parts.join("  |  ")
    : "Market depth: no leg was readable for this quote.";
  const thin = ["from", "to"].some(function (role) {
    return legs[role] && legs[role].verdict !== "OK";
  });
  // `panel-note warn`, NOT a new class. styles.css:251 already styles exactly
  // this state -- a left rule in the "slow" colour over a soft background -- and
  // index.html's own comments make the same point about `pair-off` and `subtle`:
  // a second vocabulary for one state is rule 8 with a stylesheet attached.
  if (thin) note.className = "panel-note warn";
  region.appendChild(note);
}

// --- swaps -----------------------------------------------------------------

function wireSwapForm() {
  const form = el("swap-form");
  const region = el("swap-result");
  if (!form) return;

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (!latestQuote) {
      say(region, "There is no quote to create a swap from. Get one first; nothing was sent.", "result-error");
      return;
    }
    const addressField = el("payout_address");
    const address = (addressField.value || "").trim();
    if (!address) {
      say(region, "Enter the address you want to be paid at. Nothing was sent.", "result-error");
      return;
    }

    say(
      region,
      "Creating a swap from quote " + latestQuote.id + ": the server checks this " + latestQuote.to_asset +
        " address with that chain's own daemon, then derives a deposit address. That is two RPC calls and can " +
        "take a few seconds.",
      "result-working"
    );

    try {
      const swap = await postJson("/api/swaps", {
        quote_id: latestQuote.id,
        payout_address: address,
      });
      // The status page is a real URL, so going there is a navigation rather
      // than a view swap. It survives closing the tab, which a swap measured in
      // blocks needs.
      say(region, "Swap " + swap.id + " created. Opening its page...", "result-working");
      window.location.href = "/swap/" + encodeURIComponent(swap.id);
    } catch (error) {
      say(region, "No swap was created: " + error.message, "result-error");
    }
  });
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
        const link = document.createElement("a");
        link.href = install;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        link.textContent = "not installed \u2014 get it";
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

wireQuoteForm();
wireSwapForm();
wireLivePolling();
clearBrowserFilledPayoutAddress();
wireCopyButtons();
wireWalletMenu();
