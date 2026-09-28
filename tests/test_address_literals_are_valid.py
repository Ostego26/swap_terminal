"""Every address-shaped literal in this repository decodes. A CLEAN GATE, not a baseline.

Role: test (reads the tree's own source; no chain, no network, no database)
Reads: every tracked .py file
Writes: nothing
Can move funds: no
Mainnet-safe: yes
Live-safe: yes

Operator, 2026-09-27: "make sure any an all addresses used for any transaction are valid
addresses that pass all tests."

WHY A GATE AND NOT A ONE-TIME FIX. 25 address-shaped literals in this tree decoded as nothing
when that was first measured, and they had accumulated one at a time, each written by somebody
who needed a plausible string for one test. Nothing could have stopped the 26th. This is the
check that does, and it is a CLEAN GATE with no baseline file -- rule 19: "never add a baseline
line for code you are writing now", and a ratchet here would let the next one in.

WHAT A VIOLATION MEANS, so the failure message is actionable rather than a puzzle. An
address-shaped literal that does not decode is one of three things, and all three are defects:

  a placeholder in a usage example, which a reader will copy into a real payout
  a test fixture, which makes the test prove less than it appears to
  a typo in a real address, which is money sent nowhere

THE PRECISION THIS GATE NEEDS, and how it gets it. A regex over source cannot know what is an
address, so it would flag base58 ALPHABET strings, hex digests and Python identifiers. Rather
than allowlisting those one by one -- which is a baseline wearing different clothes -- the
matcher is narrowed by rules that are true of addresses and false of the others:

  A CHAIN-SPECIFIC PREFIX. bc1/tb1/bcrt1/ltc1/tltc1 (and grc1/tgrc1, which cannot exist)
  mean "this claims to be bech32", so it must decode as bech32. Nothing else in this tree
  begins that way.
  NOT A PYTHON IDENTIFIER. `maxSupportedTransactionVersion` is 30 base58-legal characters and
  a valid identifier; a real address essentially never is, because it would have to draw no
  digit in 34 characters.
  NOT A BASE58 ALPHABET. The two 58-character alphabets appear as data in this tree, and a
  string that IS one of them (or a prefix of one) is a charset, not an address.

Solana is deliberately out of scope: its addresses are plain base58 of a 32-byte key with NO
checksum, so `So11111111111111111111111111111111111111112` is correct and undecodable by every
rule above. chains/solana_address.py owns that format, and claiming to check it here would be
claiming a checksum Solana does not have.
"""

from __future__ import annotations

import pathlib
import re
import sys

import base58
import pytest

REPOSITORY_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "swap_terminal"))

from modules.address_authority import (  # noqa: E402  the path shim above must run first
    INVALID,
    NOT_EXPRESSED,
    VALIDATORS,
    check_address,
)
from modules.address_network import (  # noqa: E402  the path shim above must run first
    BITCOIN_BASE58_ALPHABET,
    MAINNET,
    TESTNET,
    XRP_BASE58_ALPHABET,
    decodes_as_address,
)
from valid_addresses import (  # noqa: E402  conftest puts tests/ on sys.path
    ALL_VALID,
    INVALID_PLACEHOLDERS,
)

_B58 = BITCOIN_BASE58_ALPHABET.decode()


def fixture_asset(name: str) -> str:
    """Which chain a fixture name belongs to: everything before the first underscore.

    ADDED 2026-09-27, AND THE REASON IS THE DEFECT THIS WHOLE DAY IS ABOUT. The two fixture
    tests below used to call modules/address_network.decodes_as_address(), which understands
    bech32, Bitcoin-alphabet base58check and XRP-alphabet base58check -- and NOTHING ELSE.
    So the moment XMR_PAYOUT and SOL_PAYOUT were added to valid_addresses.py, this gate
    failed on three valid addresses:

        XMR_PAYOUT         "not decodable as base58check"   (Monero: 11-char blocks, Keccak)
        XMR_SECOND_PAYOUT  "not decodable as base58check"
        SOL_PAYOUT         accepted/refused by length alone (Solana has NO checksum)

    That is exactly the trap modules/address_authority.py exists to name, arriving in a
    TEST instead of on the fund path: a blanket check over every chain calls two working
    chains invalid. Had the same blanket check been put at payout_service's send site, every
    valid Monero payout would have been refused -- on a swap whose deposit was already taken.

    So the gate now asks the AUTHORITY, per asset, which is the same thing the fund path
    asks (rule 8: one question, one answer, one place).

    An UNPREFIXED name, or one naming a chain with no validator, raises rather than defaults
    -- a fixture silently skipped is a fixture nobody is checking.
    """
    asset = name.split("_", 1)[0]
    assert asset in VALIDATORS, (
        f"fixture {name!r} names asset {asset!r}, which has no validator in "
        f"modules/address_authority.VALIDATORS ({', '.join(sorted(VALIDATORS))}). Name it after its "
        f"chain, or add the validator -- a fixture no validator covers is unchecked."
    )
    return asset
_XRP = XRP_BASE58_ALPHABET.decode()

# Solana keys are 32 raw bytes in base58 with no checksum and no version byte.
SOLANA_PUBKEY_BYTES = 32

# A literal claiming a bech32 human-readable part. tgrc1/grc1 are included precisely because
# they CANNOT be valid -- Gridcoin has no bech32 -- and the tree contained two of them.
BECH32_LITERAL = re.compile(
    r"""["']((?:bcrt1|tltc1|tgrc1|grc1|tb1|bc1|ltc1)[a-zA-Z0-9]{6,80})["'](?P<after>.?)"""
)

# A base58-shaped literal of address length, under either alphabet's characters.
BASE58_LITERAL = re.compile(
    r"""["']([rRSmn123][""" + re.escape(_B58 + _XRP) + r"""]{25,40})["'](?P<after>.?)"""
)

SKIP_DIRECTORIES = {".git", "__pycache__", "node_modules", ".venv", "grc-sol-swap"}


def _is_a_dict_key(match: re.Match) -> bool:
    """True when the literal is immediately followed by `:` -- a dict key, so a FIELD NAME.

    THIS REPLACED `value.isidentifier()`, WHICH WAS A HOLE THAT NEARLY DISABLED THIS GATE.
    base58's alphabet is alphanumeric only, so every base58 address beginning with a letter is a
    valid Python identifier -- measured: RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV (the real mainnet
    accident), mqT6J2i1qsDTo1fVfpEbUupe2c13pr5T3M, S8kKq2VrZ4mQvYtN6dWxJ3hLpB7cFgTnEu and
    rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh all return True from isidentifier(). The exclusion
    written to skip one RPC parameter name was skipping essentially the whole base58 half of the
    gate, and only bech32 and digit-leading addresses were being checked at all.

    It was caught by this file's own control test, not by anything failing -- a gate that checks
    nothing reports success, which is rule 13's "skipped and exit_code=0 in the same output".

    The one literal it exists for is chains/solana.py's
    `{"encoding": ..., "maxSupportedTransactionVersion": 0}` -- a JSON-RPC option name that is
    base58-legal by coincidence. A key position is a precise signal: nothing in this repository
    pays money to a dict key.

    ITS OWN HOLE, STATED: a fixture mapping address -> balance would put a real address in key
    position and be skipped. There is none in the tree today (grepped by name), and the trade is
    deliberate -- this exclusion is narrow enough to name its own gap, where isidentifier() was
    broad enough to hide the gate.
    """
    return match.group("after").strip().startswith(":")


def _is_an_alphabet(value: str) -> bool:
    """True when the literal is (part of) a base58 charset rather than an address."""
    return any(value in alphabet or alphabet in value for alphabet in (_B58, _XRP))


def _is_a_solana_pubkey(value: str) -> bool:
    """True when the literal is a 32-byte plain-base58 key, which is Solana's format.

    EXCLUDED BY PROPERTY, NOT BY PATH, and the difference matters. A path exclusion
    ("skip tests/test_solana_*.py") is a baseline: it stops checking a file rather than
    describing what is not checkable, so an invalid BITCOIN address written into a Solana test
    would sail through. This asks the question that actually distinguishes them -- does it
    decode as plain base58 to exactly 32 bytes, with no checksum -- which is true of
    `11111111111111111111111111111111` (the system program) and
    `So11111111111111111111111111111111111111112`, and false of every address in this gate's
    scope.

    Solana's format carries NO checksum, so nothing here can validate one. chains/solana_address
    owns that format, and claiming to check it here would be claiming a guarantee Solana does
    not offer.
    """
    try:
        return len(base58.b58decode(value)) == SOLANA_PUBKEY_BYTES
    except Exception:  # noqa: BLE001 -- checked: the only question is "does this decode as plain base58 to 32 bytes", and False says no. Every exception base58 raises for a bad character or a short string means the same thing, and False routes the literal INTO the gate rather than out of it -- so a mistake here makes the check stricter, never looser.
        return False


def _source_files() -> list[pathlib.Path]:
    return [
        path
        for path in sorted(REPOSITORY_ROOT.rglob("*.py"))
        if not SKIP_DIRECTORIES & set(path.parts)
    ]


def _candidates(paths=None):
    """(path, line_number, literal) for every address-shaped literal in `paths` (default: tree).

    PARAMETERIZED SO THE GATE CAN BE TESTED ON A PLANTED VIOLATION. It was not, and a mutation
    check found what that costs: making _is_a_dict_key() return True unconditionally -- which
    skips EVERY literal and turns this whole file into a no-op -- left the suite green, because a
    gate with nothing to find and a gate that cannot find anything look identical from outside.
    Same shape as rule 13's twelve cycles printing exit_code=0 beside "skipping".
    """
    for path in _source_files() if paths is None else paths:
        text = path.read_text(errors="replace")
        for number, line in enumerate(text.splitlines(), 1):
            for matcher in (BECH32_LITERAL, BASE58_LITERAL):
                for match in matcher.finditer(line):
                    value = match.group(1)
                    if (_is_a_dict_key(match) or _is_an_alphabet(value)
                            or _is_a_solana_pubkey(value)):
                        continue
                    try:
                        reported = path.relative_to(REPOSITORY_ROOT)
                    except ValueError:
                        reported = path  # a planted file outside the tree, in a test
                    yield reported, number, value


def test_the_matcher_still_matches_addresses_and_still_skips_the_things_that_are_not():
    """A GATE THAT MATCHES NOTHING PASSES FOREVER, which is the failure this guards.

    The first version of this test asserted "at least 20 address-shaped literals exist in the
    tree", and it failed -- on a clean tree, for the right reason. After the sweep that replaced
    them with derived fixtures there are FOUR left, so the count was measuring how many literals
    the repository happens to contain rather than whether the regex works. Four is the goal, not
    a symptom.

    So the matcher is tested against controls instead, which is the property that actually
    matters: it must MATCH real addresses of every shape this repository pays, and must SKIP the
    three kinds of non-address the tree genuinely contains. That is independent of how many
    literals anybody writes.
    """
    must_match = [
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",                 # BTC mainnet P2PKH (Satoshi's)
        "3J98t1WpEZ73CNmQviecrnyiWrnqRhWNLy",                # BTC mainnet P2SH
        "2MwJ8Q735vzVkaL1SnfbGvefofrDnXgXbfY",               # testnet P2SH
        "RyyNX8E7tDRzACL47h8JKKDUXf9aMCz8YV",                # GRC mainnet -- the accident
        "mqT6J2i1qsDTo1fVfpEbUupe2c13pr5T3M",                # GRC testnet
        "rHb9CJAWyB4rj91VRWn96DkukG4bwdtyTh",                # XRP
        "tb1qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",         # BTC testnet bech32
        "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4",         # BTC mainnet bech32
        "tltc1qu3ksx4xz82ngf3my3pjm4aejkst40ay88ua93a",       # LTC testnet bech32
        # A format that CANNOT exist, built rather than spelled -- the hrp of a real testnet
        # bech32 address swapped for Gridcoin's imaginary one. Writing the literal would put an
        # undecodable address in this file, which the gate below would then (correctly) fail on:
        # it caught exactly that when the Satoshi address above was first mistyped.
        "tgrc1" + "qw508d6qejxtdg4y5r3zarvary0c5xw7kxpjzsx",
    ]
    for address in must_match:
        line = f'    x = "{address}"'
        matched = [m.group(1) for rx in (BECH32_LITERAL, BASE58_LITERAL) for m in rx.finditer(line)]
        assert address in matched, f"the matcher no longer sees {address}"
        assert not (_is_an_alphabet(address) or _is_a_solana_pubkey(address)), (
            f"{address} is being excluded by a value-based rule"
        )
        # Every control except the impossible-hrp one must itself decode. A mistyped control
        # should fail here, naming itself, rather than as a tree violation one test later --
        # which is how the mistyped Satoshi address surfaced the first time.
        if not address.startswith("tgrc1"):
            decodable, why = decodes_as_address(address)
            assert decodable, f"control {address} does not decode: {why}"

    must_skip = [
        (_B58, "Bitcoin's base58 alphabet, which is data"),
        (_XRP, "XRP's base58 alphabet"),
        ("11111111111111111111111111111111", "Solana's system program: 32 raw bytes, no checksum"),
        ("So11111111111111111111111111111111111111112", "Solana wrapped SOL"),
    ]
    for value, why in must_skip:
        # In VALUE position, which is how a real address appears.
        line = f'    x = "{value}"'
        matches = [m for rx in (BECH32_LITERAL, BASE58_LITERAL) for m in rx.finditer(line)]
        excluded = _is_an_alphabet(value) or _is_a_solana_pubkey(value)
        assert excluded or not matches, f"{value} ({why}) reaches the gate and would fail it"

    # And the dict-key rule, on the exact line that made it necessary.
    key_line = '        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},'
    keyed = [m for m in BASE58_LITERAL.finditer(key_line) if not _is_a_dict_key(m)]
    assert not keyed, f"the dict-key rule no longer skips {[m.group(1) for m in keyed]}"


def test_the_literal_count_does_not_climb_back():
    """Rule 3: state the denominator, and state it after checking the instrument.

    THIS TEST'S FIRST NUMBER WAS WRONG AND THE REASON IS THE POINT. It asserted "at most 12
    literals, down from 4 after the sweep" -- and 4 was an artifact of the broken
    isidentifier() exclusion described in _is_a_dict_key(), which was silently skipping every
    base58 address that begins with a letter. With the instrument fixed the same tree measures:

        48 address literals across 23 files, of which 0 are undecodable

    Both figures came from running the scanner, five minutes apart, over the same files. The
    first one was not a smaller count; it was the same count taken through a broken lens, which
    is exactly the failure this whole gate is about -- a check that is not checking reports
    success.

    So the assertion is a CEILING on drift rather than a claim about a good number. 48 is not a
    target and the nine in this file are controls. What must not happen is literals accumulating
    again, which is how the 25 undecodable ones arrived: one at a time, each written by somebody
    who needed a plausible string for one test.
    """
    literals = list(_candidates())
    ceiling = 60
    assert len(literals) <= ceiling, (
        f"{len(literals)} address literals in the tree, over the ceiling of {ceiling} "
        f"(measured 48 on 2026-09-27). Use tests/valid_addresses.py rather than writing one -- "
        f"a derived address cannot be mistyped and says what it is for:\n"
        + "\n".join(f"  {path}:{number}  {value}" for path, number, value in literals)
    )


def test_the_gate_catches_a_planted_invalid_address(tmp_path):
    """THE POSITIVE CONTROL, and the gate had none until a mutation check asked for one.

    Every other test here confirms the tree is CLEAN -- which a gate that checks nothing also
    confirms. Making _is_a_dict_key() return True unconditionally, so every literal is skipped
    and this whole file becomes a no-op, left the suite green. A gate with nothing to find and a
    gate that cannot find anything look identical from outside.

    The planted strings come from valid_addresses.INVALID_PLACEHOLDERS, where each is DERIVED
    from a valid address rather than spelled -- because writing them here as literals put invalid
    addresses in this file and the gate below failed on them, correctly. Suppressing the gate for
    its own test file would have been a baseline, which rule 19 forbids.
    """
    planted = tmp_path / "planted.py"
    planted.write_text(
        "\n".join(f'X{index} = "{value}"'
                  for index, value in enumerate(INVALID_PLACEHOLDERS.values()))
    )
    found = {value for _path, _number, value in _candidates([planted])}

    # FIVE OF THE SIX, AND THE SIXTH IS A REAL LIMIT OF SCANNING SOURCE -- stated rather than
    # rounded away. The charset case ends in "0", a character base58 excludes precisely because
    # it is confusable with O, so the string is not address-SHAPED and this matcher correctly
    # declines to call it an address. decodes_as_address() still refuses it, which is the check
    # that runs when something is about to be paid; only the source scanner cannot see it. A
    # matcher widened to catch it would start matching every hex digest and identifier in the
    # tree, which is the trade that produced the isidentifier() hole above.
    charset_case = INVALID_PLACEHOLDERS["base58 charset"]
    assert charset_case not in found, (
        "if the matcher now sees a non-base58 character, it will also see hex digests"
    )
    expected = set(INVALID_PLACEHOLDERS.values()) - {charset_case}
    assert found == expected, f"the scanner missed {sorted(expected - found)}"

    # Every one of the six is refused by the decoder, scanner-visible or not.
    for label, value in INVALID_PLACEHOLDERS.items():
        decodable, why = decodes_as_address(value)
        assert not decodable, f"{label}: {value} was accepted as valid ({why})"


def test_a_planted_valid_address_is_not_a_violation(tmp_path):
    """The other direction, so the gate cannot pass by refusing everything. A file of real
    addresses must produce matches and zero violations."""
    planted = tmp_path / "fine.py"
    planted.write_text("\n".join(f'X{i} = "{a}"' for i, a in enumerate(sorted(ALL_VALID.values()))))
    found = list(_candidates([planted]))
    assert len(found) >= 10, f"only {len(found)} of {len(ALL_VALID)} valid fixtures matched"
    # Asked per asset, via the reverse lookup, for the reason fixture_asset() gives: a
    # blanket decoder refuses the Monero and Solana fixtures, which are valid.
    by_value = {value: name for name, value in ALL_VALID.items()}
    refused = [
        f"{by_value[v]}={v}: {check_address(fixture_asset(by_value[v]), v).why}"
        for _p, _n, v in found
        if v in by_value and check_address(fixture_asset(by_value[v]), v).state == INVALID
    ]
    assert refused == [], refused


def test_every_address_shaped_literal_in_the_tree_decodes():
    """THE GATE. No baseline, no allowlist, no per-file tolerance.

    A failure names the file, the line and WHY, because the reason chooses the fix: a bad
    checksum is a typo, `grc1...` is a format that does not exist, and a 22-byte payload is a
    truncation."""
    violations = []
    for path, number, value in _candidates():
        decodable, why = decodes_as_address(value)
        if not decodable:
            violations.append(f"  {path}:{number}  {value}\n        {why}")
    assert not violations, (
        f"{len(violations)} address-shaped literal(s) do not decode as any address this "
        f"repository can pay:\n" + "\n".join(violations)
    )


@pytest.mark.parametrize("name", sorted(ALL_VALID))
def test_every_shared_fixture_address_decodes(name):
    """The fixtures themselves, one test each so a failure names which one.

    Through the per-asset authority rather than the three-encoding decoder; see
    fixture_asset() for the measurement that forced the change.
    """
    verdict = check_address(fixture_asset(name), ALL_VALID[name])
    assert verdict.state != INVALID, f"{name} = {ALL_VALID[name]} does not decode: {verdict.why}"


@pytest.mark.parametrize("name", sorted(ALL_VALID))
def test_no_shared_fixture_is_a_mainnet_address(name):
    """A mainnet address in a fixture is one copy-paste from a real payout.

    NO NAME-PREFIX EXCLUSION ANY MORE, and that is a strengthening rather than a tidy-up.
    This used to skip every fixture starting with "XRP", because XRP has no testnet address
    format -- true, and it meant a rule enforced by SPELLING: a fixture named XRP_anything
    was exempt, including one that should not have been. SOL_PAYOUT would have needed a
    second such exemption and XMR_PAYOUT must not get one.

    So the exemption is now read off the ENCODING instead. address_authority reports
    NOT_EXPRESSED for a format that carries no network at all (XRP, Solana), which is an
    absent question rather than a failed measurement, and every other fixture must say
    TESTNET. A future XRP fixture that somehow decoded as mainnet would now fail, where the
    old spelling-based skip would have waved it through.
    """
    verdict = check_address(fixture_asset(name), ALL_VALID[name])
    assert verdict.network != MAINNET, f"{name} = {ALL_VALID[name]} is MAINNET: {verdict.why}"
    if verdict.network != NOT_EXPRESSED:
        assert verdict.network == TESTNET, f"{name} = {ALL_VALID[name]} is not testnet: {verdict.why}"
