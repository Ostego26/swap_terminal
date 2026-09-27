# Getting stagenet coins into the Monero wallet, and what it unblocks

**Status 2026-09-27: BLOCKED, and the blocker is the network policy of the
container this session runs in, not the wallet and not the code.**

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

Start the wallet against a stagenet daemon, then run the check:

    monero-wallet-rpc --stagenet --rpc-bind-port 38083 \
        --wallet-file <the stagenet wallet> --prompt-for-password --disable-rpc-login

    python3 monero_chain_check.py --host 127.0.0.1 --port 38083

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
