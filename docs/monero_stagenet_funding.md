# Getting stagenet coins into the Monero wallet, and what it unblocks

**Status 2026-09-27: BLOCKED ON FUNDING ONLY. The wallet works, the address is
confirmed by the wallet itself, the balance is 0.0 XMR, and the one live faucet
is rate limited until tomorrow.**

RUN ON THE OPERATOR'S HOST, 2026-09-27, against `monero-wallet-rpc --stagenet`
on port 38083. What it settled:

    get_balance       ANSWERED. total 0.0 XMR, unlocked 0.0 XMR
    validate_address  ANSWERED. nettype STAGENET, straight from the daemon, and
                      it confirms the address below is that wallet's own primary
                      address -- which until then rested only on the Keccak
                      checksum computed offline in chains/monero_keys.py
    get_transfers     ANSWERED with no `in` key, which is correct for a wallet
                      with no incoming transfers and confirms nothing
    exit status       3 = INCONCLUSIVE, so nothing can mistake it for a pass

THE BALANCE OF 0.0 IS ITSELF A RESULT: the faucet's "you have reached request
limit" did NOT mean it had already paid out. That was the cheap thing worth
checking before waiting a day, and the answer is no.

So the ten transfer field names are still unconfirmed and one transfer is still
the whole remaining blocker. Everything else on the Monero path is now either
confirmed or offline-verified.

This is the one step on the GRC<->XMR path that cannot be done from here at all.
Everything else in that work -- the ECDSA adaptor signatures, the ed25519 group
law, the DLEQ wire format -- is offline arithmetic and is done or in progress.
The Monero *wire format* is not arithmetic: it is ten field names in
`chains/monero_transfers.py` that were written from memory because
`getmonero.org` returned 403 in the environment they were written in, and the
only thing that can confirm them is a wallet with a transfer in it.

## Why this session cannot do it

Measured 2026-09-27, with `curl` through the container's egress proxy. Every
host returned `403` to the CONNECT, which is the gateway refusing the tunnel
rather than the host refusing the request:

    community.rino.io:443              403 CONNECT tunnel failed
    stagenet-faucet.xmr-tw.org:443     403 CONNECT tunnel failed
    melo.tools:443                     403 CONNECT tunnel failed
    stagenet.community.rino.io:38081   403 CONNECT tunnel failed
    node.monerodevs.org:38089          403 CONNECT tunnel failed
    stagenet.xmr-tw.org:38081          403 CONNECT tunnel failed

The proxy's own `recentRelayFailures` records each as
`connect_rejected: gateway answered 403 to CONNECT (policy denial or upstream
failure)`. The allowlist that IS reachable is the package and code
infrastructure -- github.com, pypi.org, files.pythonhosted.org,
registry.npmjs.org, index.crates.io, proxy.golang.org -- which is why the Go
DLEQ implementation could be cloned and built here and a faucet cannot be
opened. For the same reason `s.altnet.rippletest.net:51234` now closes
mid-exchange, so the XRP testnet work of the previous session is also no longer
reproducible in-container.

So this is a proposal in CLAUDE.md rule 16's sense and it is stated as one: the
steps below have NOT been executed by this session, and cannot be.

## The wallet

    537wxk1vzCDembafqWxfTgNcZGoK6rAsbP1JHKiQkjYLLzNDtgMTUKACBguFzx2XnFf1FQVqogcjd9LXTQ52jGiVBV52C1V

A stagenet primary address, created during the 2026-09-27 session. It is
recorded here because it was not recorded anywhere in the tree, and an address
that exists only in a chat log is a step nobody can repeat. It is a RECEIVE
address on a valueless test network -- not a credential, and not a thing that
needs the handling `.env` gets.

**Not verified from here.** Nothing in this container can reach a stagenet
daemon, so this session cannot confirm that the string above is well formed on
stagenet, that the wallet file still exists on the operator's host, or that the
two match. `monero_chain_check.py` answers all three in one run; see below.

## Getting coins

These are ordinary web forms, so a browser on the operator's machine is the whole
tool. **TRIED 2026-09-27, and only one of the three responded at all:**

    https://stagenet-faucet.xmr-tw.org/          the ONLY one that worked
    https://community.rino.io/faucet/stagenet/   did not work
    https://melo.tools/faucet/stagenet           did not work

The one that works answered, for the address above:

    已達領取限額，請明天再試。
    You have reached request limit, please come tomorrow.

    Current block height of faucet: 2217007

So the faucet is alive and synced -- the tip is real -- and it is RATE LIMITED, not
refusing the address. Two things follow, and the first is free:

1. **CHECK THE WALLET BEFORE WAITING.** "Reached request limit" is keyed on the
   claimer, so a payout may already have happened -- from an earlier attempt, or
   from a shared address of some kind. Run the check below before assuming the
   balance is zero.
2. Otherwise the answer is literally tomorrow. Nothing in this repo can shorten
   that, and the two alternate faucets did not respond today.

AND THERE IS NO EXPLORER ROUTE. Monero amounts and destinations are not public, so
no block explorer can report an address's balance. Only a wallet holding the view
key can, which means the check below is not merely the confirmation step -- it is
the ONLY way to learn whether coins arrived.

A PREVIOUS VERSION OF THIS PAGE NAMED A THIRD ROUTE THAT DOES NOT EXIST, and it is
recorded here rather than quietly deleted. It said to "sweep the old stagenet
wallet", whose keys were "exposed in git history (see docs/key_exposure_runbook.md)".
Checked 2026-09-27: `docs/key_exposure_runbook.md` is about **Solana** keys and says
nothing about a Monero wallet, and a grep of the whole tree for a Monero address,
mnemonic, spend key or view key finds exactly two hits -- the address above, in this
file and in its test. **There is no second stagenet wallet established anywhere in
this repository.** The sentence was written from a recollection of a conversation
and presented in the same voice as the measurements around it, which is precisely
what CLAUDE.md rule 17 forbids. If such a wallet exists it exists only on the
operator's machine, and only they can say so.

## Confirming it arrived, and what that proves

**A COMMAND WITH A <BRACKET> IN IT IS NOT A COMMAND.** An earlier version of this
page wrote `--wallet-file <the stagenet wallet>`, and pasting it produced
`bash: the: No such file or directory` on 2026-09-27 -- which is the failure mode
of every placeholder in a pasted block, and CLAUDE.md's "Context that shapes all
four" asks for a single pasteable block precisely to avoid it. So: find the wallet
first, then start it, with nothing to substitute by hand.

Find the wallet file (Monero's default directory, plus anywhere else it may be):

    ls -la ~/.bitmonero/stagenet/ 2>/dev/null
    find "$HOME" -maxdepth 4 -name "*.keys" -path "*stagenet*" 2>/dev/null

A Monero wallet is a PAIR: `NAME` and `NAME.keys`. Pass the one WITHOUT the
`.keys` suffix to `--wallet-file`. This starts the first stagenet wallet it finds
and says which one it picked, so there is no placeholder and no guessing:

    WALLET=$(find "$HOME" -maxdepth 4 -name "*.keys" -path "*stagenet*" 2>/dev/null | head -1)
    WALLET="${WALLET%.keys}"
    echo "using wallet: ${WALLET:-(none found -- name it yourself below)}"
    monero-wallet-rpc --stagenet --rpc-bind-port 38083 \
        --wallet-file "$WALLET" --prompt-for-password --disable-rpc-login

Then, in another terminal:

    python3 monero_chain_check.py --host 127.0.0.1 --port 38083

If a wallet-rpc is ALREADY running on 38083, skip straight to the second command
-- that is what happened on 2026-09-27, when the placeholder broke the first line
and the check ran anyway against a wallet-rpc already up.

`monero_chain_check.py` cannot spend: it builds its adapter with
`can_spend=False` unconditionally, imports no signing path, and has no flag that
broadcasts. It reads, and it reports which network the daemon says it is on in
capitals if the answer is MAINNET.

What the run establishes, and why it is the gate on everything Monero-side:

1. **The ten `FIELD_*` names in `chains/monero_transfers.py` are right or
   wrong.** They are currently a hypothesis. Fifty unit tests pass against
   responses seeded from that same hypothesis, so the green suite is evidence of
   self-consistency and of nothing else -- which is the trap the script exists
   to break.
2. **`incoming_transfers` returns something.** With a zero balance the script
   cannot tell an empty wallet from a wrong field name, which is precisely why
   the funding has to come first: `incoming_transfers_findings()` uses the
   balance as the discriminator between those two, and with no coins both read
   the same.

Until 1 is settled, the Monero 2-of-2 shared-spend-key path -- the fourth and
last piece of a GRC<->XMR swap -- cannot be written against anything but
documentation that this container also cannot open.

## The other way out

If the operator would rather this session do it than do it themselves: the
container's **Network access** setting is what denies the hosts above. It is in
the cloud environment menu in the session title bar, under Edit -- either a
broader access level, or the specific faucet and node hosts added to the allowed
domains. The levels are described at
`https://code.claude.com/docs/en/claude-code-on-the-web`. With any of those six
hosts reachable, everything on this page becomes something this session can run
and report rather than hand over.
