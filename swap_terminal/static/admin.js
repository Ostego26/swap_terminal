/*
 * Progressive enhancement for the operator surface. Read-only, and decides nothing.
 *
 * Role: static asset (behavior only)
 * Reads: /api/admin/chains, and only when the operator asks
 * Writes: nothing but the DOM. There is no POST anywhere in this file, and there
 *         is no route on the admin blueprint that would accept one.
 * Can move funds: no
 * Mainnet-safe: yes
 *
 * The whole /admin page is rendered by the server, so this file has exactly one
 * job: turn the "probe chains" link into an in-place result instead of a
 * navigation. With scripting off the link still works -- it is an <a> to a real
 * GET endpoint, and the JSON it returns is readable.
 *
 * WHY THE VERDICT TEXT COMES FROM THE SERVER. Each row's `detail` string is
 * written by services/admin_view.probe_chain(), not assembled here. "Did not
 * answer" versus "cannot be probed" is a distinction that matters -- a Monero
 * wallet with no probe implemented is not a Monero wallet that is down -- and it
 * is decided in Python where it can be tested, not in a ternary in a browser.
 */

"use strict";

(function () {
  const link = document.getElementById("probe-link");
  const region = document.getElementById("probe-result");
  const script = document.querySelector("script[data-probe-url]");
  if (!link || !region || !script) return;
  const url = script.getAttribute("data-probe-url");

  function line(text, className) {
    const p = document.createElement("p");
    if (className) p.className = className;
    p.textContent = text;
    return p;
  }

  link.addEventListener("click", async function (event) {
    event.preventDefault();
    region.innerHTML = "";
    // Announce BEFORE, with the scale and the cost, because this is the one
    // thing on the page that can take minutes (rule 14).
    region.appendChild(
      line(
        "Probing now: one read-only call per configured chain, each up to its own RPC timeout (30s by default). " +
          "Nothing is signed and nothing is sent.",
        "result-working"
      )
    );
    const started = Date.now();

    let data;
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      data = await response.json();
    } catch (error) {
      region.innerHTML = "";
      region.appendChild(
        line(
          "The probe request itself failed (" + error.message + "). That is this page failing to reach its own " +
            "server, and says nothing about any chain.",
          "result-error"
        )
      );
      return;
    }

    const seconds = (Date.now() - started) / 1000;
    const ufn = (seconds / 1.2096).toFixed(1);
    region.innerHTML = "";
    region.appendChild(
      line(
        "Probed at " + data.probed_at + " -- " + data.probes_attempted + " of " + data.adapters_configured +
          " configured adapter(s) could be probed, in " + ufn + "µfn (" + seconds.toFixed(1) + "s).",
        ""
      )
    );

    const chains = data.chains || [];
    if (chains.length === 0) {
      // `(none)` is a result (rule 14). An empty region here would be
      // indistinguishable from a probe that never ran.
      region.appendChild(line("(none) no chain adapter exists to probe.", "result-idle"));
      return;
    }

    const list = document.createElement("ul");
    list.className = "cards";
    chains.forEach(function (chain) {
      const item = document.createElement("li");
      // Three outcomes, three treatments: answered, did not answer, and was not
      // probed at all. The third is not a failure and must not be styled as one.
      let state = "unknown";
      let word = "NOT PROBED";
      if (chain.probed && chain.reachable) {
        state = "running";
        word = "ANSWERED";
      } else if (chain.probed) {
        state = "stopped";
        word = "NO ANSWER";
      }
      item.className = "card card-" + state;

      const head = document.createElement("div");
      head.className = "card-head";
      const title = document.createElement("span");
      title.className = "card-title mono";
      title.textContent = chain.asset;
      const badge = document.createElement("span");
      badge.className = "badge badge-" + (state === "running" ? "ok" : state === "stopped" ? "halted" : "unknown");
      badge.textContent = word;
      head.appendChild(title);
      head.appendChild(badge);
      item.appendChild(head);

      if (chain.network) {
        item.appendChild(
          line("Network, as the daemon itself reports it: " + chain.network, "card-body")
        );
      }
      item.appendChild(line(chain.detail, "card-note"));
      list.appendChild(item);
    });
    region.appendChild(list);
  });
})();
