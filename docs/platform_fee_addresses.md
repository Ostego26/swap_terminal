# The platform fee: where it goes, and the two addresses that decide

The rate is **1.5%**, set by the operator on 2026-09-27, charged in the currency the
redeemer RECEIVES and leaving as its own output. One rate, two routes, and they now
agree: `PLATFORM_FEE_RATE` (atomic) and `DEFAULT_FEE_BPS=150` (brokered) are pinned
equal by `tests/test_htlc_spend.py::test_both_platform_fee_clients_read_one_rate_from_one_place`.

## Which assets need an address, and which do not

    asset  route     needs an address?   why
    LTC    atomic    YES                 the fee is a separate output; PLATFORM_FEE_LTC_ADDRESS
    GRC    atomic    YES                 the same; PLATFORM_FEE_GRC_ADDRESS
    BTC    atomic    no -- charges nothing. BTCClient.redeem_contract() passes no
                     extra outputs at all, so there is no fee to send anywhere. See
                     the note beside PLATFORM_FEE_RATE for what adding it needs.
    XMR    brokered  NO, AND THIS IS NOT AN OVERSIGHT -- see below
    XRP    brokered  NO, same reason

## THERE IS NO XMR FEE DEPOSIT ADDRESS, BECAUSE THERE IS NOTHING TO DEPOSIT

Asked for on 2026-09-27 and it should not be built, and the reason is a measurement
rather than an opinion. Grepped the whole tree for `PLATFORM_FEE_XMR` and for any
pairing of "platform" with "XMR": **zero hits.** Nothing reads such a variable
because nothing would send to it.

The brokered path does not TRANSFER its fee, it WITHHOLDS it.
`services/quote_service.create_quote()` computes

    output_amount_estimate = gross_output * (1 - fee_bps / 10000) - network_fee_reserve

and `services/payout_service.py` pays exactly that `output_amount_estimate`. The 1.5%
is the difference between what the swap was worth and what the customer is paid, so
it never leaves the hot wallet in the first place. There is no second transaction, no
destination, and nothing to address.

So a `PLATFORM_FEE_XMR_ADDRESS` would be a mechanism with no reader -- which CLAUDE.md
rule 5 refuses on the main path and rule 19 refuses generally. The fee is already
yours; it accumulates as the hot wallet's growing balance.

**If the goal is ACCOUNTING rather than collection** -- seeing fee income separately
from float -- that is a real and different want, and the honest answer is that it
needs a sweep, not an address: a periodic transfer of accumulated fee value out of
the hot wallet to somewhere you count separately. Monero's wallet will make the
destination for you (`create_address`, which `monero_chain_check.py --derive-address`
already calls), but the deciding piece is a ledger of what was earned per swap, and
the `quotes` table already stores `fee_bps` and `output_amount_estimate` per row,
which is the input to that sum. Say the word and that is a query plus a sweep, not an
environment variable.

## The GRC address supplied on 2026-09-27, and its one problem

    moimRB7znV9FgZGKUmLHukYusVmzKiY5r6

**DECODED AND VERIFIED HERE, and it is a TESTNET address.** Measured rather than
inferred from its first letter:

    base58 -> 25 bytes
    version byte 0x6F (111) = TESTNET P2PKH
    checksum on wire 0e382197, recomputed sha256d 0e382197  -- VALID

A MAINNET Gridcoin address carries version byte 0x3E and starts with **S**. 0x6F is
the testnet byte, which is why this one starts with `m`. It is a perfectly good
address -- the checksum proves it is not a typo -- on the wrong network for mainnet
use.

That is exactly the shape of the bug fixed the same day: the value SHIPPED as the
default was `mnTh582mZM12fQry6rtZV7XehNVtZRVdDw`, also version byte 0x6F, and on
mainnet the fee paid to it was unspendable by anyone. Burned, every redeem, silently.
The default is gone now and an unset variable means charge no fee, so nothing burns
by accident any more -- but **setting this address on a mainnet deployment would
reintroduce the burn deliberately.**

So:

    on Gridcoin TESTNET   export PLATFORM_FEE_GRC_ADDRESS=moimRB7znV9FgZGKUmLHukYusVmzKiY5r6
                          correct, and it collects.
    on Gridcoin MAINNET   DO NOT. Supply an address starting with S, from the wallet
                          that will hold the fees. Until then, leave the variable
                          unset: the rate stays 1.5%, the amount collected is zero,
                          and every redeem logs a WARNING naming the variable.

The LTC address is still outstanding and has the same shape of question:
`PLATFORM_FEE_LTC_ADDRESS` wants a `tltc1...`/`m...`/`Q...` testnet address for
testnet and an `ltc1...`/`L...`/`M...` mainnet address for mainnet. The old shipped
default `tltc1qzxllez2nfy70rypyh3re0v4z8v0jp57egw6w4p` is a testnet bech32, with the
same burn consequence on mainnet.

## Checking an address before you export it

This is the check this page ran, and it takes no network:

    cd ~/Documents/Python/swap_terminal
    python3 - <<'PY'
    import hashlib, sys
    ALPHA = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    KIND = {0x3E: "GRC MAINNET P2PKH (starts S)", 0x6F: "TESTNET P2PKH (starts m or n)",
            0x00: "BITCOIN mainnet P2PKH", 0x05: "BITCOIN mainnet P2SH",
            0x30: "LTC mainnet P2PKH (starts L)", 0xC4: "testnet P2SH"}
    for addr in sys.argv[1:] or ["moimRB7znV9FgZGKUmLHukYusVmzKiY5r6"]:
        n = 0
        for c in addr:
            n = n * 58 + ALPHA.index(c)
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        raw = b"\x00" * (len(addr) - len(addr.lstrip("1"))) + raw
        body, checksum = raw[:-4], raw[-4:]
        good = hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4] == checksum
        print(f"{addr}\n  version 0x{body[0]:02X} -> {KIND.get(body[0], 'UNKNOWN')}  checksum_valid={good}")
    PY

Pass addresses as arguments to check your own. A bech32 address (`tltc1...`,
`ltc1...`) is not base58 and this will not decode it; for those the network is in the
human-readable prefix, which is the part before the `1`.
