"""BIP-350's published bech32/bech32m test vectors, verbatim.

Role: test data (the published spec tables, byte-exact)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes -- these are spec examples; three of them are MAINNET addresses and that is
      the point, see WHY MAINNET VECTORS BELONG HERE below
Live-safe: yes

WHY THIS FILE HOLDS LITERALS WHEN tests/valid_addresses.py HOLDS NONE.

Every other address in this suite is DERIVED, because a derived address cannot be mistyped and
says what it is for. `tests/test_address_literals_are_valid.py` is the clean gate that holds
that, and it is a good gate: it was written after a sweep replaced dozens of invented literals,
and its own comments record a mutation check proving it can still find one.

A published test vector is the one case where deriving destroys the value. The whole point of
BIP-350's tables is that they were produced by NEITHER this repository's decoder nor
tests/valid_addresses.py's encoder -- so if both have the same sign error, these catch it and
nothing else can. Rewriting them as derived strings would make them agree with our code by
construction, which is precisely the thing they exist not to do.

So this file is named in that gate's skip list, as a CATEGORY with a reason, in the same shape
as its existing `_is_an_alphabet()` and `_is_a_solana_pubkey()` exclusions -- not as a ratchet
or a baseline entry (rule 19 forbids both). And the exemption is not a hole: the gate asserts
that this module's valid vectors all decode and its invalid vectors are all refused, so an
invented address could not survive here either. Adding a literal to any OTHER file still fails.

WHY MAINNET VECTORS BELONG HERE. Three of the valid vectors are `bc1...` mainnet addresses.
They are spec examples, nobody holds their keys (the P2TR one's program is secp256k1's
GENERATOR x-coordinate, which is in every textbook), and no code path in this repository can
send to a string that only appears in a test data module. The mainnet gate over shared FIXTURES
is a different question and still applies to tests/valid_addresses.py.

WHAT THE ABSENCE OF THIS FILE COST. Until 2026-09-28 no test in this suite used a bech32m
address, and `bech32.bech32_decode()` -- the installed BIP-173 reference decoder, called at
five sites across three modules -- refused every Taproot address ever issued. A swap paying out
to bc1p... could not be created, an ALREADY CREDITED swap was set to status='failed'
terminally with the customer's coin in our wallet, and 1.5% of every HTLC redeem stayed with
the redeemer because the platform fee output was silently dropped. Six independent review
dimensions found that one root cause. These tables are what makes it impossible to reintroduce.
"""

from __future__ import annotations

# (address, expected scriptPubKey hex) -- BIP-350's valid-address table. The scriptPubKey is
# asserted, not just "it decoded": an address accepted with the WRONG program burns money as
# completely as a refused one strands it, and only one of those is visible from a boolean.
BIP350_VALID = (
    ("BC1QW508D6QEJXTDG4Y5R3ZARVARY0C5XW7KV8F3T4",
     "0014751e76e8199196d454941c45d1b3a323f1433bd6"),
    ("tb1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3q0sl5k7",
     "00201863143c14c5166804bd19203356da136c985678cd4d27a1b8c6329604903262"),
    ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqzk5jj0",
     "512079be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"),
    ("BC1SW50QGDZ25J", "6002751e"),
    ("bc1zw508d6qejxtdg4y5r3zarvaryvaxxpcs", "5210751e76e8199196d454941c45d1b3a323"),
    ("tb1pqqqqp399et2xygdj5xreqhjjvcmzhxw4aywxecjdzew6hylgvsesf3hn0c",
     "5120000000c4a5cad46221b2a187905e5266362b99d5e91c6ce24d165dab93e86433"),
)

# (address, why it is invalid) -- BIP-350's invalid-address table.
#
# TWO OF THESE ARE THE WHOLE REASON THE FIX IS NOT "ACCEPT EITHER CONSTANT". A v1 address
# carrying bech32's checksum and a v0 address carrying bech32m's are CORRUPTIONS, not
# alternative spellings, and a decoder that tried both constants in turn would accept them.
BIP350_INVALID = (
    ("BC130XLXVLHEMJA6C4DQV22UAPCTQUPFHLXM9H8Z3K2E72Q4K9HCZ7VQ7ZWS8R", "witness version 17"),
    ("bc1pw5dgrnzv", "1-byte witness program, under BIP-141's minimum of 2"),
    ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7v8n0nx0muaewav253zgeav",
     "41-byte witness program, over BIP-141's maximum of 40"),
    ("BC1QR508D6QEJXTDG4Y5R3ZARVARYV98GJ9P", "v0 with a 16-byte program (neither 20 nor 32)"),
    ("tb1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vq47Zagq", "mixed case"),
    ("bc1p0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7v07qwwzcrf",
     "witness v1 carrying a BECH32 checksum where BIP-350 requires bech32m"),
    ("tb1q0xlxvlhemja6c4dqv22uapctqupfhlxm9h8z3k2e72q4k9hcz7vqglt7rf",
     "witness v0 carrying a BECH32M checksum where BIP-173 requires bech32"),
    ("bc1gmk9yu", "empty data section, so there is no witness version to read"),
)

# The one invalid vector other test files need by name: a well-formed-looking Taproot address
# that must still be refused. Named so no test file writes it out again (rule 8).
CORRUPTED_TAPROOT = BIP350_INVALID[5][0]

# OP_0..OP_16 by witness version, for rebuilding the scriptPubKey the table predicts. Only the
# versions the vectors actually use, so an unexpected one raises a KeyError instead of being
# quietly encoded wrong.
WITNESS_VERSION_OPCODE = {0: 0x00, 1: 0x51, 2: 0x52, 5: 0x55, 16: 0x60}
