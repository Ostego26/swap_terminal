"""Do the credentials THIS PROCESS would use match the ones a daemon actually read?

Role: submodule (one decision -- same, different, or absent -- over two mappings it is
      handed. It opens no file, reads no environment and holds no transport.)
Reads: nothing. Two dicts, passed in.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=========================================================================
IT COMPARES VALUES AND CAN NEVER REPORT ONE
=========================================================================

That is the whole design constraint and it is why this is its own module rather than
four lines in a script. CLAUDE.md is unambiguous: a credential is "never read back into
a terminal, and never printed into a log or a commit." A comparison needs the values; a
REPORT must not have them. So the values enter compare() and only a verdict word leaves,
and tests/test_credential_match.py seeds distinctive values and asserts that no returned
string contains any of them -- the same shape tests/test_secrets_are_not_logged.py
already uses.

NO HASHES EITHER, and that was considered and refused. A SHA-256 of an rpcpassword is
not the password, but it is derived from it, it would be pasted into a terminal and then
into a chat, and a weak password's hash is brute-forceable. A boolean answers the
question completely, so anything stronger than a boolean is a leak with no upside.

=========================================================================
WHY THE QUESTION CAME UP, AND WHY MY FIRST CHECK WAS THE WRONG ONE
=========================================================================

2026-10-12. The operator's Gridcoin desk daemon answered 401 Authorization Required,
from the CONTAINER and -- decisively -- from the HOST on 127.0.0.1. Gridcoin hardcodes
127.0.0.0/8 as allowed before the allow list is consulted (src/rpc/server.cpp:521-526),
so a loopback 401 rules out rpcallowip, the bind, the bridge and the firewall together:
it is the credentials and nothing else.

I then asked them to run `grep -cE '^(rpcuser|rpcpassword)'` over both conf files and
said "a 0 for the testnet file is the whole answer." It came back 2 and 2. THAT CHECK
TESTED THE WRONG PROPOSITION: I tested whether the net-specific file HAS credentials
when the question is whether they MATCH. And the sentence this tree already had said so
-- regtest/daemons.GRC_CREDENTIALS_ARE_PER_NETWORK: the testnet conf "carries DIFFERENT
credentials from the mainnet file beside it." Presence was never the issue.

So this module answers the proposition that was actually open, and it answers it without
anybody reading a password out loud.

=========================================================================
WHAT IT DOES NOT DO
=========================================================================

NOT A CONSTANT-TIME COMPARE, deliberately. This compares a local config file against a
local environment for a human reading a report; there is no remote attacker timing it,
and `hmac.compare_digest` here would imply a threat model that does not apply and
suggest this is an authentication path. It is not -- the daemon does the authenticating.

NOT A CONF READER. chains/daemon_conf.py already parses a daemon conf and already names
rpcpassword in SECRET_CONF_KEYS; this takes the mapping that comes out. One parser
(rule 8).
"""

from __future__ import annotations

from typing import NamedTuple

#: The verdicts. Five, and none of them collapses into another: BOTH_ABSENT is a real
#: and common state (an unencrypted regtest daemon with no credentials at all) and must
#: not read as a match, while a value present on one side only sends a reader to a
#: different file from a value present on both and differing.
SAME, DIFFERENT, ONLY_IN_ENV, ONLY_IN_CONF, BOTH_ABSENT = (
    "SAME", "DIFFERENT", "ONLY IN THE ENVIRONMENT", "ONLY IN THE CONF", "ABSENT FROM BOTH"
)

#: Which environment field pairs with which conf key. The conf spellings are Bitcoin
#: Core's and Gridcoin's alike -- both use rpcuser/rpcpassword, which is one of the
#: places they genuinely agree (see chains/rpc_matrix.py for where they do not).
FIELDS: tuple[tuple[str, str, str], ...] = (
    ("user", "rpcuser", "the RPC username"),
    ("password", "rpcpassword", "the RPC password"),
)


class FieldVerdict(NamedTuple):
    """One field's answer. Carries NO value and cannot be made to.

    `label` is prose for a person, `env_key` and `conf_key` are NAMES, and `verdict` is
    one of the five words above. There is deliberately no field a value could occupy --
    a caller that wanted to print one would have to go and get it itself, which is the
    point.
    """

    label: str
    env_key: str
    conf_key: str
    verdict: str


def compare(configured: dict, conf_values: dict) -> list[FieldVerdict]:
    """Per field: same, different, or missing on one side. Values in, verdicts out.

    `configured` is a Config.RPC entry (keys `user` and `password`); `conf_values` is
    what chains/daemon_conf parsed out of the daemon's own file (keys `rpcuser` and
    `rpcpassword`).

    EMPTY AND ABSENT ARE THE SAME THING HERE, which is config.py's own convention: an
    unset variable arrives as "" rather than as a missing key, so `or ""` collapses both
    and a reader is not told the difference between a variable that does not exist and
    one set to nothing. Neither can authenticate.
    """
    verdicts = []
    for env_key, conf_key, label in FIELDS:
        mine = str(configured.get(env_key) or "")
        theirs = str(conf_values.get(conf_key) or "")
        if not mine and not theirs:
            verdict = BOTH_ABSENT
        elif not theirs:
            verdict = ONLY_IN_ENV
        elif not mine:
            verdict = ONLY_IN_CONF
        else:
            verdict = SAME if mine == theirs else DIFFERENT
        verdicts.append(FieldVerdict(label, env_key, conf_key, verdict))
    return verdicts


def headline(verdicts: list[FieldVerdict]) -> str:
    """One line saying whether this explains a 401. Never names a value.

    THE SENTENCE AN OPERATOR ACTS ON. A 401 with every field SAME means the conf being
    compared is not the file the daemon read -- which on Gridcoin is the likeliest
    remaining cause, because it uses split config files per network and nothing in the
    file itself says which network it belongs to.
    """
    states = {verdict.verdict for verdict in verdicts}
    if states == {SAME}:
        return (
            "EVERY FIELD MATCHES this conf. So if the daemon is still answering 401, this is "
            "NOT the file it read -- check its startup log for `Using data directory "
            "<datadir>/testnet` and compare against the conf in THAT directory. Gridcoin uses "
            "a separate file per network and the file itself does not say which one it is."
        )
    if DIFFERENT in states:
        return (
            "AT LEAST ONE FIELD DIFFERS, which is sufficient on its own to produce 401 "
            "Authorization Required. Set the environment to match this file -- or point the "
            "environment at the daemon these credentials belong to."
        )
    if ONLY_IN_CONF in states:
        return (
            "THE DAEMON HAS A CREDENTIAL THIS PROCESS DOES NOT, so every call it makes is "
            "unauthenticated and 401 is the expected answer."
        )
    if ONLY_IN_ENV in states:
        return (
            "THIS PROCESS HAS A CREDENTIAL THE CONF DOES NOT, so either this is the wrong conf "
            "or the daemon is reading its credentials from somewhere else -- a command-line "
            "flag beats a conf file."
        )
    return (
        "NEITHER SIDE HAS A CREDENTIAL. A daemon with no rpcuser and no rpcpassword refuses "
        "RPC outright, which is 401, and setting them in the conf requires a restart."
    )


#: THE GRIDCOIN-ONLY FLAG, named once. configure_env.py gates Gridcoin's conf behind its
#: own opt-in because the DEFAULT datadir's file belongs to a live staking wallet; BTC and
#: LTC need no second flag. Spelled here rather than inline in the sentence below so the
#: branch is one lookup and a fourth chain needing a flag is one entry.
_EXTRA_READ_FLAG = {"GRC": " --read-gridcoin-conf"}


def how_to_settle_a_401(asset: str) -> str:
    """The two commands that settle a 401 without anybody reading a password out loud.

    =========================================================================
    THIS SENTENCE EXISTS BECAUSE I HAD TO TYPE IT INTO A CHAT TWICE
    =========================================================================

    2026-10-12 and again on 2026-10-13. The operator's GRC answered 401 from the host
    and from the container; `swap_stack.py off`, `up` step 8 and `chains` each printed
    the recorded remedy, which says correctly what a 401 PROVES and which FILE the
    credentials live in -- and named no command. So the two things that actually resolve
    it reached them only because I said so in conversation, which is the one place a
    remedy cannot be re-read from.

    daemon_capabilities.py's own header makes this argument about itself: "A capability
    map that no report reads is documentation, and rule 5's test applies to it: if a
    reader has to open a file to learn something, it is in the wrong place." A remedy
    that lives only in a chat message is one step worse than that.

    =========================================================================
    WHY IT IS HERE AND NOT IN daemon_capabilities OR regtest/daemons
    =========================================================================

    Both of those print a 401 remedy and both would otherwise spell these commands --
    `SHAPE_MEANS` for any chain, `regtest/daemons.GRC_CREDENTIALS_ARE_PER_NETWORK` for
    Gridcoin -- which is rule 8's exact shape, and daemon_capabilities' SHAPE_MEANS
    comment already records catching it once: "The first draft of the 401 entry here
    restated it, which would have made a third copy of the one sentence in this tree
    that is already shared properly."

    This module is the leaf both can import (it pulls in nothing but `typing`, where
    regtest/daemons already imports chains.base), and it is the module whose subject IS
    comparing credentials without reporting one. So the commands live beside the
    comparison they drive.

    =========================================================================
    THE CONF PATH IS A PLACEHOLDER AND MUST STAY ONE
    =========================================================================

    Every other value here is literal; `<datadir>` is not, and that is the finding
    rather than a gap. This host runs three Gridcoin wallets, the datadir is chosen by
    whoever typed the launch command, and guessing it is the failure measured on
    2026-09-29 and again on 2026-10-12. The daemon prints `Using data directory
    <datadir>/testnet` every run and that is the only reliable source, so the sentence
    sends the reader there rather than naming a path that is right one time in three.

    NEITHER COMMAND PRINTS A VALUE. credential_check.py returns verdict words from a
    type with no field a value could occupy; configure_env.py copies the conf into .env
    with the value in a tuple and never in a reason line. That property is what makes
    these safe to put in a report an operator pastes into a chat.
    """
    flag = _EXTRA_READ_FLAG.get(asset.upper(), "")
    return (
        f"TWO COMMANDS SETTLE IT AND NEITHER PRINTS A PASSWORD. (a) `python3 "
        f"credential_check.py --asset {asset} --conf <the conf that daemon read>` says SAME or "
        f"DIFFERENT per field, so you learn WHICH of the two is wrong rather than retyping both. "
        f"(b) `python3 configure_env.py --read-daemon-confs{flag} --conf "
        f"{asset}=<that same path> --apply` copies them out of that conf into .env for you -- the "
        f"value goes in the file and never on the screen. Then recreate the container (`python3 "
        f"swap_stack.py restart`), because an already-running one keeps the environment it "
        f"started with."
    )
