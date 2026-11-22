# Credentials in this repository's history: measured state, and what remains

Role: documentation (reference), and the hand-off for the one item here that is
      not a session's to perform
Reads: nothing at runtime
Writes: nothing
Can move funds: no
Mainnet-safe: yes

Measured 2026-11-22 with git itself, and **without reading any secret value**:
`--name-only` and `git grep -l` print paths, never content. The chain-safety rules
forbid reading a credential back, and that constraint is why parts of this document
end in a question for the operator rather than an answer.

## What CLAUDE.md said, and which thirds of it are still true

CLAUDE.md's standing item read: *"swap_terminal/important and swap_terminal/test.py
both contain plaintext private keys, and so does this repository's history. NOT
deleted: deleting would make the tree look clean while the keys stayed published."*

That is three claims. Two are now false.

**The two files are gone, from more than the working tree.**

    git log --all --pretty=format: --name-only --diff-filter=A | sort -u \
      | grep -E '(^|/)important$|(^|/)test\.py$'      ->  no output

No path named `important` and none ending `/test.py` appears anywhere in the
reachable history under those names. They are not merely deleted from HEAD.

**The `.env` backup that leaked the live credential is gone too.** Searching every
commit's filenames for secret-shaped names:

    .env|\.bak|wallet\.dat|\.pem$|keypair|id_rsa|privkey
      ->  .env.example                              a template
          swap_terminal/chains/solana_payout_keypair.py   a module, not a key

No `.env.bak`, no `*.bak`, no `wallet.dat`, no `*.pem`, no `id_rsa`. The
`.env.bak.2026-03-28_185451` that CLAUDE.md rule 2 records as *"how a live
`GRIDCOIN_RPC_PASSWORD` reached GitHub"* is not in the history any more. The
2026-09-24 force-push did what rule 4 licensed it to do.

**AND THE NOTE'S OWN WARNING CAME TRUE WHILE NOBODY WAS WATCHING.** It said the
files were deliberately *not* deleted, because deleting them *"would make the tree
look clean while the keys stayed published"*. They have since been deleted. A
reader who trusted that paragraph would have concluded the keys were still sitting
in the tree, and would have been wrong in the direction that matters least --
but anyone reasoning the other way, from "the files are gone" to "the keys are
gone", would have been wrong in the direction that matters most.

## What IS still there, and it is the only open part

`e48e81e` ("Add atomic swap modules, Solana/Serum exchange, Gridcoin orderbook",
66 files) **is an ancestor of HEAD**, and four files at that commit assign an RPC
password in source:

    swap_terminal/grc-sol-swap/abstergo_exchange/server.js
    swap_terminal/identity.py
    swap_terminal/modules/rpc_clients.py
    swap_terminal/transactions.py

Those are filenames from `git grep -l`. **Whether any of the four holds a LIVE
credential rather than a regtest one was not established, because establishing it
means reading the value, and reading a credential back is forbidden outright.**
The current tree also matches that pattern in several places
(`capture_daemon_commands.py`, `swap_terminal/regtest/daemons.py`,
`swap_terminal/chain_tx.sh`, the runbook) and those are regtest daemon passwords
for local nodes, which are not secrets of value. The four above are from 2026-03,
before the regtest harness existed, which is the reason to look rather than a
finding.

## The remediation, and why no session can do it

Three steps, in this order, and the order is the whole point:

1. **Rotate.** Change the Gridcoin RPC password (and anything else those four files
   turn out to hold) on the daemon and in `.env`. Until this is done, purging the
   history only hides a credential that still works.
2. **Purge.** Rewrite the history to remove the values. `git filter-repo` is the
   supported tool; the 2026-09-24 precedent is the one rewrite CLAUDE.md rule 4
   allows, and tidiness is still not a reason.
3. **Force-push, and re-clone every working copy.** A rewrite changes every commit
   id from the oldest rewritten commit forward, so existing clones -- including the
   operator's own `~/Documents/swap_terminal` -- must be re-cloned, not pulled.

**Step 1 cannot be done from a session**: rotating a credential requires knowing
and setting the new one, and a session may neither read nor write key material.
**Step 3 is the operator's by precedent** -- CLAUDE.md records that the 2026-09-24
force-push was run by the operator themselves -- and by consequence: it invalidates
their checkout, so it must happen when they are at the keyboard.

What a session *can* do, and what this document is, is establish exactly what is
left, so the sweep is a short list rather than a search.
