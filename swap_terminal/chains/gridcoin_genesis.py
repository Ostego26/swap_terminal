"""Which Gridcoin network a daemon is on, decided by its GENESIS BLOCK HASH.

Role: submodule (the one place that maps a Gridcoin genesis hash to a network name)
Reads: nothing -- a pure table plus one classifier. No I/O, no environment.
Writes: nothing
Can move funds: no, and yes. It moves nothing itself, but it is read by
      regtest/funding_steps.assert_test_network(), WHICH IS THE GATE THAT DECIDES
      WHETHER A RUN MAY BROADCAST. A wrong entry here would let a run proceed
      against Gridcoin mainnet, so every value below is quoted with the file and
      line it was read from rather than recalled.
Mainnet-safe: yes (no I/O). The TABLE is about mainnet; the module touches nothing.

=========================================================================
WHY A GENESIS HASH RATHER THAN THE `testnet` BOOLEAN
=========================================================================

MEASURED 2026-11-22, against the exact source docker/grc-desk.Dockerfile pins
(ref `5.5.1.7-testnet`, commit 30a89f2ab0bb4f7134ca7a9c74e233e76130ebd8, clone
verified with `git rev-parse HEAD`).

Gridcoin has THREE networks at that commit -- `main`, `test` and `regtest`
(src/chainparamsbase.cpp:13-15, and CRegTestParams at src/chainparams.cpp:398-401)
-- and NO RPC THAT REPORTS A NETWORK NAME AT ANY VERSION. `getblockchaininfo` has
no `chain` field; it pushes a BOOLEAN:

    src/rpc/blockchain.cpp:4809   res.pushKV("testnet", OnTestnet())
    src/chainparams.h:129-132     inline bool OnTestnet() {
                                      return Params().NetworkIDString() == CBaseChainParams::TESTNET; }

So `testnet` is FALSE ON MAINNET AND FALSE ON REGTEST. One bit cannot name three
networks, and the two it conflates are the only two that matter here: the one that
holds real value and the one that holds none.

THAT CONFLATION WAS A LIVE DEFECT IN THIS REPOSITORY, and it is the reason this
module exists. funding_steps.MAINNET_STATEMENTS mapped `testnet == False` to a
POSITIVE STATEMENT OF MAINNET for all three of its signals. On a Gridcoin regtest
node every one of those signals answers False, so the gate classified a regtest
daemon -- a chain whose coins are minted out of nothing -- as mainnet and refused
the run, with no override. Its own comment had even read `CBaseChainParams::REGTEST
is "regtest"` off chainparamsbase.cpp while writing the mapping.

The error is not "the gate was too strict". It is that AN AMBIGUOUS ANSWER WAS
RECORDED AS POSITIVE EVIDENCE, which is the exact failure the gate was built to
prevent, running in the opposite direction. Its governing sentence is "an absence
of evidence is treated as mainnet"; `testnet=False` is an absence of evidence
about which of two networks you are on, and it was being treated as presence.

A GENESIS HASH IS THE OPPOSITE KIND OF ANSWER. It names one network out of all of
them, it is a consensus constant rather than a status field, and no proxy, port
forward or half-migrated daemon can present mainnet's genesis while serving
regtest -- the hash IS the chain. So this is a STRONGER gate than the boolean it
supplements, not a relaxation of it. A daemon that answers with mainnet's genesis
is refused on better evidence than before.

THE PINNED VALUES, read from src/chainparams.h at the commit above:

    :26   hashGenesisBlock         main      000005a2…c191599
    :29   hashGenesisBlockTestNet  test      00006e03…9bae713
    :33   hashGenesisBlockRegTest  regtest   4692b956…1e065371

and each is asserted by the daemon against the block it actually builds, so a
running node cannot disagree with its own table:

    src/chainparams.cpp:145   assert(block.GetHash(true) == (!OnTestnet() ? hashGenesisBlock
                                                                          : hashGenesisBlockTestNet))
    src/chainparams.cpp:155   assert(block.GetHash(true) == hashGenesisBlockRegTest)
    src/chainparams.cpp:221   assert(consensus.hashGenesisBlock == uint256S("0x000005a2…"))
    src/chainparams.cpp:350   assert(consensus.hashGenesisBlock == uint256S("0x00006e03…"))

MAIN AND TEST ARE VERSION-INDEPENDENT; REGTEST IS NOT. A genesis hash is a
consensus constant, so `main` and `test` carry over to 5.5.1.0 and to any other
Gridcoin unchanged. `regtest` DID NOT EXIST at 5.5.1.0 -- that tag declares only
MAIN and TESTNET (measured: `git show 5.5.1.0:src/chainparamsbase.cpp`, two
branches plus a throw, no CRegTestParams class, `grep -c regtest` -> 0) -- so a
daemon answering with the regtest genesis is necessarily 5.5.1.7 or later. That is
information rather than a problem, and network_of_genesis() does not need to know
it.

WHAT IS *NOT* ESTABLISHED, stated in its own voice (rule 17). Every value here was
READ FROM SOURCE. No Gridcoin daemon has answered `getblockhash 0` for this
repository, because none has ever been reachable from the environment these lines
were written in. In particular Gridcoin spells its block hash `GetHash(true)` at
the assert sites above -- a PARAMETERIZED hash -- and whether `getblockhash 0`
returns that same value or some other digest is READ, not MEASURED. The failure
mode is the safe one: an unrecognized hash yields None, which the gate treats as
no evidence, which it treats as mainnet, so it refuses. classify() therefore
returns the hash it did not recognize, so the first real run says WHICH value to
add here instead of just refusing with nothing to go on (rule 14).
"""

from __future__ import annotations

#: Gridcoin's network name for each genesis hash, lowercase and without `0x`.
#:
#: THE KEYS ARE THE NETWORK'S IDENTITY. They are consensus constants read from
#: src/chainparams.h at commit 30a89f2ab0bb4f7134ca7a9c74e233e76130ebd8; the module
#: docstring above names the line for each one and the assert that binds it to the
#: block the daemon actually builds.
#:
#: THE VALUES ARE GRIDCOIN'S OWN SPELLINGS, from src/chainparamsbase.cpp:13-15 --
#: CBaseChainParams::MAIN is "main", TESTNET is "test", REGTEST is "regtest". They
#: are NOT this repository's asset or network vocabulary and are deliberately not
#: translated here: a table that renames what it reads is a table a reader cannot
#: check against the source it claims to quote.
GRIDCOIN_GENESIS: dict[str, str] = {
    "000005a247b397eadfefa58e872bc967c2614797bdc8d4d0e6b09fea5c191599": "main",
    "00006e037d7b84104208ecf2a8638d23149d712ea810da604ee2f2cb39bae713": "test",
    "4692b9564c585f76f41e02e304767b2ad16f1d18f72efcf0a724efe01e065371": "regtest",
}

#: The network whose coins are real. Named once so no caller spells it inline, and
#: so the one comparison that must never be got wrong reads as a name.
GRIDCOIN_MAINNET = "main"

#: The networks a harness may broadcast on. `regtest` is here because its coins are
#: minted from a genesis premine and `test` because its coins come from a faucet;
#: neither can be sold. It is a tuple rather than a set so that reporting it has a
#: stable order (rule 14: the same run must print the same line twice).
GRIDCOIN_TEST_NETWORKS = ("test", "regtest")


def normalize_block_hash(value: object) -> str:
    """A block hash as the table spells it: lowercase hex, no `0x`, nothing else.

    WHY THIS IS NOT `str.lower()` AT EACH CALL SITE. Three spellings of one hash
    reach this module from two directions -- Gridcoin's own source writes
    `uint256S("0x4692…")` WITH the prefix, while `getblockhash` returns 64 bare hex
    characters, and JSON gives whatever the daemon sent. A table keyed on one
    spelling and probed with another answers "unknown network", and on this gate
    "unknown" means "refuse", so the failure would arrive as a harness that cannot
    be started rather than as an obvious typo.

    ANYTHING THAT IS NOT A STRING COMES BACK AS "". A daemon that answers
    `getblockhash 0` with null, a number or a list has not given a hash, and
    returning "" rather than raising lets the gate report "no evidence" through the
    one path it already has for that -- which is the path that refuses.
    """
    if not isinstance(value, str):
        return ""
    return value.strip().lower().removeprefix("0x")


def network_of_genesis(value: object) -> str | None:
    """Gridcoin's name for the network with this genesis hash, or None if unknown.

    None IS NOT "mainnet" AND IT IS NOT "safe". It means this module cannot say,
    and every caller must treat it the way funding_steps treats an absent field:
    as an absence of evidence, which that gate treats as mainnet and refuses on.
    Returning a string here for an unrecognized hash -- any string -- would convert
    "I do not know" into a claim, which is the defect this module was written to
    remove.
    """
    return GRIDCOIN_GENESIS.get(normalize_block_hash(value))


def is_test_network(value: object) -> bool:
    """True only when this genesis hash NAMES a Gridcoin network that holds no value.

    Written as a positive membership test against GRIDCOIN_TEST_NETWORKS rather than
    as `!= GRIDCOIN_MAINNET`, and the difference is the whole point: `!=` would
    answer True for None, for "", for a typo and for any future network nobody has
    considered. The question a broadcasting harness has to answer is "did something
    SAY this is a test network", and only a name in that tuple says it.
    """
    return network_of_genesis(value) in GRIDCOIN_TEST_NETWORKS


def is_mainnet(value: object) -> bool:
    """True only when this genesis hash IS Gridcoin mainnet's.

    The strongest refusal signal this module can produce, and it is deliberately a
    separate function from `not is_test_network(...)`: an unknown hash is neither a
    test network nor a positive statement of mainnet, and a gate that reports it as
    mainnet would be making the same over-claim -- in the same direction -- as the
    `testnet == False` mapping this module replaces. Unknown is unknown; it refuses
    for lack of evidence, not for evidence of mainnet, and the operator reading the
    run needs those two refusals to say different things.
    """
    return network_of_genesis(value) == GRIDCOIN_MAINNET


def describe(value: object) -> str:
    """One phrase naming what this hash identifies, for a run's own output.

    NEVER A BLANK, and never just "unknown" (rule 14: an empty result must still be
    a result, and a count or a value is worth more than an adjective). An
    unrecognized hash is ECHOED BACK, truncated, because the first real Gridcoin
    daemon to answer this gate may answer with a digest this table does not carry --
    and the operator reading that refusal needs the value to add, not the news that
    there was one. See the docstring's closing paragraph: `getblockhash 0` returning
    these exact bytes is READ from source and has never been measured.
    """
    text = normalize_block_hash(value)
    if not text:
        return "no genesis hash was returned"
    network = GRIDCOIN_GENESIS.get(text)
    if network is None:
        return f"genesis {text[:16]}… is NOT a Gridcoin network this table knows"
    return f"genesis {text[:16]}… is Gridcoin {network}"
