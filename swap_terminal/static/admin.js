/*
 * Progressive enhancement for the operator surface. Read-only, and decides nothing.
 *
 * Role: static asset (behavior only)
 * Reads: /api/admin/chains and /api/admin/peg, and only when the operator asks
 * Writes: nothing but the DOM. There is no POST anywhere in this file, and there
 *         is no route on the admin blueprint that would accept one.
 * Can move funds: no
 * Mainnet-safe: yes
 *
 * The whole /admin page is rendered by the server, so this file has exactly one
 * job: turn the two "ask for this" links into in-place results instead of
 * navigations. With scripting off both links still work -- each is an <a> to a
 * real GET endpoint, and the JSON it returns is readable.
 *
 * WHY THE VERDICT TEXT COMES FROM THE SERVER. Each chain row's `detail` string
 * and each peg `finding` line is written in Python -- probe_chain() and
 * wallet_leveling.peg_findings() -- not assembled here. "Did not answer" versus
 * "cannot be probed" is a distinction that matters (a wallet with no probe
 * implemented is not a wallet that is down), and "off peg" versus "not priced"
 * matters the same way. Both are decided where they can be tested with seeded
 * inputs, not in a ternary in a browser (rule 10).
 *
 * ONE PROBE IMPLEMENTATION, TWO CALLERS. wireProbe() below was the chain probe's
 * body until 2026-09-30, when the peg check needed the identical five steps:
 * announce before, time it, replace the region, report a transport failure as
 * THIS PAGE failing rather than as the thing being probed failing, and render
 * `(none)` for an empty answer. Copying it would have been rule 8's shape -- two
 * copies agreeing on the day they are written -- and the drift would have been
 * invisible, because a probe that renders the wrong thing still renders.
 */

"use strict";

(function () {
  const script = document.querySelector("script[data-probe-url]");
  if (!script) return;

  function line(text, className) {
    const p = document.createElement("p");
    if (className) p.className = className;
    p.textContent = text;
    return p;
  }

  /*
   * Wire one link to one read-only GET and render its answer in place.
   *
   * `announce` is printed BEFORE the request and names the scale and the cost,
   * because this is the part of the page that can take minutes and a blinking
   * cursor is what an operator reaches for Ctrl-C over (rule 14). `render` gets
   * (region, data, elapsedText) and owns everything specific to the endpoint.
   */
  function wireProbe(options) {
    const link = document.getElementById(options.linkId);
    const region = document.getElementById(options.regionId);
    const url = script.getAttribute(options.urlAttribute);
    if (!link || !region || !url) return;

    link.addEventListener("click", async function (event) {
      event.preventDefault();
      region.innerHTML = "";
      region.appendChild(line(options.announce, "result-working"));
      const started = Date.now();

      let data;
      try {
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        data = await response.json();
      } catch (error) {
        region.innerHTML = "";
        region.appendChild(
          line(
            "The request itself failed (" + error.message + "). That is this page failing to reach its own " +
              "server, and says nothing about what was being checked.",
            "result-error"
          )
        );
        return;
      }

      const seconds = (Date.now() - started) / 1000;
      // Rule 6: microfortnights in anything a human reads, with the seconds in
      // parentheses, and the symbol is µ and never an ASCII u.
      const elapsed = (seconds / 1.2096).toFixed(1) + "µfn (" + seconds.toFixed(1) + "s)";
      region.innerHTML = "";
      options.render(region, data, elapsed);
    });
  }

  wireProbe({
    linkId: "probe-link",
    regionId: "probe-result",
    urlAttribute: "data-probe-url",
    announce:
      "Probing now: one read-only call per configured chain, plus a second on a Bitcoin-style daemon to name " +
      "its network, each up to its own RPC timeout (30s by default). Nothing is signed and nothing is sent.",
    render: function (region, data, elapsed) {
      region.appendChild(
        line(
          "Probed at " + data.probed_at + " -- " + data.probes_attempted + " of " + data.adapters_configured +
            " configured adapter(s) could be probed, in " + elapsed + ".",
          ""
        )
      );

      // THESE FOUR KEYS ARE BUILT BY stack_authority.chain_probe_envelope(), which
      // routes/admin.py returns verbatim. This file is the only consumer that
      // cannot import the constant naming them, so it is pinned from the other
      // side instead: tests/test_web_surfaces.py::
      // test_the_admin_page_javascript_reads_the_key_python_writes fails if
      // CHAIN_PROBE_ROWS_KEY changes and this line does not.
      //
      // Worth pinning because the OTHER reader of this envelope got it wrong and
      // stayed wrong: swap_stack.py's `up` expected the rows to be the whole body
      // and printed "COULD NOT ASK ... got dict" on a working probe until
      // 2026-10-09. This line was right the whole time, by luck of being written
      // against the route rather than against a belief about it.
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

        // `network` is a NAME or null, never a sentence explaining its absence.
        // A daemon that answered but would not name its network gets no line
        // here and its reason in `detail` below -- rather than the line reading
        // "Network: answered, but reported no chain name", which is what this
        // rendered on the operator's screen on 2026-09-30.
        if (chain.network) {
          item.appendChild(
            line("Network, as the daemon itself reports it: " + chain.network, "card-body")
          );
        }
        item.appendChild(line(chain.detail, "card-note"));
        list.appendChild(item);
      });
      region.appendChild(list);
    },
  });

  wireProbe({
    linkId: "peg-link",
    regionId: "peg-result",
    urlAttribute: "data-peg-url",
    announce: "Pricing USDC and USDT now: two HTTPS reads, no key, no chain.",
    render: function (region, data, elapsed) {
      const asked = (data.assets_asked || []).length;
      const priced = (data.assets_priced || []).length;
      // Echo what decided the answer, not just the answer (rule 14): asking for
      // two and pricing zero is a different fact from pricing two that disagree,
      // and the verdict word alone does not separate them.
      region.appendChild(
        line(
          "Checked at " + data.probed_at + " -- " + priced + " of " + asked + " stablecoin(s) priced, in " +
            elapsed + ".",
          ""
        )
      );
      // SUSPECT IS THE HEADLINE AND IT INCLUDES "COULD NOT CHECK". peg_findings()
      // returns suspect=True for an absent price as well as for an off-peg one,
      // deliberately, because a dollar that was not checked must not render like
      // a dollar that held.
      region.appendChild(
        line(
          data.suspect
            ? "SUSPECT: the dollar every USD figure on this page is quoted in is either off peg or unchecked. " +
                "Read the findings below before trusting a valuation."
            : "The peg holds within tolerance, so the USD figures on this page are quoted in a unit that has not " +
                "moved.",
          data.suspect ? "result-error" : ""
        )
      );

      const findings = data.findings || [];
      if (findings.length === 0) {
        region.appendChild(line("(none) the peg check produced no finding at all, which should be impossible -- " +
          "peg_findings() emits one line per stablecoin plus the ratio.", "result-error"));
      } else {
        const list = document.createElement("ul");
        list.className = "finding-list";
        findings.forEach(function (finding) {
          const item = document.createElement("li");
          item.className = "mono breakable";
          item.textContent = finding;
          list.appendChild(item);
        });
        region.appendChild(list);
      }

      const failures = data.fetch_failures || [];
      if (failures.length > 0) {
        failures.forEach(function (failure) {
          region.appendChild(line("Feed failure: " + failure, "result-error"));
        });
      }
    },
  });
})();
