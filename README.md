# swap_terminal

## Running the API: which of the two servers you are actually on

There are two ways this application serves HTTP and they are not
interchangeable. The banner each one prints says which you are looking at.

    production      gunicorn -c gunicorn.conf.py wsgi:app
    development     python3 swap_terminal/app.py

**gunicorn is the production server.** `wsgi.py` at the repository root is the
entry point it imports, and `gunicorn.conf.py` beside it carries the settings.
Under gunicorn, `app.py`'s `if __name__ == "__main__":` block is **never
reached** -- which is strictly better than relying on its defaults, because
nothing is then depending on a keyword argument being right. `app.py` keeps
that block for local development and it is unchanged.

`app.py`'s `debug` and `host` handling still does its job, and its job is now
narrower. It governs the development server only:

    SWAP_TERMINAL_DEBUG   unset/off by default. Turning it on starts the
                          Werkzeug interactive console, which executes Python
                          as a process holding the wallet RPC credentials.
                          gunicorn has no equivalent and never runs it -- so
                          under gunicorn this variable does nothing at all.
    SWAP_TERMINAL_HOST    127.0.0.1 by default. gunicorn.conf.py reads THE SAME
                          variable for its `bind`, deliberately, so that moving
                          from one server to the other does not silently change
                          what the API is reachable from.
    SWAP_TERMINAL_PORT    5000 by default, and also shared with gunicorn.

Gunicorn-only settings, all with defaults that need no configuration:

    GUNICORN_WORKERS           2. Sync workers. See the warning below.
    GUNICORN_TIMEOUT_SECONDS   60, above the 30s default of the wallet RPC
                               calls a request can be waiting on.
    GUNICORN_LOG_LEVEL         info.

### The WSGI process serves HTTP and starts no workers

**Gunicorn forks N worker processes and every one of them imports the
application.** Anything started at import time, or inside `create_app()`, is
therefore not started once -- it is started **N times, in N processes**, and
nothing crashes and nothing logs an error.

This repository has already measured what the plural form of that costs. Two
payout workers paid the same swap twice: 2 sends, 1 swap id, 2 `broadcast`
rows, on-chain and final. Those were two processes started by hand.
`GUNICORN_WORKERS=4` would make four the default, silently, on every restart.

So the division of labor is fixed and no setting changes it:

| | |
|---|---|
| the gunicorn process | serves HTTP. Starts no workers, ever. |
| `swap_terminal/supervisor.py` | owns the deposit watcher, the payout worker and the reconcile worker, and is their reaper. |

`preload_app`, `workers`, `threads` and the worker class are tuning for the
first row. **`preload_app` is not a fix for a background thread** and it is
worth saying because it looks like one: only the forking thread survives
`fork()`, so preloading turns "N copies running" into "zero copies running, in
a parent that has forked away from them". A different wrong answer, not a right
one.

Two mechanisms enforce this, and neither of them is this paragraph:

- `wsgi.py` snapshots the process's threads and child pids around
  `import app` and **raises** if either grew. It runs in every worker, because
  every worker imports `wsgi`. A worker that spawned something during import
  never serves a request.
- `gunicorn.conf.py`'s `post_fork`/`post_worker_init` hooks re-measure across
  the fork -- the window `wsgi.py` cannot see -- and print the result either
  way, so a clean boot says `spawn guard: 0 threads, 0 child processes started`
  rather than saying nothing.

`tests/test_wsgi_spawn_guard.py` holds it in the suite, including a test that
starts a real thread in a real subprocess and asserts the real guard refuses.

Start the workers separately, and only ever once:

    python3 swap_terminal/supervisor.py start
    python3 swap_terminal/supervisor.py status
    python3 swap_terminal/supervisor.py stop

## Regtest HTLC verification

`regtest_htlc_verify.py` at the repository root drives the real HTLC modules
against local `bitcoind` and `litecoind` regtest daemons and reports which
branch of a funded contract actually spends. It is not a pytest test -- it
needs two external daemons, starts processes, and mines four-figure numbers of
blocks -- so it lives at the root and is run by hand:

    source .venv/bin/activate
    python3 regtest_htlc_verify.py --wipe

It REFUSES to run against any chain whose `getblockchaininfo` reports anything
but `regtest`, unconditionally and with no flag to override. See the module
docstring for the flags and the `ST_REGTEST_*` environment variables.

The parts of it that can be proven without a daemon -- the scriptSig layout,
the sighash, the key and address encodings, the verdict wording -- are covered
by `tests/test_regtest_harness_units.py` and run with the normal suite.

## The deposit vout artifact

`migrate_deposit_vouts.py` at the repository root reports -- and, when you name
rows, deletes -- the fabricated `vout=0` rows in `deposit_events`.

Before 2026-09-25, `chains/base.py::_extract_matching_vouts()` matched a
transaction's outputs on `scriptPubKey.addresses`, a field removed in Bitcoin
Core 22.0. On a Core 22+ node nothing ever matched, so every deposit was
credited by the fabricated fallback beside it: `vout=0`, with the amount from
the wallet's `listtransactions` summary. The search now finds the real output,
and `deposit_events` has `UNIQUE(asset, txid, vout)` -- so a deposit whose real
output is at `vout=N` INSERTS a second row rather than updating the old one,
and `refresh_swap_from_chain()` sums both. The swap's total doubles and it
halts in `under_review`. That is a halt rather than a wrong payout, but the
swap stops moving until somebody clears it.

Dry run by default. It opens the database `mode=ro`, so SQLite -- not the
script's control flow -- enforces that:

    source .venv/bin/activate
    python3 migrate_deposit_vouts.py --db /path/to/swap_terminal.db

Stop the workers first if you intend to apply anything
(`python3 swap_terminal/supervisor.py stop`): a `deposit_watcher` poll will
re-insert a row the migration has just deleted.

It does not decide which rows are fabricated, and it cannot. One transaction
may legitimately pay the deposit address twice, which produces exactly the same
shape, so the dry run prints every row with its `first_seen_at` and both totals
-- with and without the `vout=0` row -- and you decide. Deleting takes both
`--apply` and an explicit `--delete-event-id` per row; a backup is taken with
`sqlite3.Connection.backup()` first, and its path is printed.
