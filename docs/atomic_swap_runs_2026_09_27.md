# Five atomic swaps on real chains, 2026-09-27 — what ran, what broke, and the chain facts it cost to learn

Role: record (read-only; nothing reads this file to make a decision)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

This is a log of the first evening any swap in this repository moved coins on two chains and
finished. It exists because five of the things below were found by RUNNING and could not have
been found by reading, and because the next person should not have to re-derive them.

Every txid, address and byte offset here came off the operator's own terminal. Nothing in this
file is reconstructed or estimated. Where a number is an estimate it says so.

Amounts are testnet and regtest throughout. Two funded legs were lost permanently; that is
recorded in full below rather than summarized away, because the cause is a design defect that
is still open.

---

## 1. THE FIVE THAT COMPLETED

All five read the preimage OFF A CHAIN rather than receiving it in a message. That is the
property that makes them atomic; a pair of confirmed transactions does not establish it.

### BTC -> LTC, regtest x regtest, 22:16 — `atomic_swap.py`, OK=11 FAIL=0

    commitment       8efa06e06b456cec551d515b892e04049a6b30ef35b409a2d14d8af229d6e867
    BTC funding      2d227a433335a903...   contract 2N8wTHzyBrvDf4cH5eJth37sho7RFNxbV8q
                                           scriptPubKey a914ac25a1cecd6ac9e906f903f822d41b62c0beeb7787
    LTC funding      d2cb3969a3bc67f1...   contract QioxQoKJZXmsaiDcJ3a74RDoGp2wvq4cG1
                                           scriptPubKey a914f396575516a14d53230348ce60d1a8a91d43ce0287
    LTC claim        6db96bd18daa175b...   <- publishes the preimage
    BTC claim        1777116e6d90a8e1...   <- spent with the bytes read back off LTC
    vouts            LTC 0, BTC 1
    locks            BTC 288 blocks (48h at 600s), LTC 576 blocks (24h at 150s), margin +24h

### LTC -> GRC, regtest x TESTNET, 22:42 — `atomic_swap.py`, OK=11 FAIL=0

    commitment       119ec1e56ab386ff7cf32cde1f164f946c289f5fc11e1217f9de50c5688e29e8
    LTC funding      3e9756d0ade87c7a...   contract QjLEgytcB7YKMydRXBgPZBuNSKuLiYtfSx
    GRC funding      ec793912564f9f09...   contract 2MvNcPJ2F21SB3cZTuWZ9An3SYErC4KW5kh
    GRC claim        5ceb20d09d889f9c...   <- publishes the preimage on Gridcoin
    LTC claim        cdf69133475c7581...
    vouts            GRC 1, LTC 0
    locks            LTC 1152 blocks (48h at 150s), GRC 960 blocks (24h at 90s), margin +24h
    fee that WOULD have been charged: 4.66080000 GRC on 310.72 = exactly 1.5%

### BTC -> GRC, regtest x TESTNET, 22:45 — `atomic_swap.py`, OK=11 FAIL=0

    commitment       9a62aac7a7f60312e557157cacb6b8ad25b6c0b47c75230725590d4c2f64432e
    BTC funding      52efcb4c773a1ffc...   contract 2N3eTnz2H5f9xBQBz3F92sSyGY7Ju8AH7BK
    GRC funding      4f604943c006a834...   contract 2NAttpMeSniSJTHLVhZG26Wg9CWYt6BSqhE
    GRC claim        b66f2226a649271c...
    BTC claim        43b02ac11275fac0...
    vouts            GRC 1, BTC 1
    size             0.0005 BTC for 1843.46 GRC, sized from the live rate
    fee that WOULD have been charged: 27.65190000 GRC on 1843.46 = exactly 1.5%

### XRP -> GRC, TESTNET x TESTNET, 23:01 — `atomic_swap_xrp.py`, OK=16 FAIL=0

    commitment       afed0eff92b7649cef21a07b385829ff424a642d1b349542752a9d6b663b4fae
    XRPL condition   A0258020AFED0EFF...810120
    XRP escrow       C5563F1C9FECEFB8...   OfferSequence 21051284, validated after 4 polls
    GRC HTLC         7cf4b61200b913e43a3007926c8cea09bd8609bc6fba45c2de734cb6290eb504
                     p2sh 2N8ycSa5qUNLK3e8ZRW8jigRHSSaoq3f8Y8, vout 1
    GRC claim        d3134b2cfa8a208bfd8dc693268621dd4f7d53ac1482e6fcd8ebcbc1725e8842
    XRP finish       A4F8123482E24386...
    size             1000000 drops for 66.10183885 GRC
    B's balance      115999980 -> 116999980 drops (+1000000), asserted not assumed

### GRC -> XRP, TESTNET x TESTNET, 23:02 — `atomic_swap_xrp.py`, OK=16 FAIL=0

    commitment       c94cc1fd97c7c11276037182816c66fcd4b95e0663e52877953afb305a3b3726
    GRC HTLC         9283c4c5d0df8c5b7e6da1299f2cc08ce457de6b7803cb6f8a2028157227cbbc
                     p2sh 2Mv9sm8UGo6wRiXYzwbv7xzBbAQea2Tn2xN, vout 1
    XRP escrow       98E03E67336167A6...   OfferSequence 21051164
    XRP claim        B5E22EA1F4AACBDA...   <- the FULFILLMENT carries the preimage
    GRC claim        b6f7f57dc789bfb5fe9b3b903e89620d6282bd70430b8e14a7da09a6d88f9ba1
    A's balance      81997340 -> 82996980 drops (+999640 = 1000000 escrowed - 360 finish fee)

**THE TWO XRP DIRECTIONS REVEAL THE SECRET BY DIFFERENT MECHANISMS, and that is the finding
worth keeping from this pair.** `xrp-first` publishes the preimage in a scriptSig on Gridcoin;
`grc-first` publishes it in the `Fulfillment` field of an `EscrowFinish` on the XRP Ledger. The
XRP Ledger has no scripting language at all. So atomicity does not require a script — it
requires only that TAKING your leg forces you to publish something the counterparty can read.
This pair is the existence proof that the property is separable from scripting -- which is why
the finding outlasted the scriptless-chain work that was later removed.

### ONE VOUT WAS NEVER 0 BY DEFAULT

Across the five runs the contract sat at vout 1 four times and vout 0 twice. A driver that
guessed 0 would have spent a change output and burned a fee on four of them. This repository has
fixed that same defect three times (BTC 2026-09-25, GRC 2026-09-26), and
`modules/htlc_chain_read.htlc_vout()` matching the scriptPubKey on chain is what made every one
of these runs land on the right index.

---

## 2. THE TWO LEGS THAT WERE LOST, AND WHY IT IS A DESIGN DEFECT

    310.72 GRC   contract 2MvKptaGt7AYcnRBbkz3pDsJm7odJdVz6Y1, funding f0bb0c9fada32a9c...
                 (GRC testnet, from the LTC->GRC run that died on the vContracts byte)
    0.1 LTC      contract QfQ1Q9SmNqZWURgq3bbDUim1ygdbWAhn7c, funding 1182423e0ae1d180...
                 (LTC regtest, the other leg of the same run)

Earlier aborted runs stranded further legs on BTC and LTC regtest, including BTC funding
fc5b23dbc9868f26... and LTC funding be96f6c93f3bc548... from the run the timelock check refused
AFTER funding both legs. Regtest coins, so only the GRC figure is worth stating as a loss.

**These are not recoverable by waiting out the timelock.** `atomic_swap.py` mints four keypairs
IN PROCESS and never writes them anywhere — deliberately, because this repository forbids
persisting a key. BOTH branches of each contract name those keys: the hashlock branch and the
refund branch. When the process exits, neither can ever be signed again. A timelock does not
help when nobody holds the refund key.

So every crash between funding and claiming destroys that leg permanently, and it happened twice
in one evening on the first two real runs.

**THE FIX IS NOT WRITTEN YET AND IS DELIBERATELY NOT MINE TO MAKE.** Point the REFUND branch at
a WALLET address the daemon can always spend, instead of at a minted key. Then a crash costs a
wait rather than the coins, and the hashlock branch can keep its in-process key because it is
the branch that is supposed to be used. That changes what the fund path builds, which is live
posture and the operator's call (rule 16). It should be made before this driver touches a chain
where the coins matter.

---

## 3. WHAT RUNNING FOUND THAT READING HAD NOT

Six defects, in the order they surfaced. Every one was in code that had been read, reviewed and
tested before tonight.

### 3.1 The timelock check compared BLOCKS across two chains — `ac51560`

The security property of the whole protocol, and it refused a correct swap:

    initiator   BTC  288 blocks x 600s = 172800s = 48h
    participant LTC  576 blocks x 150s =  86400s = 24h
    in blocks:   288 - 576 = -288   -> REFUSED
    in seconds: 172800 - 86400 = +86400 = 24h of margin -> safe

Litecoin needs FOUR TIMES the blocks for HALF the time. `modules/htlc_timelock` states the
policy in HOURS and converts through each chain's own `SECONDS_PER_BLOCK`, so the two legs were
never meant to have comparable block counts.

The old docstring NAMED the bug and then committed it: it said "Litecoin at 2.5 minutes a block
and Bitcoin at 10 means the same block count is four times the wall-clock" — and subtracted one
block count from the other anyway. Blocks remaining is the comparable unit WITHIN one chain;
across two it is a category error.

It also refused AFTER both legs were funded, while printing "Nothing was funded". Planning is now
separate from funding, and `PlannedLeg` carries no txid so the claim is structural.

### 3.2 The driver guessed what `create_contract` returns — `d2a1b4e`

All three clients return `{"txid", "vout", "redeemScript": BYTES, "p2shAddress"}` — camelCase,
script as bytes. The driver looked for `redeem_script` and `p2sh_address`, found neither, and
fell through to `or ""`. The address printed as a blank line; the script became
`str(b"\x63\xa8...")`, a bytes repr, which died on `fromhex` at position 1 — the quote character.

`str()` on bytes does not raise. It produces a plausible string that fails five lines later
talking about hexadecimal.

### 3.3 `secret` went in as hex, and all three clients declare `secret: bytes` — `4cf38ae`

`push_data(secret)` reached `bytes([length]) + data` and raised `can't concat str to bytes`,
with both legs already funded. Same class as 3.2: an interface guessed rather than read.

**The stubs were wrong in the same way the driver was, which is why the tests passed.** A stub
that does not enforce its own signature cannot catch a caller that violates it — it models the
shape of the call and not the contract, so it agrees with whatever the caller does. The stubs now
raise `TypeError` on a non-bytes `secret` or `redeem_script`, and answer in the real camelCase
shape.

### 3.4 The dry run never opened a socket — `87f0655`

`--run` omitted printed three lines derived from its own arguments and contacted nothing. A check
that cannot fail is rule 13's defect, and it had already misled once: the identical clean plan
preceded a connection-refused traceback on a chain the driver had never reached.

It now performs the whole READ-ONLY half of the real run — names both networks, reads both tips,
derives and judges both locktimes — and reports what it PROVED and what it did NOT (a balance, a
wallet unlock, a `create_contract` the daemon might refuse).

### 3.5 GRIDCOIN SERIALIZES `vContracts` AFTER `nLockTime` — `7316c8c`

This is the one that cost 310.72 GRC. `createrawtransaction` on Gridcoin testnet returns 90
bytes for one input and one P2PKH output, and the parser refused it because one byte was
unaccounted for:

     0..3   version      02000000
     4..7   nTime        279ab96a   <- decodes to 22:35:19Z, the minute it was asked for
     8..8   vin count    01
     9..40  txid         (reversed)
    41..44  vout         00000000
    45..45  scriptSig    00 (empty)
    46..49  sequence     ffffffff
    50..50  vout count   01
    51..58  value        a086010000000000
    59..59  script len   19
    60..84  scriptPubKey 76a914...88ac
    85..88  nLockTime    00000000
    89      vContracts   00        <- THE EXTRA BYTE

Confirmed against `Gridcoin-Research/src/primitives/transaction.h` rather than inferred from the
byte: `CTransaction::SerializationOp` reads nVersion, nTime, vin, vout, nLockTime, and then
`vContracts` (a `std::vector<GRC::Contract>`) when nVersion >= 2, else the legacy `hashBoinc`
string. An empty vector serializes as a varint 0.

**Only an EMPTY vContracts is accepted.** A Gridcoin contract is a protocol message — a beacon,
a poll, a vote — and the suffix is carried verbatim into a transaction this module then SIGNS.
The check widened by exactly one byte; 2, 3, 5 and 8 extra bytes are all still refused.

### 3.6 Gridcoin has no `gettxout` — `c929aa5`

Every GRC spend printed

    GRC RPC call failed: GRC RPC Error: {'code': -32601, 'message': 'Method not found'}

with a stack trace, and then succeeded. That is `lookup_contract_output`'s route 1 missing and
route 4 answering — the four-route design working as written. The DEFECT IS IN THE OUTPUT: the
thing that worked looks broken, which is rule 14 pointed backwards.

---

## 4. GRIDCOIN FACTS, MEASURED BY ASKING THE DAEMON

Established by `help <method>` one method at a time, after grepping `help` twice returned nothing
while `getrawtransaction` demonstrably worked — the grep assumption was simply wrong.

    gettxout                     help: unknown command: gettxout
    getrawtransaction            getrawtransaction <txid> [verbose=bool]
    gettransaction               gettransaction "txid" ( includeWatchonly )
    signrawtransaction           signrawtransaction <hex string> [...] [<privatekey1>,...]
    signrawtransactionwithkey    help: unknown command: signrawtransactionwithkey

So Gridcoin is on the pre-0.17 Bitcoin RPC surface and `signrawtransaction` is its only signer.
Worth knowing before anybody "modernizes" a call in the spend path on the strength of what
Bitcoin Core 28.1 accepts.

Three more, each of which shapes how a GRC leg has to be driven:

- **`createhtlc` reads each party's PUBKEY out of the wallet.** A swap with a real counterparty
  needs their pubkey IMPORTED, not just their address. BTC and LTC legs need no such exchange, so
  a counterparty flow has a GRC-specific step.
- **`createhtlc` AND `claimhtlc` both call `EnsureWalletIsUnlocked()`**, and `createhtlc` also
  sends. A staking-only unlock is not enough (rpc -13, `Wallet is unlocked for staking only.`).
- **`walletpassphrase <passphrase> <timeout> [stakingonly]`** — `stakingonly` true DISABLES
  SENDING. It is a restriction on sending, not a prerequisite for staking, so a full unlock
  permits both. Measured: the XRP/GRC runs renewed the unlock from 2027-09-27T01:07:00Z to
  2027-09-27T23:02:09Z, 21.9 hours LATER, rather than shortening it.
- **GRC's miner fee is 0.01 GRC on a ~327-byte spend = 0.0306 coin/kvB**, against Litecoin's
  0.000315. About a hundred times the rate. Immaterial at today's price and worth knowing if that
  changes.

---

## 5. WHAT IS STILL NOT PROVEN

Stated because a list of five successes reads as more than it is.

- **Both sides were played by ONE process in every run.** The cryptography and the chain behavior
  are proven; counterparty misbehavior is not exercised at all. The one step a real participant
  does differently is reading the claim — they are not the claimer, so `gettransaction` will not
  find it and they need `-txindex` or a block scan.
- **Three directed pairs of six remain untested** in `atomic_swap.py`: `LTC -> BTC`,
  `GRC -> LTC`, `GRC -> BTC`. The last two put the 48-hour lock on GRC testnet, where a stranded
  leg waits out a real two days instead of a mined instant.
- **No platform fee was collected on any run.** `PLATFORM_FEE_{BTC,LTC,GRC}_ADDRESS` are unset,
  which by design charges nothing and never blocks a redeem. The 1.5% arithmetic was exercised
  and correct on all three GRC-side runs; only the collection is missing.

---

## 6. THE METHOD NOTE, because it is the transferable part

Five of the six defects in section 3 were in code that had been read and reviewed. Each was found
by running it against a real daemon, and each looked correct in its own file.

Three of them — 3.2, 3.3 and the stub problem — were the same mistake: AN INTERFACE ASSUMED
RATHER THAN READ, with a test double that shared the assumption. A stub written from the same
belief as the caller cannot falsify that belief. The fix that generalizes is to make the double
enforce the real signature, so a wrong call fails at the boundary instead of agreeing all the way
down.

And in four separate places tonight a claim about a test result or a chain fact was written
before it was checked: a "0 failures" over an output showing two, mutation kill counts read as
success over a broken baseline, a `help | grep` assumption stated twice, and a staking-unlock
claim contradicted by the daemon's own help text. Every correct value was already on screen. The
discipline that held, when followed, was reading counts off the junit XML's `testsuite`
attributes and quoting the daemon rather than recalling it.
