# An ADAPTOR signature spends a 2-of-2 on Gridcoin, and spending it publishes the Monero share

    Role: record (what one run established, and what it did not)
    Reads: nothing
    Writes: nothing
    Can move funds: no
    Mainnet-safe: yes -- every txid below is Gridcoin TESTNET, port 25715
    Live-safe: yes

`adaptor_regtest_verify.py --chain grc`, against the operator's Gridcoin testnet daemon
**v5.5.1.0** at `127.0.0.1:25715`, 2026-09-28. Final line:

    OK=48  FAIL=0  XFAIL=1  SKIP=2
    exit code 0 -- ESTABLISHED on every chain

This is the run `docs/monero_swap_protocol.md` section 2 called for when it named the gap as
"narrower and harder: THE JOIN", and it is the first time in this tree that an adaptor
signature has met a consensus rule.

## What makes this different from the 2026-09-28 run recorded one document over

`docs/gridcoin_2of2_spend_2026_09_28.md` records 40 OK / 0 FAIL on the same daemon, hours
earlier. **Every signature in that run was an ordinary ECDSA signature.** The chain cannot tell
an ordinary 2-of-2 spend from an adaptor one -- both are a DER signature under the same public
key, and the mempool verifies both identically -- so acceptance was never going to be the
measurement. The measurement is the RECOVERY, and there was none to make.

Here the redeem's second signature is Bob's PRE-signature under `Y_a`, completed with Alice's
Monero spend share. Bob's key never signed that digest.

## The transactions

    split of the operator's funding   9438768c5bf8adc14ff1e28eecb4e94e07078e475a3a5cf6134b81277ee16aba
    lock A (redeem branch)            7451773d99518f3700d997100cd930d726891c640a68aa9c62c771e4cc548a7a
    Tx_redeem  ADAPTOR, under Y_a     ccacc0614e34af7acd69a7941092b906d687fbc6bf6f7061492f868de16607ec
    lock B (cancel branch)            04eb71d817fbaa379f73b46d38702dddce76ac17aae1cc7f8ae7685368efb0f7
    Tx_cancel  plain, at T1           843b5fa64e1b91aa77dffe54aae9a0d34d0f3ee6220c0c696e3f3e20c3716e12
    Tx_refund  ADAPTOR, under Y_b     8fd143103ed91b36ff8d64aafb403cb3c5369800003128fe5367bb9dc6d21e0d

    Y_a  02e63b9726525d5b..    Y_b  030ae17c9cca8170..
    Monero lock address  57QiMXmWDn9S3Sju92wJqr9WkTbzk97uL3U3mrTZAn19d6HrvUUEvwQMPGzjML9obpiCsuHxVgfMTPE9BhBBkzkXJVNdMS8

Seven broadcasts, seven txids predicted before a byte was sent, seven matches -- including both
adaptor spends, whose bytes depend on a randomized nonce and so could not have been guessed.

## The six decisive outcomes

    1  funded and located by scriptPubKey match                        OK
    2  spends in key order                                             OK
    3  REFUSED transposed                                              OK
    4  REFUSED without the OP_0 dummy                                  OK
    5  the redeem PUBLISHES Alice's Monero spend share                 OK
    6  the recovered share reconstructs a key that opens the ADDRESS   OK

Five and six are new. The line behind them, verbatim:

    Tx_redeem's published scriptSig parsed to 2 signature(s)
    OK  GRC Tx_redeem PUBLISHES the Monero spend share (the join):
        got=recovered=True only_the_adaptor_leaked=True ed25519_public_matches_setup=True
    OK  GRC s_a(recovered) + s_b opens the Monero lock ADDRESS

Three separate facts, and each is load bearing:

  **recovered**                     a scalar came out of `recover_adaptor_secret` for one of
                                    the two signatures in the scriptSig, read back FROM THE
                                    DAEMON rather than from the bytes this process broadcast.
  **only_the_adaptor_leaked**       the OTHER signature yielded nothing. Without this, the
                                    recovery could have been finding the scalar in something
                                    other than the adaptor mechanism.
  **ed25519_public_matches_setup**  its ed25519 public key equals the public spend share
                                    CAPTURED AT SETUP -- the one the lock address was built
                                    from, not a re-derivation from the secret it was adapted
                                    with, which would pass unconditionally.

And then the cross-curve close: `s_a + s_b` on ed25519 gives a private spend key whose public
key is the one **decoded back out of the lock address**. Not compared against the inputs that
built the address, which would establish only that addition is associative.

## The other branches

    Tx_refund PUBLISHES the Monero spend share (the join)            OK   under Y_b, the mirror
    Tx_cancel (plain signatures on both sides) leaks NOTHING         OK   218 bytes read back
    10d-i the early punish is REFUSED                                OK
    10d-ii the refund SPENDS the second 2-of-2                       OK

The cancel's silence is asserted against the PUBLISHED bytes -- 218 of them, fetched from the
daemon -- rather than argued from this repository not calling `adapt` on that path. "We did not
call it" is a reason to believe, not a check (rule 17).

## WHAT THIS RUN DOES NOT ESTABLISH

**NO MONERO MOVED.** The lock address was derived and never funded. What is established is a
KEY MATCH: the reconstructed private spend key's public key equals the address's. That the
matching key actually spends is established separately and elsewhere --
`monero_shared_key_verify.py --sweep` on a Monero REGTEST chain, 738,723,841,921,372 atomic
units, txid `f584606948f430bf4835eb399c4e5373a027ee91cfb139ddf8d972bf1f15eade` -- and not in
this run. A full GRC<->XMR swap has still never been performed end to end.

**THE CONSENSUS HALF OF THE TIMELOCK IS STILL A READING.** `5b refused before T1 by CONSENSUS`
is SKIP, for the same reason as every previous run: Gridcoin has no `generateblock`, no
`getblocktemplate` proposal mode and no `submitblock`, so nothing here can ask a daemon to
validate a block without mining one. What IS measured is that the same 306 bytes were refused
at height 3296051 and accepted at 3296056 -- which isolates nLockTime, because nothing about
the transaction changed and only the height did. The stronger claim rests on
`src/validation.cpp:1777` and is labeled a reading everywhere it appears.

**THE CRYPTOGRAPHY IS STILL UNAUDITED.** `modules/adaptor_ecdsa.py` is a single-author
implementation from a written specification, checked against that specification's own vectors.
A chain accepting its output says the ENCODING is right and says nothing about whether the
scheme is secure. Its header's first sentence is unchanged by this run.

**THIS IS TESTNET.** Gridcoin testnet, port 25715, against coins the operator put at a
throwaway address on purpose.

## Two things the run incidentally settled

**The daemon has `-txindex`.** Step 7 located a CONFIRMED transaction (`confirmations=1`) via
"getrawtransaction without a block hash", which only answers for a confirmed transaction when
an index exists. That is worth recording because the read-back was moved into the mempool
window hours earlier specifically to survive a daemon WITHOUT one -- so this run did not
exercise the case the change was made for. The change is still right and still untested for
its own reason, which is the honest way to hold it.

**The operator-funding route carried a whole run.** One GUI payment of 3.50 GRC to a
seed-derived address, split in-process into two 1.50 outputs, everything after it signed in
this process. The staking-only wallet was never asked to create or sign anything, its unlock
deadline was never discarded, and it never stopped staking.
