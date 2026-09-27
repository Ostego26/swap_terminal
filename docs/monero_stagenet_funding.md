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
tool. **TRIED 2026-09-27, and only one of the first three responded at all:**

    https://stagenet-faucet.xmr-tw.org/          WORKED, 1 claim/day, rate limited
    https://community.rino.io/faucet/stagenet/   did not work
    https://melo.tools/faucet/stagenet           did not work

FOUR MORE FOUND BY SEARCH on 2026-09-27, and the first is better than everything
above because its limit is HOURLY rather than daily:

    https://cypherfaucet.com/xmr-stagenet        0.01 sXMR once per HOUR, captcha.
                                                 Its page reported a 1000.00 sXMR
                                                 balance and 3,919 payouts made.
                                                 Also runs BTC and LTC testnet
                                                 faucets at cypherfaucet.com.
    https://get.xmr.id/form.html                 XMR.ID's stagenet request form
    https://monerica.com/site/stagenet-faucet    a directory entry, not a faucet --
                                                 use it to find live ones when the
                                                 list above has rotted
    https://github.com/moneroexamples/monero-stagenet-services
                                                 the community list of stagenet
                                                 explorers, nodes and wallets; the
                                                 place to look first in a year

**NOT TRIED BY THIS SESSION.** They came from a web search, and this container's
network policy denies every one of these hosts at the gateway, so nobody here has
loaded any of them. That is the difference between a found URL and a working
faucet, and rule 17 asks for it to be said rather than blurred.

Also named by that search, and worth knowing for a different reason:
`stagenet.xmrchain.net` is a stagenet block explorer. It does NOT answer the
question this page is about -- Monero amounts and destinations are not public, so
no explorer reports an address's balance -- but given a txid it can confirm a
transaction exists and is in a block. The wallet remains the only thing that can
say what arrived.

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

## THE ROUTE THAT NEEDS NO FAUCET AND NO NETWORK: MONERO REGTEST

**This is the recommended path, and it removes the faucet from the critical path
entirely.** Added 2026-09-27 after asking whether test coins could be swapped for
stagenet XMR in reverse -- they cannot, and the reason is worth stating because it
generalizes: a swap MOVES coins, it does not create them, so the reverse direction
needs a counterparty who already holds stagenet XMR. There is none. And using this
repo's own swap machinery to obtain the coins needed to test that machinery is
circular. The faucet is the only issuer on stagenet.

But stagenet is not the only network that can answer the question.

**THE TEN FIELD NAMES ARE A PROPERTY OF THE SOFTWARE, NOT OF THE NETWORK.**
`txid`, `amount`, `address`, `amounts`, `subaddr_index`, `confirmations`, `type`,
`unlock_time`, `locked` and `double_spend_seen` are the keys
`monero-wallet-rpc` puts in a `get_transfers` reply. The same binary emits the same
keys on regtest as on stagenet. So a regtest transfer confirms them exactly as well
as a stagenet transfer would, and it can be had in seconds with no faucet, no peer
and no internet.

CONFIRMED FROM MONERO'S OWN SOURCE on 2026-09-27, read rather than recalled
(`raw.githubusercontent.com/monero-project/monero/master`):

    --regtest            src/cryptonote_core/cryptonote_core.cpp:84
                         "Run in a regression testing mode."
    --fixed-difficulty   same file, line 94, "Fixed difficulty used for testing."
    --offline            same file, line 114
    generateblocks       src/rpc/core_rpc_server_commands_defs.h:1113, taking
                         amount_of_blocks, wallet_address, prev_block,
                         starting_nonce and returning height and blocks
    and it is GATED      src/rpc/core_rpc_server.cpp:1956 --
                         `if (m_core.get_nettype() != FAKECHAIN)` returns
                         CORE_RPC_ERROR_CODE_REGTEST_REQUIRED, so this RPC
                         cannot be reached on stagenet or mainnet even by
                         accident. Regtest is its own nettype (FAKECHAIN),
                         not a mode over another one.

That last line is the safety property worth noticing: there is no way to call
`generateblocks` against a real network, so nothing in this procedure can touch
stagenet or mainnet state.

### The procedure: one command

`monero_regtest.py` at the repository root does all of it. It needs `monerod` and
`monero-wallet-rpc` on PATH and nothing else -- no faucet, no peers, no internet:

    python3 monero_regtest.py                 # prints the plan, starts nothing
    python3 monero_regtest.py --run           # daemon, wallet, 80 blocks, 1 transfer
    python3 monero_chain_check.py --host 127.0.0.1 --port 28083
    python3 monero_regtest.py --stop          # terminates both, asserts they are gone

Ports are 28081/28080/28083 rather than Monero's real ones, deliberately: a regtest
daemon on 18081 or 38081 is one typo away from a wallet that meant mainnet or
stagenet. The data directory defaults to `~/xmr-regtest`, and the script REFUSES a
`--data-dir` inside `~/.bitmonero` (where the real chains live) or inside the
checkout.

The steps it runs, and why each one is there rather than a shorter version:

### The procedure, by hand

Two terminals. Nothing to substitute by hand except a wallet password of your
choosing. `--offline` is deliberate: it stops the daemon looking for peers, which
on a private chain it should never find.

Terminal 1 -- the daemon, on a data directory of its own so it cannot disturb the
stagenet chain:

    mkdir -p "$HOME/xmr-regtest"
    monerod --regtest --offline --fixed-difficulty 1 \
        --data-dir "$HOME/xmr-regtest" \
        --rpc-bind-port 18081 --p2p-bind-port 18080 \
        --log-level 0 --detach

Terminal 2 -- a fresh wallet on that chain, then blocks paid to it. `--trusted-daemon`
because it is our own, and the wallet's port is 38083 so
`monero_chain_check.py --host 127.0.0.1 --port 38083` needs no change:

    monero-wallet-cli --regtest --trusted-daemon \
        --daemon-address 127.0.0.1:18081 \
        --generate-new-wallet "$HOME/xmr-regtest/regwallet"
    # note the address it prints, then exit the interactive wallet with: exit

    ADDR=$(monero-wallet-cli --regtest --trusted-daemon \
        --daemon-address 127.0.0.1:18081 \
        --wallet-file "$HOME/xmr-regtest/regwallet" --password "" \
        --command address 2>/dev/null | grep -oE '[0-9A-Za-z]{95,}' | head -1)
    echo "regtest wallet address: ${ADDR:-(read it off the generate step above)}"

    curl -sS http://127.0.0.1:18081/json_rpc -H 'Content-Type: application/json' \
      -d "{\"jsonrpc\":\"2.0\",\"id\":\"0\",\"method\":\"generateblocks\",\"params\":{\"amount_of_blocks\":80,\"wallet_address\":\"$ADDR\"}}"

Eighty blocks because a coinbase output needs 60 confirmations before it unlocks;
80 leaves margin and takes seconds at difficulty 1. Then start the wallet RPC and
send a small amount to a SECOND address in the same wallet -- an ordinary transfer
is what puts an `in` entry in `get_transfers`, which a coinbase alone does not:

    monero-wallet-rpc --regtest --trusted-daemon \
        --daemon-address 127.0.0.1:18081 \
        --wallet-file "$HOME/xmr-regtest/regwallet" --password "" \
        --rpc-bind-port 38083 --disable-rpc-login &

    python3 monero_chain_check.py --host 127.0.0.1 --port 38083

**WHAT THIS DOES AND DOES NOT SETTLE.** It settles the ten field names, the
`amounts`-versus-`amount` arithmetic that `_reject_amount_disagreement()` checks at
runtime, the contradictory `locked` semantics named in `chains/monero.py`'s header,
and the real deposit scan in step 4 of the check. Those are the whole of what is
currently unconfirmed about the Monero wire format, and they are the reason that
script exists.

It does NOT settle anything about stagenet specifically, and it does not settle
whether a shared 2-of-2 address from `chains/monero_keys.py` is spendable -- that
needs a transfer to such an address and a sweep out of it, which regtest can also
do, and which is the next thing to try once the field names are confirmed. It also
does not remove the reason to fund the stagenet wallet eventually: a swap
rehearsal against a network with real peers and real timing is a different test.

It does remove the faucet from the critical path today, which is the point.

## The other way out

If the operator would rather this session do it than do it themselves: the
container's **Network access** setting is what denies the hosts above. It is in
the cloud environment menu in the session title bar, under Edit -- either a
broader access level, or the specific faucet and node hosts added to the allowed
domains. The levels are described at
`https://code.claude.com/docs/en/claude-code-on-the-web`. With any of those six
hosts reachable, everything on this page becomes something this session can run
and report rather than hand over.
