# Working rules for swap_terminal

Standing instructions from the operator. These apply to every change, not just
the one that prompted them.

Carried over from Project Mammon's CLAUDE.md on 2026-09-24, at the operator's
instruction, and **renumbered to match it** so a reader moving between the two
repositories does not have to relearn the numbering: rule 13 means the same
thing in both. Where a Mammon rule was about Kalshi specifically, it has been
re-grounded in this tree rather than copied as decoration, and the substitution
is named in the rule.

Every measurement below was taken on 2026-09-24 against this tree, with the
denominator stated (rule 3). None of them is inherited prose.

## What this system is, and why that raises the stakes

swap_terminal moves real cryptocurrency between chains. Two implementations
live here side by side:

  Flask app            `app.py`, `routes/`, `services/`, `workers/`, `chains/`
                       quote -> deposit -> payout, backed by SQLite.
  Atomic swap modules  `modules/atomic_*_client.py`, `modules/atomic_htlc_scripts.py`,
                       `modules/atomic_swapper.py` -- HTLC cross-chain swaps on
                       BTC / LTC / GRC.
  Solana/Serum bridge  `grc-sol-swap/abstergo_exchange/` -- an Express server
                       verifying Gridcoin deposits and paying out on Solana.

**The difference from Mammon that changes everything.** Mammon's rule 16 draws
its safety line at "reversible by a deploy versus reversible only by a trade."
Here there is no such thing as reversible by a trade. A broadcast transaction is
final. A revealed HTLC preimage cannot be un-revealed. A payout to a wrong
address is gone, with no exchange to call and no counterparty to unwind it
with. So every rule below that Mammon justifies by "it costs money to undo"
applies here with the stronger justification that it cannot be undone at all.

Three values in this system are irrecoverable if mishandled, and they are named
once here so no rule has to re-explain them:

  the HTLC preimage    revealing it before the counterparty's leg is funded and
                       confirmed hands them both legs. It is the single most
                       dangerous value in the tree.
  a signing key        `SOLANA_PAYER_KEYPAIR_PATH`, the wallet RPC credentials,
                       any WIF. Exposure is total loss of whatever it controls.
  a broadcast tx       amount, fee, address and script are fixed the instant it
                       is relayed.

## 1. Be verbose in comments and docstrings

Write the reasoning down, at length. When a piece of code exists because
something went wrong, the comment names the date, the measured numbers, and
what the wrong behavior looked like from the outside. A future reader needs to
know why the obvious simpler version is wrong, and the only place that survives
is next to the code.

Every module carries a header stating its role, what it reads, what it writes,
whether it can move funds, and whether it is safe to run against mainnet:

```
Role: diagnostics (read-only)
Reads: swap_terminal.db (swaps, deposits)
Writes: nothing
Can move funds: no
Mainnet-safe: yes
```

`Can move funds` replaces Mammon's `Can send orders`, and `Mainnet-safe`
replaces `Live-safe`. Mammon measured that `Live-safe` was the field most often
left off -- thirteen of sixteen partial headers stopped before it -- and it is
the question an operator has to answer before running anything against a wallet
with a balance in it. Same field, same reason, different word.

Measured 2026-09-24: **0 of 45 Python files carry this block.** There is no
backlog to ratchet against yet, which means the cheap moment to start is now
(rule 19: a ratchet holds the past; it is not a place to put work down). Write
one into every file you touch.

Verbosity is never the thing to trim: remove a comment only when deleting the
code it describes.

## 2. Remove dead code and unused files

Delete it, do not quarantine it. Git history is the archive; a file kept "for
reference" is a file the next person has to read, grep past, and reason about.
Two implementations of one thing is worse than either implementation alone,
because the question "which one runs?" has to be answered before any other
question can be.

Before deleting, prove it is dead rather than assuming: grep the whole tree for
the filename, not just the import graph. Shell scripts, `package.json` scripts,
`.desktop` launchers, cron entries and docs all reference files by name in ways
an import-based check never sees.

Deletion is not a license to guess. On a system holding keys, "I could not find
a caller" is not the same as "there is no caller"; say which one you
established.

This tree arrived with the failure already in it. The first push on 2026-09-24
carried `transactions.json.bak`, `transactions.json.pre_cleanup.bak`,
`createMarket.js.bak.2026-03-28_185915`, `createMarket.js.bak.2026-03-28_190340`
and `SwapForm.jsx.bak.2026-03-26_112534` -- five snapshots of files that already
exist in a version control system. It also carried `.env.bak.2026-03-28_185451`,
which is how a live `GRIDCOIN_RPC_PASSWORD` reached GitHub. **The backup habit
is what leaked the credential.** `.gitignore` now blocks `*.bak` and `*.bak.*`;
the habit is what actually has to stop.

## 3. Always be optimizing

Leave the thing faster, smaller, or clearer than it was. Measure before claiming
an improvement, and state the denominator: a count without what it was counted
out of has caused real errors more than once. If a number cannot be measured,
say that instead of estimating and letting the estimate harden into a fact.

## 4. Prefer merging

Merge. Do not rebase, reset, or force-push shared history without being asked.

One exception, already exercised: history was rewritten once on 2026-09-24 to
purge a committed credential, with the operator running the force-push
themselves. Purging a secret is the reason that justifies a rewrite. Tidiness is
not.

## 5. SQL and Python are the main path

Execution, authority and state belong in SQL and Python. `.csv`, `.md`, `.txt`,
`.json` and `.jsonl` are **mirrors** -- exports for other things to read,
operator visibility, diagnostics. They are never the authority for a decision
that moves funds, and nothing on the payout path may depend on parsing one.

The practical tests, when adding or changing a stage:

- Where does the decision live? If a reader has to open a file to learn whether
  a payout is authorized, the authority is in the wrong place.
- What happens if the file is stale, truncated, or half-written? On the money
  path the answer must be "nothing, because nothing reads it for that."
- Could this be a view or a table? If the logic is a filter, a join, or a
  ranking over rows already in `swap_terminal.db`, it probably should be.
- Write SQL first, mirror second. A stage that writes rows as it goes and emits
  the file at the end degrades into a partial result when interrupted. A stage
  that accumulates in memory and writes both at the end loses everything.

**This tree currently violates this rule in four places**, measured 2026-09-24:

    transactions.json            466,196 bytes   TWO writers: transactions.py, clear_prices.py
    gridcoin_transactions.csv     76,461 bytes
    swap_intents.json              2,353 bytes   server.js's authority for live swap intents
    transaction_history.json           2 bytes   empty -- it is `{}` or `[]` and always has been

`swap_terminal.db` already exists (`config.py:8`, `db.py:113`) and is the
authority the tree should be using. `swap_intents.json` is the worst of the
four because `server.js` reads it to decide whether a swap intent is still
valid -- that is a fund-releasing decision being read out of a JSON file.
`transactions.json` is the second worst because it has two writers, which is
Mammon's exact diagnosis: when one artifact has several writers, the question
is not "which writer wins" but "why is a file carrying authority at all."

`transaction_history.json` at 2 bytes is rule 14's defect in file form: it
cannot be distinguished from a file whose writer has been broken since the day
it was created.

## 6. Report timing in microfortnights

**1µfn = 1.2096s.** Every timing this system *reports* -- logs, status lines,
reports, diagnostics, tables -- is in microfortnights.

**The unit is written `µfn`. Never `ufn`, and never with a space before it.**
`2.3µfn`, not `2.3 µfn`, exactly as nobody writes `2 s` for two seconds.
Together: `done in 2.3µfn (2.8s)`. The parenthesised seconds follow the same
rule -- `2.8s`, never `2.8 s`.

µ in anything a human READS, ASCII in anything a machine parses. Identifiers
stay ASCII (`ufn_ttl`, `UFN_SECONDS`) and so do environment variable names -- a
µ in a name an operator has to type, or a shell has to export, buys nothing and
costs a support call.

The boundary is report versus interface. Seconds stay where an external API
demands them: `requests(timeout=)`, `time.monotonic()` arithmetic,
`time.sleep()`, SQLite's `busy_timeout`, `SWAP_INTENT_TTL_MS`, and any variable
whose name already says `_SECONDS` or `_MS`. Converting at those call sites
would introduce rounding into control flow to satisfy a display convention.
Convert on the way *out*, at the print or the row write.

**Blocks and confirmations are NOT times and are never converted.** Six
confirmations is six confirmations. An HTLC locktime expressed in block height
is a block height. Rendering either in µfn would be a category error that
invents precision the chain does not have. Where a locktime is a unix timestamp
and you are reporting how long is left, that duration is a time and takes µfn.

Where both are useful -- an operator reading a log against a `_MS` env var they
may need to edit -- print the µfn figure and put the seconds in parentheses.

There is no `seconds_to_microfortnights()` here yet. Write one, once, in a
shared module, rather than spelling `1.2096` at each call site.

## 7. Learn continuously, and dry-run before risking funds

Mammon's rule 7 is about shadow-trading a family until it earns the right to
trade. The transferable half is the posture: **nothing touches mainnet until it
has been proven somewhere it cannot lose money.**

- Testnet and regtest first. BTC/LTC testnet, Solana devnet, a local
  `solana-test-validator`, Gridcoin testnet. A swap path that has never
  completed end to end on a test chain is not ready for a real one.
- Every swap attempt is evidence whether or not it succeeded. Record the
  outcome -- contract txid, redeem txid, refund txid, timings, fees actually
  paid -- in SQL, append-only. Failed swaps are the more valuable half: a
  refund that had to be claimed tells you a timelock or a confirmation wait was
  wrong.
- Never delete that record to save space. If disk is the problem, the answer is
  a churn table, not the record of what the system did with money.
- A measurement that only exists in a log is not learning. If it decides
  something, it belongs in SQL where the next run can read it (rule 5).

## 8. Always be looking for duplicative logic to merge, modularize and optimize

Two copies of one rule is not redundancy, it is a bug with a delay on it. The
copies agree on the day they are written and drift from then on, and the drift
is invisible: each looks correct in its own file, and nothing fails until a
decision made through copy A contradicts a decision made through copy B.

**This is the largest structural problem in the tree.** Measured 2026-09-24,
there are TWO complete and independent RPC client families with zero shared
code:

    chains/base.py                  128 lines   class RPCAdapter
      chains/bitcoin.py               4 lines     class BitcoinAdapter(RPCAdapter)
      chains/litecoin.py              4 lines     class LitecoinAdapter(RPCAdapter)
      chains/gridcoin.py              4 lines     class GridcoinAdapter(RPCAdapter)
                                    ---
                                    140 lines total

    modules/rpc_clients.py          150 lines   class RPCClient
      modules/atomic_btc_client.py  248 lines
      modules/atomic_ltc_client.py  293 lines
      modules/atomic_grc_client.py  310 lines
                                    ---
                                   1001 lines total

The `chains/` family is the good shape and is 140 lines: one base class, three
four-line subclasses that differ only by an `asset` string. The `modules/`
family is 1001 lines across four files for the same three chains, and the three
`atomic_*_client.py` files differ from each other by 45 and 62 lines -- close
enough to be near-copies, far enough apart that at least one of them disagrees
with the others about something. Which one is the odd one out, and on what, is
an open question this rule exists to make someone answer.

Mammon's weather-city bug is the cautionary case: one regex defect, three
copies, three separate diagnoses, three commits, three tests, for one bug --
and the second and third were only found because someone went looking.

So when you touch a rule, grep for it. If a second implementation exists:

- Merge them, and let the survivor own the concept. `chains/base.py`'s
  `RPCAdapter` is the survivor for connection handling; the atomic-swap-specific
  methods belong on top of it, not beside it.
- If they genuinely differ, the difference is the point and belongs in a comment
  at BOTH sites, naming the other one. A reader who finds one must be told the
  other exists.
- Deriving one from the other beats maintaining both.

## 9. Always be culling dead code and files

Rule 2 says how to delete safely. This is the standing habit: every time you
are in a file, leave less of it behind. Dead code is not inert. It gets read,
greped past, copied from, and eventually maintained -- and the copy made from a
dead function is a live bug with a dead parent nobody thinks to check.

The cull runs alongside rule 8's merge, because consolidation CREATES dead code:
the moment two client families become one, imports, helpers and fixtures are
orphaned. Merging without culling leaves the old copies looking authoritative,
which is worse than either the duplication or the deletion alone.

What to look for while you are already in the file: imports nobody uses; a
helper whose only caller you just deleted; a test pinning behavior that has been
replaced rather than moved; a constant duplicated into a module that now lives
in a shared one; files whose whole reason was a migration that completed.

And rule 2's guard applies to every one of them: grep the tree for the NAME, not
just the import graph.

## 10. Layer by decision distance: suite -> file -> module -> submodule -> function

Each layer should only know about the one below it, and the thing that actually
decides should be the smallest, most testable piece at the bottom.

  suite       a set of files that together do one job. The Flask app, the
              atomic-swap modules and the Solana bridge are three suites.
  file        the entry point, and it lives at the ROOT. If an operator, a cron
              entry or a launcher names it, it belongs where it can be found
              without knowing the layout.
  module      what a file runs. Owns a stage, holds no decision of its own.
  submodule   what a module runs. Same rule, one level finer.
  function    what a submodule calls. This is where a decision lives, and the
              only level that may contain one.

Why this shape: every defect worth fearing here is a DECISION that ended up
somewhere it could not be seen or tested -- a fee chosen inside a broadcast
branch, a timelock derived inside a formatting helper, a confirmation threshold
inlined into a loop. When the decision is a function at the bottom, it can be
called with seeded inputs and asserted on directly. When it is buried three
levels up inside orchestration, the only way to test it is to run the whole
thing against a real chain, and the only way to find it is to already know it is
there.

Honest state of the tree, so nobody reads this as a description: it is not
arranged this way. Measured 2026-09-24, eleven `.py` files sit at the package
root -- `PID_ringdown.py`, `atomic_swap_gui.py`, `clear_prices.py`,
`identity.py`, `orderbook_grc.py`, `test.py`, `transactions.py` and others --
and they are a mix of entry points, libraries and experiments with no marker
saying which is which. `transactions.py` alone is large enough to contain its
own GUI thread (`transactions.py:726`). Treat rule 10 as the direction for new
code and for whatever you are already touching, not as license for a mass
reorganisation.

## 11. One vocabulary for assets and chains, derived in one place

Mammon's rule 11 normalizes Kalshi settlement cadence across every ticker. The
structural rule transfers even though the subject does not: **one vocabulary,
derived in ONE place, applied identically everywhere.**

Here the subject is the asset/chain identifier and its decimal precision. `BTC`,
`LTC`, `GRC`, `SOL` and the SPL mints each have a native precision -- 8 decimal
places for the Bitcoin-derived chains, 9 for SOL, per-mint for tokens -- and a
value that crosses a boundary with the wrong precision is silently wrong by
orders of magnitude. Mammon's `15M`-resolving-to-monthly defect is the same
shape: a marker that resolves to the wrong token silently merges two
populations under one key, and the key decides whether money moves.

The practical tests when adding or touching an asset:

- Is the new asset in the single shared table, with its decimals, its
  confirmation requirement, and its dust threshold?
- Does the identifier mean the same thing in Python and in JavaScript? The
  Flask app and the Express server both name chains; if they disagree about
  what `GRC` implies, one of them is wrong and neither will say so.
- Did every consumer follow automatically? If a chain had to be added to a
  second place by hand, that second place is the bug.
- Is the conversion between a human amount and a base unit done once, in one
  function, rather than by multiplying by a literal at each call site?

Currently the three `chains/*.py` subclasses each carry a bare `asset = "BTC"`
string and nothing else -- no decimals, no confirmation count, no dust limit.
That is the right place for the table; it is just empty.

## 12. Conform to a declared standard

Measured 2026-09-24: there is **no `pyproject.toml` and no `tests/` directory**
in this repository. So unlike Mammon, this rule cannot start by pointing at an
existing declaration. Create one, and once it exists it is the standard and is
not renegotiable mid-change:

    python3 -m ruff check <the files you touched>

Clean before committing, and clean the files you TOUCHED -- not the tree. A
sweep across files you were not otherwise in is a large diff on a
key-holding system with no behavioral benefit.

The rules that cost the most, and why:

- **`BLE001`, blind except.** Measured 10 bare `except:` / `except Exception:`
  in 45 Python files, and 45 `catch` blocks across 28 JS files. A broad catch is
  legitimate when a diagnostic must not die on a bad row. It is never legitimate
  when the caller cannot tell the failure from a real answer -- and on this
  money path that distinction is the whole game. `except Exception: return 0`
  around a confirmation count reads to the caller as "zero confirmations,"
  which is indistinguishable from a real unconfirmed deposit and will either
  stall a payout forever or, if the sign is the other way, release one early.
  If you catch broadly, the handler must say so in its return value or on the
  way out.
- **`S608`, SQL built by interpolation.** Values go in as parameters. A `noqa`
  marks "identifier, not input," and a reviewer should be able to see which from
  the line.
- **`C901`, complexity.** A `main()` past the ceiling is orchestration that has
  swallowed decisions, which is rule 10's defect wearing a lint code. The fix is
  to extract the decision so it can be called with seeded inputs, not to raise
  the ceiling.
- **`F841`/`F401`, dead names.** These are rule 9 automated.

Two things the linter cannot check and this rule still asks for. Import-time
side effects: a module that mutates `os.environ` or touches the filesystem when
imported makes every later import order-dependent. And use the domain API over
the general-purpose one: `shutil.copy2` on a WAL-mode SQLite database silently
drops a third of it, where `Connection.backup()` is correct by construction.

## 13. Every spawn needs a reaper, and stop must be proven to reach it

The distinctive damage is that an orphan does not crash anything. It holds a
lock, or a wallet, and everything downstream reports success while doing
nothing -- or worse, two copies of a payout worker both decide the same swap is
unpaid.

**Measured 2026-09-24, this tree has three unreaped loops and no supervisor:**

    workers/deposit_watcher.py:11    while True:  ... time.sleep(poll_seconds)
    workers/payout_worker.py:11      while True:  ... time.sleep(poll_seconds)
    workers/reconcile_worker.py:12   while True:  ... time.sleep(poll_seconds)

`app.py:43` calls only `app.run(debug=True, host="0.0.0.0", port=5000)`.
**Nothing in the tree starts these workers, and nothing stops them.** There is
no pid file, no supervisor, no kill list. Separately,
`transactions.py:726` starts a `threading.Thread(daemon=True)` whose only reaper
is process exit.

Two consequences worth stating plainly, because they are what this rule exists
to prevent:

- A payout worker started by hand in a second terminal is invisible to the
  first. Two payout workers polling the same SQLite database will both read the
  same pending swap. Without a database-level state transition guard, that is a
  double payout, and it is on-chain and final.
- `app.run(debug=True, host="0.0.0.0")` binds every interface with the Werkzeug
  debugger enabled. The debug reloader also spawns a second process, which is a
  second unreaped spawn. On a host with wallet RPC access this is the highest
  severity line in the repository, and it is one keyword.

So, when you add anything that spawns:

- Name the reaper in a comment at the spawn site, and add the pattern or pid
  file to the stop path in the same commit. A spawn and its reap are one change.
- Prefer a pid file to a `pgrep -f` pattern. A pattern matches what the command
  line happens to look like today; renaming a script silently orphans it.
- A stop that cannot prove it worked is not a stop. Follow it with a check that
  the process is gone, and make the absence the assertion -- not the exit code
  of the kill.
- Treat "skipped" plus "success" in the same output as a defect in the output.
- Guard the work, not just the process. A single-instance lock is good; a
  database constraint that makes a second payout impossible is better, because
  it survives the case where the lock was wrong.

## 14. Silence is a defect. Say what you are doing while you do it

An operator watching a blinking cursor cannot tell working from hung, and the
way that resolves is Ctrl-C. Here that can mean killing a process between
broadcasting a funding transaction and recording it -- money on chain, no row
in the database.

The conditions that produce it are everywhere in this tree: a deposit watcher
polling for confirmations has nothing to say for tens of minutes by design; an
HTLC refund wait is measured in hours; a chain RPC that has stopped responding
looks exactly like a chain that is quiet.

So, for anything that runs in a terminal or gets pasted back:

- **Announce before, not only after.** Print the target and the scale up front
  -- which chain, which network, which database, how many pending swaps.
  **Always print the network.** A line that does not say `mainnet` or `testnet`
  is a line that will eventually be read as the wrong one.
- **Emit progress on anything that can exceed a couple of seconds**, with a
  counter and elapsed time (`waiting for confirmations 3/6, 412µfn`).
- **Never let an empty result print nothing.** `(none)` is a result; a blank gap
  is ambiguous between zero rows and a query that broke.
- **State what the number means, next to the number.** `pending_payouts=0  <-
  expected non-zero while swaps are open; 0 may mean the worker is not running`.
- **Make "did nothing" look different from "did work."** A poll that found
  nothing and a poll that paid someone must not share a success line.
- **Echo the parameters that decide the answer** -- network, database path,
  confirmation threshold, fee rate. Pasted output has to be self-describing a
  day later, because it usually is read a day later.
- **Never print a secret.** This is where rule 14 and the preimage rule meet: a
  progress line that helpfully echoes the swap's secret has just published it.
  Log the secret *hash*, never the secret.

## 15. One database is the authority. Everything else is a buffer with one reader

`swap_terminal.db` is the only place a decision may be made from.

Measured 2026-09-24, the tree does not believe this yet. `swap_terminal.db`
exists and is wired (`config.py:8` defines `SWAP_DB_PATH`, `db.py:113` connects
it), and alongside it sit four files carrying state that decisions are read
from, listed in rule 5. The Express server in `grc-sol-swap/` has no connection
to `swap_terminal.db` at all -- it keeps its swap intents in
`swap_intents.json`. So there are currently two systems of record for swaps,
and nothing reconciles them.

Before adding any database or state file:

- **Is it an authority or a buffer?** An authority is forbidden. There is one.
  A buffer is allowed only to keep a concurrent writer off the main lock, and
  the justification goes in a comment at the connect site.
- **Exactly one reader.** A buffer that a second consumer starts reading has
  become a source of truth, and the two will disagree.
- **No fund-moving decision may be read from one.** If a gate has to open a
  JSON file to learn whether a payout is authorized, the authority is in the
  wrong place.
- **Something must name it and fold it in.** A staging file with no importer is
  a leak, and it ages silently.

SQLite has ONE writer lock. If a daemon writing constantly would starve the app,
give it a buffer and one importer -- but know what Mammon measured on
2026-08-28: splitting one contended file into four did not remove the
contention, it moved it. Four files were not better than one. The constraint to
satisfy is "keep the concurrent writer off the authority's lock," not "give
everyone their own file."

## 16. Fix what you find. Surface only what moves money

Standing authorization: when you find a bug while doing something else, fix it
in the same pass. Do not stop to ask, do not file it as a note, do not hand it
back with proof attached and wait. The proof took as long as the fix.

WHAT STILL COMES BACK, and the line is money rather than risk-of-being-wrong:

  fund movement     anything changing what gets sent, to which address, in what
                    amount, at what fee, or whether a swap may proceed at all --
                    the payout path, signing, fee selection, address derivation,
                    confirmation thresholds, timelock values.
  armed state       funded HTLCs awaiting redeem or refund, pending payouts,
                    open swap intents, anything canceling or resizing them.
  secrets and env   `.env`, keys, keypair files, wallet RPC credentials, live
                    runtime state. Never read one back into a terminal, and
                    never print one into a log or a commit.
  a fix you cannot  if the change cannot be tested here, it is a proposal and
  test              not a fix. Say which.

Everything else -- diagnostics, reporting, dead code, duplicated logic, wrong
comments, lint, tests, anything off the fund-moving path -- is yours to fix on
sight, under the standards already in this file: a test that fails without the
fix, the reasoning written down next to the code.

A wrong comment is a bug. A docstring that says a function is testnet-only when
it reads a mainnet RPC endpoint will eventually be trusted by someone in a
hurry. Fix the sentence with the same seriousness as the code, and say which of
the two was wrong.

## 17. Never infer or assume. Test

The failure is not being wrong. It is presenting a hypothesis in the register of
a measurement, so the reader cannot tell which they are holding.

Before a claim about what the system is doing, run the thing that would show it
false. Then say which you have. If it cannot be tested from here, say THAT --
rule 16 already draws the line between a fix and a proposal, and this draws the
same line one step earlier, between a finding and a hypothesis.

"I could not find a caller" is not "there is no caller" (rule 2). This is the
general form: a reason to believe something is not the same as having checked
it, and the two must never be written in the same voice.

On a chain this is unusually cheap to honor. Block explorers, `getrawtransaction`,
`gettxout` and a testnet faucet will settle most questions in under a minute.
"The refund path works" is a claim that should be backed by a testnet txid.

## 18. All English is American English

Behavior, not behaviour. Authorized, not authorised. Canceled, labeled,
modeling, judgment, gray, center, analyze.

PROSE ONLY, AND NEVER IDENTIFIERS. This is rule 6's boundary applied to a second
alphabet: American spelling in what a human reads, and nothing at all in what a
machine parses. A Solana or Kalshi API field, an RPC method name, a
`behaviour=` keyword argument in a third-party library -- those stay exactly as
the external system spells them.

Measured 2026-09-24: **0 British spellings** across the `.py`, `.js` and `.md`
files in this tree. This starts as a clean gate rather than a backlog, and the
cheapest moment to keep it clean is every commit from here.

## 19. No patches. No new ratchets. Fix the code

A ratchet is a per-file baseline a hygiene test compares against, so a NEW
violation fails while the existing backlog is tolerated. It is legitimate for
exactly one thing: stopping a measured defect class from GROWING on the day you
first measure it. It is not a place to put work down.

Mammon's experience is the argument: three ratchets, added with good intent, and
nothing came off any of them until somebody was told to. A baseline makes the
check green, green looks like done, and the backlog stops being visible as work.

**THE RULES, and they are absolute:**

- **Never add a baseline line for code you are writing now.** If your change
  needs a new entry to pass, your change is the defect.
- **Never add a suppression to make a check pass.** `noqa` is a claim you
  checked, and the comment beside it says what you checked.
- **When you find N instances, fix them.** Not one. If N is too large for one
  pass, fix every instance in the files you TOUCHED, and say what remains and
  where -- but the remainder is named work, not a new baseline.
- **A ratchet that reaches zero gets DELETED, along with its baseline file.**
- **The test for whether something is a patch:** does it stop the symptom being
  reported, or does it stop the cause existing? Only the second is a fix.

This repository has no ratchets and, measured above, two clean gates already
(module headers at 0/45 written, British spellings at 0). Keep it that way. The
counts in rules 8, 12, 13 and 15 are named work, not baselines.

## 20. SQL always wins. Do not ask which

Rule 5 asks "could this be a view?" as a test. This answers it: yes, unless you
can say why not. A filter, a join, or a ranking over rows already in
`swap_terminal.db` is a query. A per-row Python loop over the same rows is the
same logic in a place only its author can inspect -- and a loop with one
try/except around it has half-applied state on any row that raises, where a SQL
predicate has none.

**DO NOT ASK WHICH.** When the answer is knowable from the tree, establish it
and act. Say what you established and what you did. The line that still comes
back is rule 16's and it has not moved -- fund movement, armed state, secrets,
and a fix you cannot test here.

The corollary, and it is the harder half: "figure it out" is not license to
guess. Rule 17 is unchanged. A decision made from a measurement is what this
rule asks for; a decision made from a plausible reading is the thing it is
easiest to mistake for one.

**WHAT THIS DOES NOT MEAN.** Not a mandate to rewrite working Python into SQL on
sight. It governs where NEW logic goes, and which way to resolve a duplicate you
are already touching. A derivation SQLite cannot express -- an RPC call, a
signature, a script assembly -- stays in Python and says so at the site.

---

## Chain-safety rules

These are this repository's equivalent of Mammon's live-safety rules, and they
apply to inspection, testing, bounded fixes and proposals alike:

- **Default to testnet.** Any command, script or test whose network is not
  explicitly stated is assumed to be pointed at a real chain and is not run.
- **Never broadcast without being asked.** Build, sign and print a raw
  transaction for the operator to inspect; broadcasting is theirs.
- **Never reveal a preimage.** Not in a log, not in a commit, not in a pasted
  diagnostic, not in an error message. Log `secret_hash`, never `secret`.
- **Never move, copy, or read back a key.** Not `.env`, not a keypair JSON, not
  a WIF, not `wallet.dat`. If a task seems to need one, it is a proposal for the
  operator, not a step to take.
- **Do not cancel, replace, or fee-bump a pending transaction** without being
  asked.
- **Do not claim a refund or redeem a contract** as a side effect of testing.
- Prefer read-only inspection first. Fixes should be small, reversible in code,
  and backed by tests.

## Verify by behavior, never by reading the code

**Verify by observed outcome, never by reading that the code looks right.**

1. Seed real (or realistically synthetic) rows into the real tables, or fund a
   real contract on a test chain.
2. Run the real script -- not a paraphrase of its logic, not a hand-copied
   fragment of its SQL, not a reimplementation in the test.
3. Query the real output table, or the real chain, and assert on what is
   actually there.
4. Never accept "the code contains a check for X" as evidence X is enforced.
   Only "condition X produced or suppressed outcome Y" counts.

For HTLC work specifically, the only honest proof that a contract is correct is
that both of its branches were exercised on a test chain: a redeem with the
preimage, and a refund after the timelock expired. A redeem script that has only
ever been exercised on the happy path is a refund path that has never been
tested, and the refund path is the one that runs when something has already gone
wrong.

---

## Context that shapes all of this

swap_terminal moves real cryptocurrency, and an on-chain action is final. The
safety invariants in the payout and signing code are not obstacles to route
around, and weakening one to make a test pass is never the fix.

The operator runs commands on their own machine and pastes the output back.
Every diagnostic has to be a single pasteable block that answers a question the
operator can read off the screen, that states its network and its database, and
that is safe to run against a live wallet unless it says otherwise.

Test before shipping, and diff the full suite line by line against a recorded
baseline rather than comparing failure counts. Counting failures hides a new
break that lands the same day an old one is fixed.

---

## Re-measured 2026-09-24, after the first enforcement pass

Every figure above was taken on 2026-09-24 before any of these rules had been
applied to the tree. An enforcement pass ran the same day and moved several of
them. The originals are kept exactly as written rather than overwritten,
because the drift is the point (rule 1) -- and here the drift happened in
hours, not days.

    rule  measurement                            was            now
    1     Python files with a module header      0 of 45        41 of 41 in
                                                                swap_terminal/,
                                                                10 of 10 in tests/
    12    pyproject.toml                         absent         present
    12    tests/ directory                       absent         83 tests, all passing
    12    ruff findings under the new standard   270 (first     0
                                                 measurement)
    13    unreaped worker loops                  3, no supervisor   supervisor.py,
                                                                    pid files, a stop
                                                                    that proves absence
    18    British spellings in .py/.js/.md       0              3, ALL OF THEM IN THIS
                                                                FILE, now fixed

The denominator moved too, which is why rule 1's count cannot be read as
"45 became 45": six files were deleted as proven dead (modules/rpc_clients.py,
modules/bitshares_client.py, modules/module.py, models/__init__.py,
PID_ringdown.py, clear_prices.py) and two were added (microfortnights.py,
supervisor.py), so 45 - 6 + 2 = 41. Stating the count without the denominator
would have hidden the cull entirely.

THREE MEASUREMENTS IN THE RULES ABOVE TURNED OUT TO BE WRONG, and they are
corrected here rather than in place:

  rule 5 says transactions.json has "TWO writers: transactions.py,
  clear_prices.py". Measured by grepping the whole tree for the filename:
  clear_prices.py wrote to a hardcoded
  /home/mpjones26/Documents/Prototypes/transactions.json -- a DIFFERENT file,
  outside this repository. swap_terminal/transactions.json has one writer and
  no reader other than its writer, which makes it a single-writer cache rather
  than a contested authority. It is still a rule 5 item; it is not the second
  worst one.

  rule 8 presents modules/ as one family of 1001 lines rooted at
  rpc_clients.py. Measured: NOTHING imports rpc_clients.py. The three
  atomic_*_client.py files each define their own rpc_call and never touch it,
  so it was a fourth independent JSON-RPC implementation that nothing used. It
  has been deleted. The real count of live JSON-RPC implementations against a
  Bitcoin-style daemon in this tree is FIVE in Python (chains/base.py, three
  atomic clients, identity.py) plus an inline requests.post in transactions.py,
  one in JavaScript (server.js) and six curl invocations in chain_tx.sh.

  rule 18 says 0 British spellings. Measured with a 40-pattern scan over all
  84 .py/.js/.jsx/.md files: 3, every one of them in CLAUDE.md itself --
  "licence" twice and "cancelling" once. Fixed in the same commit that found
  them. (Three further matches are the rule's own examples, which quote the
  British spelling in order to name it, and are left alone.) The file that
  states the rule is the one place a violation refutes itself.

WHAT DID NOT MOVE, and is named work rather than a baseline (rule 19):

  - swap_terminal/important and swap_terminal/test.py both contain plaintext
    private keys, and so does this repository's history. NOT deleted: deleting
    would make the tree look clean while the keys stayed published. Remediation
    is the operator's -- sweep, rotate, then purge history (the one rewrite
    rule 4 allows).
  - the HTLC refund branch is unspendable: the locktime is encoded as a varint
    rather than a script number, so a requested height of 500,000 is enforced
    as 128,000,254. Measured in tests/test_htlc_locktime_encoding.py.
  - two payout workers pay the same swap twice. Measured in
    tests/test_payout_concurrency.py, with the claim-by-UPDATE and the partial
    unique index that would prevent it demonstrated against a real database.
  - grc-sol-swap/abstergo_exchange/swap_intents.json is still the Express
    server's authority for a fund-releasing decision.
