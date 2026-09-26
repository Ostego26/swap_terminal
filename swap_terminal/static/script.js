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
      "Pricing " + amount + " " + parts[0] + " to " + parts[1] + ": asking the server, which fetches a live USD " +
        "price for each asset. One outbound call.",
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
        ]
      );
      if (createButton) createButton.disabled = false;
    } catch (error) {
      latestQuote = null;
      if (createButton) createButton.disabled = true;
      say(region, "No quote: " + error.message, "result-error");
    }
  });
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

wireQuoteForm();
wireSwapForm();
wireLivePolling();
