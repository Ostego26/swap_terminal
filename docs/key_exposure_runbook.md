# Exposed key material: what is exposed, and the order to fix it

Written 2026-09-27. Every fact below was established by command, and the commands
are given so they can be re-run rather than believed.

## What is exposed

Five remote branches carry three key-bearing paths each. Verified by walking every
remote branch's tree:

    for b in $(git branch -r | grep -v HEAD | sed 's/ *origin\///'); do
      n=$(git ls-tree -r --name-only "origin/$b" | grep -c "wgrc.json\|swap_terminal/important\|swap_terminal/test.py")
      [ "$n" -gt 0 ] && echo "origin/$b : $n"
    done

    origin/claude/deposit-vout-migration : 3
    origin/claude/htlc-and-payout-guards : 3
    origin/claude/htlc-client-fixes      : 3
    origin/claude/regtest-harness        : 3
    origin/claude/server-auth-and-intents: 3

The three paths:

    swap_terminal/grc-sol-swap/abstergo_exchange/wgrc.json   a Solana ed25519 keypair
    swap_terminal/important                                   plaintext key material
    swap_terminal/test.py                                     plaintext key material

`claude/xrp-adapter`, the branch all of today's work is on, carries NONE of them --
confirmed with `git ls-tree -r --name-only HEAD`. Nothing done today added to this
and nothing done today can remove it either, because the material is in commits
those five branches point at.

The Solana keypair's public key is `BGdUSPGWiwStabibSXwiLwJCsk6iXDTeyL6fgWbNcAvN`,
which `rotate_solana_key.mjs` names as the `destinationSolanaAddress` on all three
intents in `swap_intents.json`, one of them marked paid for 5,560,821 lamports.

## Why deleting the branches is not the first step

A pushed private key is compromised from the moment it is pushed. GitHub keeps
unreachable objects fetchable by SHA for a period after a branch is deleted, forks
and clones keep their own copies, and anything that mirrored the repository has it
regardless. So branch deletion reduces exposure going forward and does nothing
about the key already being out. ROTATE FIRST. Deleting first only removes your own
ability to see what was exposed.

## The order

**1. Move the funds off the exposed keypair.** The tool for it is already in the
tree and was written for exactly this:

    cd swap_terminal/grc-sol-swap/abstergo_exchange
    node rotate_solana_key.mjs            # describes what it would do
    node rotate_solana_key.mjs --move     # performs it

It moves SPL tokens first and sweeps SOL last, which is the correct order -- a token
account needs rent-exempt SOL to exist, so sweeping SOL first can strand the
tokens. It refuses mainnet-beta without `--i-understand-this-is-mainnet`.

Note while reading its output: the comment about the rent-exempt minimum said
"about 890880 lamports" and was corrected to 650240 on 2026-09-27 (SIMD-0437 step
2). The diagnosis in that comment never depended on the number -- 25000 is neither
zero nor the minimum under either figure.

**2. Confirm the old keypair is empty** before deleting anything, because after
deletion you lose the record of what to check:

    solana balance BGdUSPGWiwStabibSXwiLwJCsk6iXDTeyL6fgWbNcAvN --url devnet
    solana balance BGdUSPGWiwStabibSXwiLwJCsk6iXDTeyL6fgWbNcAvN --url mainnet-beta

Both should read 0. The second is the one that matters.

**3. Then delete the five branches.**

    for b in claude/deposit-vout-migration claude/htlc-and-payout-guards \
             claude/htlc-client-fixes claude/regtest-harness \
             claude/server-auth-and-intents; do
      git push origin --delete "$b"
    done

Check first whether any of them holds work not present on `claude/xrp-adapter`:

    for b in <the five>; do echo "== $b"; git log --oneline origin/claude/xrp-adapter.."origin/$b" | head; done

**4. Ask GitHub to purge the unreachable objects.** Deletion alone leaves them
fetchable by SHA. GitHub Support will garbage-collect on request, and that request
is the only thing that removes them from the remote.

## What cannot be undone

The keypair is public. Step 1 is not "securing" it -- it is emptying it. It must
never be funded again, and `SOLANA_PAYER_KEYPAIR_PATH` must never point at it.

Two other secrets from this session's history, neither in git: the Gridcoin testnet
RPC password and the Gridcoin wallet passphrase both appear in a chat transcript
and in `~/.bash_history`. Testnet, so the exposure is bounded, but the passphrase
should not be reused on a mainnet wallet.
