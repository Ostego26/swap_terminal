# An ADAPTOR signature spends a 2-of-2 on Gridcoin, and all five transactions move a coin

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

## RUN 2, the same day: LOCK C, AND THE FIFTH TRANSACTION SPENDS

The run above left one thing untouched, and the operator named it: *"i want to test each branch
first."* Of the five transactions the protocol specifies, FOUR had moved a coin. `Tx_punish` had
been broadcast once per run, before T2, and asserted REFUSED -- the timelock HOLDING, which is
the opposite measurement from the branch working.

Lock C was added for it (a lock spends once, and lock B's refund takes the cancel output the
punish would have). Re-run with `LOCKS_PER_RUN = 3`:

    OK=63  FAIL=0  XFAIL=1  SKIP=2  --  exit code 0, ESTABLISHED

    split of the operator's 4.60 GRC   00573228e61a76474a8c07dfa85085b31c07f30cf810f08ae8ee0043a90e7805
    lock A                             f6f766e494c68d6a3c8a267ee4b056a8a7e448945e1d663dfffcfda8eaa88927
    Tx_redeem  ADAPTOR, under Y_a      b15f63b3704f405b8490d54e79cb23a7fb94c148b7d5b03e242f300b09d581a6
    lock B                             2c9e3b7c5e152ee711dba3a00722be581b3475bc65a48b5ee145e21ce6cdf3df
    Tx_cancel  plain, at T1            e1c9ff34cee6f0eaaed402d33bf0902b3100552cd4bb3e39fe672ebee8e0cd9c
    Tx_refund  ADAPTOR, under Y_b      5fcd1fe0e828d23c18ea9120652103b1b9fccb507d409d2e138eb85e72dc775f
    lock C                             44768ac10a994178562dd352da1ca25299c0210acdd7c70db92ccbbc375e0a1d
    Tx_cancel  plain, at T1 (lock C)   7466fd1ad7a746f5bd3cdafd0a019a9f730d6d8ec024d1a2d9d0fe1ce720183d
    Tx_punish  plain, AT T2            db8c2e9456456fb65a5ec82218c8de54b129a72bf36cd463006b7e91b2a85de5

    Y_a  0392418ecacefcb5..    Y_b  03e067fb0e30e76b..
    Monero lock address
      5AijqQuSXQaPr9pz3XfgcMF4dfrmJcUooJP2H7wyJ5n2DCoX98dYQMFUVBWrw88YgqL2538WgfmGi1tYp99SJsEtVPiB5jS

**NINE broadcasts, nine txids predicted before a byte was sent, nine matches** -- including both
adaptor spends, whose bytes depend on a randomized nonce.

The punish was measured the way the cancel is, and for the same reason: the SAME 308 bytes were
refused at height 3296126 and accepted at 3296131. Gridcoin's refusal names nothing (`-22 TX
rejected`), so a refusal alone proves only "refused for some reason" -- what isolates nLockTime
is that nothing about the transaction changed and only the height did. Its scriptSig was then
read back (218 bytes) and published NOTHING, which is what taking the punish branch must tell a
counterparty.

**THE FIVE TRANSACTIONS docs/monero_swap_protocol.md SPECIFIES HAVE NOW ALL MOVED A COIN ON
GRIDCOIN.** Everything under "WHAT THIS RUN DOES NOT ESTABLISH" below is unchanged by it: no
Monero moved, the consensus half of the timelock is still a reading, and the cryptography is
still unaudited.

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
