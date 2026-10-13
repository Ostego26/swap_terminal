#!/usr/bin/env python3
"""Discover what it can, ask for the rest, and write it into .env so it PERSISTS.

Role: file (entry point -- what an operator runs)
Reads: .env (by name, never echoed), .env.example for the variable list and each
       variable's compose default, swap_terminal/network_target.py's CHAIN_PORTS for
       the test ports, /proc/net/tcp* for which of those ports are listening, and
       `dfx canister id` / `dfx identity get-principal` inside the icp-replica compose
       service
Writes: .env, mode 600, through a temp file in the same directory and a rename. Every
       line it does not own is carried through byte for byte. No .bak, ever (rule 2).
Can move funds: NO. It writes configuration and never calls an RPC that sends. What it
       CONFIGURES can move funds: an RPC credential here is what lets the payout worker
       broadcast, which is the point of persisting it.
Mainnet-safe: the SCRIPT is. What it configures is decided by the values given to it.
       The defaults it offers are TESTNET/DEVNET only and it says so per value; a
       mainnet port typed in is the operator's decision and this cannot tell.

=============================================================================
WHY THIS EXISTS, AND IT IS THE THIRD TIME THE SAME THING HAS BROKEN
=============================================================================

Operator, 2026-10-11, looking at an ATM page where all six coins read NONE: "hey why is
it completely fucking broken?"

MEASURED on their host in the same minute:

    chain variables present in the web container   44
    BTC_RPC_PORT / USER / PASS                     EMPTY
    LTC_RPC_PORT / USER / PASS                     EMPTY
    GRC_RPC_PORT / USER / PASS                     EMPTY
    SOL_RPC_URL, SOL_DEPOSIT_ACCOUNT, SOL_HOT_WALLET   EMPTY
    XRP_RPC_URL, XRP_DEPOSIT_ACCOUNT               EMPTY
    ICP_LEDGER_CANISTER_ID, ICP_OWNER_PRINCIPAL,
      ICP_DFX_SERVICE                              EMPTY
    names in .env                                  COMPOSE_FILE, SWAP_DB_DIR,
                                                   GRIDCOIN_WALLET_PASSPHRASE

Forty-four variables present and empty, because docker-compose.web.yml passes each one
as `${VAR:-}` -- so an unset SHELL variable becomes an empty STRING in the container,
chains/registry.build_adapters() skips every chain that has no port or url, and the page
correctly reports six chains with no adapter. Nothing was broken. Nothing was
configured.

AND THE OPERATOR HAD ALREADY NAMED THIS DEFECT TWICE. "hey what the fuck? yeah, new
shell should not rat fuck the entire fucking machine. it should persist. what the fuck",
and then "i already fucking told you to make it presistent and you're asking me if i
want ephemeral". It was fixed for COMPOSE_FILE, for SWAP_DB_DIR and for
GRIDCOIN_WALLET_PASSPHRASE -- three variables out of forty-seven -- and the other
forty-four stayed in whatever shell happened to export them. A host restart, or
`docker compose up -d --force-recreate web` from a fresh terminal, loses all of them at
once. That is what happened.

=============================================================================
DISCOVER BEFORE ASKING, BECAUSE A PROMPT FOR FORTY VALUES IS NOT A FIX
=============================================================================

Most of these are already knowable from the running system, and asking for something the
machine can tell you is how a setup step gets skipped:

  the three RPC ports   probed. network_target.CHAIN_PORTS already holds each chain's
                        test ports and exists because three places needed the same
                        answer; this is the fourth, and it reads the table rather than
                        spelling 18443 again (rule 8).
  the ICP canister id   asked of the replica with `dfx canister id`, inside the
  and principal         icp-replica SERVICE -- never host dfx in icp/, which HANDOFF.md
                        section 1 records as the single most destructive mistake
                        available here: host dfx judged the container's root-owned
                        .dfx/local incohesive and tried to DELETE it, and only a
                        permission error stopped it taking the ledger with it.
  SOL and XRP endpoints the documented devnet/testnet URLs, offered as a default the
                        operator accepts or overrides.
  everything else       asked. Public keys are echoed because they are public; anything
                        .env.example marks `# SECRET` is read with no echo and never
                        printed, never logged and never put in argv.

WHAT IT WILL NOT DO, and these are refusals rather than gaps:

  read a daemon's conf  bitcoin.conf and gridcoinresearch.conf hold rpcpassword, and
                        CLAUDE.md's chain-safety rules say never move, copy or read back
                        a credential -- "if a task seems to need one, it is a proposal
                        for the operator, not a step to take". So the password is typed,
                        not harvested, even though harvesting would be more convenient.
  read a keypair file   ~/.config/solana/id.json is a private key. `solana address`
                        reads it to print the public one, and this does not run it. The
                        public key is asked for instead.
  guess a mainnet value every default it offers is a test network. It cannot tell a
                        mainnet port from a test one once typed, and says so rather than
                        implying it validated anything.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

from chains import credential_match  # noqa: E402 -- after the sys.path line above
from chains.daemon_conf import (  # noqa: E402 -- after the sys.path line above
    TOP_LEVEL,
    conf_sections,
    parse_daemon_conf,
)

# CHAIN_TEST_NETWORKS is the one per-chain allowlist of network names that are not
# mainnet, and conf_section_for_port() reads it so that no [main] block can ever be
# chosen as the source of a credential.
from chains.daemon_network import CHAIN_TEST_NETWORKS  # noqa: E402 -- after the sys.path line above
from config import database_path  # noqa: E402
from env_file import assignments_in  # noqa: E402 -- the PARSE only, never the loader
from network_target import (  # noqa: E402 -- as every root tool does
    CHAIN_PORTS,
    is_external_wallet,
)
from regtest.daemons import CHAIN_DEFAULTS  # noqa: E402 -- the one owner of each chain's datadir and conf name

ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

#: `.env.example` marks a credential with this on the line above the assignment, and
#: that marker is the ONLY thing that decides whether a value is read without echo.
#: Derived from the template rather than listed here, so a credential added there is
#: withheld here without anybody remembering to add it twice (rule 8).
#:
#: NAMED `NO_ECHO_MARKER` AND NOT `SECRET_MARKER`, which is ruff's S105 answered rather
#: than suppressed (rule 19): the rule flags a NAME that looks like it holds a
#: credential, and this holds a four-character marker that appears in a template in
#: version control. Renaming is the honest fix -- a `noqa` here would be a claim about
#: the value, and the next reader would have to re-establish it. tests/conftest.py's
#: RPC_FIXTURE_AUTH is the same move for the same rule.
NO_ECHO_MARKER = "# SECRET"

#: /proc/net/tcp's columns, up to the one this reads. `sl local_address rem_address st`
#: -- so state is field 3 and a line with fewer fields is a header or a truncated read.
#: Named for the same reason swap_terminal/loopback.py names _ROUTE_MIN_FIELDS: a bare 3
#: in a slice of somebody else's file format is a number the next reader has to go and
#: count.
_PROC_NET_MIN_FIELDS = 4

#: What /proc/net/tcp writes in the state column for LISTEN.
_PROC_NET_LISTEN = "0A"

#: How many listening test ports a chain must have for this to offer one without
#: guessing. Named because the bare `1` in that comparison is the entire policy: two
#: means two networks of one chain are up and picking either would be a guess about what
#: the operator means, so the value is asked for instead.
_EXACTLY_ONE = 1

#: Documented test endpoints, offered as defaults. TESTNET AND DEVNET ONLY -- a mainnet
#: URL typed over one of these is the operator's decision and this script cannot tell.
ENDPOINT_DEFAULTS = {
    "SOL_RPC_URL": ("https://api.devnet.solana.com", "Solana DEVNET"),
    "XRP_RPC_URL": ("https://s.altnet.rippletest.net:51234", "XRP TESTNET"),
}

#: The compose SERVICE name the ICP adapter execs dfx in. NOT the container name --
#: `docker compose exec` rejects `swap-icp-replica`, and that distinction is written at
#: the call site in chains/icp.py because it was got wrong once.
ICP_SERVICE = "icp-replica"

#: What this script asks the replica for, and the dfx command that answers it.
ICP_DISCOVERY = {
    "ICP_LEDGER_CANISTER_ID": ["dfx", "canister", "id", "icp_ledger_canister"],
    "ICP_OWNER_PRINCIPAL": ["dfx", "identity", "get-principal"],
}

#: WHERE EACH DAEMON'S OWN CONF LIVES, and these are the STANDARD Core datadirs --
#: deliberately NOT regtest.daemons.CHAIN_DEFAULTS' datadirs, which are the harness's
#: own `~/regtest/btc` and `~/regtest/ltc`. A reader who finds one of these tables must
#: know the other exists and answers a different question (rule 8): that one is "where
#: does the test harness put a daemon it started", this one is "where did the operator's
#: own daemon put its conf".
#:
#: Confirmed against the operator's host 2026-10-11: `~/.litecoin/litecoin.conf` and
#: `~/.GridcoinResearch/gridcoinresearch.conf` both exist with rpcuser and rpcpassword;
#: `~/.bitcoin/bitcoin.conf` does not exist and bitcoind is not running.
DAEMON_CONFS = {
    "BTC": ("~/.bitcoin/bitcoin.conf", "BTC_RPC_USER", "BTC_RPC_PASS"),
    "LTC": ("~/.litecoin/litecoin.conf", "LTC_RPC_USER", "LTC_RPC_PASS"),
    "GRC": ("~/.GridcoinResearch/gridcoinresearch.conf", "GRC_RPC_USER", "GRC_RPC_PASS"),
}

#: EVERY PLACE A CHAIN'S CONF MIGHT BE, and why conf_candidates() is a list rather than
#: the one entry in DAEMON_CONFS above.
#:
#: =========================================================================
#: I RESPELLED A PATH THAT regtest/daemons.CHAIN_DEFAULTS ALREADY OWNS, AND
#: THE TWO TABLES DISAGREE ABOUT BITCOIN
#: =========================================================================
#:
#: Measured 2026-10-11:
#:
#:     regtest/daemons.CHAIN_DEFAULTS    BTC  ~/regtest/btc/bitcoin.conf
#:     configure_env.DAEMON_CONFS        BTC  ~/.bitcoin/bitcoin.conf
#:
#: chains/daemon_conf.py imports CHAIN_DEFAULTS for exactly this and says why in its own
#: header: it "is the one place that owns each chain's datadir and conf name. Respelling
#: them here would be two sources for the fact that moves whenever the harness is pointed
#: somewhere else (rule 8)." I then respelled them here, on a different path.
#:
#: THE COST WAS A CHAIN THE OPERATOR COULD NOT CONFIGURE. Their BTC has no conf at
#: ~/.bitcoin and nothing listening on 18332 or 18443, so --read-daemon-confs reported
#: "no conf" and asked them for an rpcuser and an rpcpassword -- while their bitcoind's
#: real conf sits at ~/regtest/btc/bitcoin.conf, a path CHAIN_DEFAULTS already owned and
#: this tool did not look at. LTC worked only by luck: their litecoind is a real testnet
#: node at ~/.litecoin, which is the path that happened to be spelled here.
#:
#: AND THE SENTENCE THAT USED TO BE HERE WAS WRONG ABOUT WHO WRITES THAT FILE. It said
#: "The moment they bring up this repository's OWN regtest harness, bitcoind writes
#: ~/regtest/btc/bitcoin.conf with both". NEITHER HALF IS TRUE, and it is the one claim
#: on this page an operator would act on. bitcoind does not write a conf at all, and the
#: harness deliberately refuses to: regtest/daemons.start_daemon() raises
#: RegtestSetupError naming the file and the four settings it needs when the conf is
#: absent, and regtest_htlc_verify.py's header says why -- "writing an rpcpassword into
#: a file on the operator's behalf is not this harness's business". The conf is the
#: operator's to create, once, by hand; this tool then reads it and never asks for the
#: password. tests/test_btc_is_reachable.py pins the refusal so the sentence cannot go
#: stale again by somebody teaching the harness to write one.
#:
#: WHICH CANDIDATE IS PREFERRED IS DECIDED BY THE PROBED PORT, not by an order chosen
#: here. CHAIN_DEFAULTS' ports are the regtest ones (BTC 18443, LTC 19443), so a daemon
#: answering on a regtest port is the harness and its conf is the harness's; a daemon on a
#: testnet port is the operator's own and its conf is the conventional one. That is the
#: same discriminator CONF_SECTION_FOR_PORT already uses one line down, and it is why this
#: needs no new knowledge: the port was measured before the file was opened.
def conventional_conf(chain: str) -> str:
    """DAEMON_CONFS' path for `chain`, read AT CALL TIME and not precomputed.

    THIS WAS A MODULE-LEVEL DICT COMPREHENSION FOR ONE EDIT and broke six tests, which is
    the small version of the thing it exists to prevent. The fixtures redirect a conf by
    monkeypatching DAEMON_CONFS; a constant derived from it at IMPORT time had already
    taken its copy, so the patch redirected nothing and the tool read the operator's real
    ~/.litecoin while the test watched a file in tmp_path.

    A derived CONSTANT is a second source for a fact that can change after import -- rule
    8 in the same commit whose whole subject is rule 8 -- and a function reading the one
    table cannot diverge from it.
    """
    entry = DAEMON_CONFS.get(chain)
    return entry[0] if entry else ""


def conf_candidates(chain: str, port: int, overrides: Mapping[str, Path] | None = None) -> list[Path]:
    """Where to look for `chain`'s conf, best first, given the port it answers on.

    AN OVERRIDE IS THE WHOLE LIST AND NOT THE HEAD OF IT. If the operator has said which
    file the daemon read, the tables have nothing to add: a fallback behind a stated path
    would quietly read a different wallet's conf when the stated one is missing, and
    "quietly read a different wallet's conf" is the failure this argument exists to end.
    Missing means missing, and the caller says so by name.

    WHY IT EXISTS, measured 2026-10-12 on the operator's host. Their Gridcoin desk daemon
    runs with `-datadir=$HOME/.GridcoinResearch-desk` and keeps its testnet credentials in
    that datadir's `testnet/` subdirectory. DAEMON_CONFS knows ONE Gridcoin path,
    `~/.GridcoinResearch/gridcoinresearch.conf`, which on this host is the MAINNET staking
    wallet -- a different wallet with different credentials. So --read-daemon-confs could
    only ever have read the wrong file for GRC, and reading the mainnet conf is the cause
    this project has already measured for this exact 401 (regtest/daemons.
    GRC_CREDENTIALS_ARE_PER_NETWORK, 2026-09-29).

    A TABLE CANNOT FIX THAT, which is why this is an argument rather than a fourth entry
    in DAEMON_CONFS. The datadir is chosen by whoever typed the launch command, three
    Gridcoin wallets run on this host (OPEN_FINDINGS finding 2), and the only reliable
    source for which one a daemon read is its own startup log -- it prints `Using data
    directory <datadir>/testnet` every run. credential_check.py takes the path as a
    required argument for the same reason and says so in its header.

    THE HARNESS PATH COMES FROM CHAIN_DEFAULTS and is never spelled here, which is the
    whole point of the function. GRC has no CHAIN_DEFAULTS entry -- the harness does not
    run Gridcoin -- so it gets its conventional path and nothing else, and that is
    correct rather than a gap.

    DEDUPED WITH ORDER PRESERVED, so a chain whose two tables agree is tried once and the
    output does not report looking in the same file twice (rule 14: output that
    contradicts itself about what it did is a defect in the output).
    """
    stated = (overrides or {}).get(chain)
    if stated is not None:
        return [stated]
    harness = CHAIN_DEFAULTS.get(chain)
    harness_path = (Path(harness["datadir"]).expanduser() / harness["conf_name"]) if harness else None
    conventional = conventional_conf(chain)
    conventional_path = Path(conventional).expanduser() if conventional else None

    regtest = bool(harness) and port == harness.get("port")
    ordered = ([harness_path, conventional_path] if regtest
               else [conventional_path, harness_path])
    seen: list[Path] = []
    for candidate in ordered:
        if candidate is not None and candidate not in seen:
            seen.append(candidate)
    return seen

#: Which conf SECTION a given port's network CONVENTIONALLY uses. Bitcoin Core names
#: them `[main]`, `[test]` and `[regtest]`, and Core 28 adds `[testnet4]`; credentials
#: are conventionally at the TOP level while the port sits in a section -- so the
#: section name decides which rpcport a conf reports, and reading the wrong one reports
#: a port the daemon is not on.
#:
#: DERIVED FROM THE PROBED PORT rather than from a fixed per-chain default, which is the
#: one place this diverges from chains/daemon_conf.CONF_FALLBACK_NETWORK. That table
#: hardcodes regtest for BTC and LTC; the operator's litecoind answers on 19332, which is
#: TESTNET, so a fixed "regtest" would read the wrong section of their real conf.
#:
#: IT IS A CONVENTION AND NOT THE ANSWER, AND THAT DISTINCTION IS WHY BTC DID NOT WORK.
#: It is read by conf_section_for_port() below, which prefers what the conf itself says
#: -- the measurement, and what this table alone did to Bitcoin, are written up there.
CONF_SECTION_FOR_PORT = {18332: "test", 18443: "regtest", 19332: "test", 19443: "regtest"}


def conf_section_for_port(chain: str, text: str, port: int) -> tuple[str, str]:
    """Which [section] of this conf configures the daemon on `port`, and one line saying why.

    =========================================================================
    A PORT DOES NOT NAME A NETWORK, AND BELIEVING IT DID IS WHY BTC WAS THE ONE
    CHAIN THIS TOOL COULD NOT CONFIGURE
    =========================================================================

    CONF_SECTION_FOR_PORT above maps 18443 to `regtest`, which is Bitcoin's
    conventional regtest port and is correct as a convention. The operator's daemon
    does not follow it. OPEN_FINDINGS.md finding 1 records their 2026-10-10
    migration off regtest -- "our grc, ltc, and btc daemons should have peers and
    not be regtest anyone and just full testnet now" -- and two measured
    consequences of it:

        BTC needs `[testnet4]`, LTC needs `[test]`.   Core v28.1.0 has two testnets
                                                      with different conf section
                                                      names; Litecoin v0.21.4 has one.
        The RPC ports did NOT have to change.         18443 and 19443 are in
                                                      network_target.CHAIN_PORTS' TEST
                                                      sets, not just the regtest ones.

    So their bitcoind is on TESTNET4 at rpcport 18443, and the table sends this tool
    to `[regtest]`. Measured 2026-10-11 by running the real parser over a conf in
    that shape:

        network='regtest'   -> the [regtest] block's rpcuser and rpcpassword
        network='testnet4'  -> the [testnet4] block's, which is the daemon's
        network=''          -> {} when the credentials are under the header

    Both outcomes are failures and they fail differently. A conf migrated from
    regtest with its old `[regtest]` block still in it hands over credentials
    BITCOIND ITSELF IGNORES -- those get written to .env and every BTC call 401s,
    which is indistinguishable from a daemon that is down. A conf whose stale block
    was replaced rather than kept hands over nothing, and this tool reports
    "has no rpcuser, rpcpassword" about a file that has both.

    =========================================================================
    SO THE CONF IS ASKED, AND THE TABLE IS THE FALLBACK
    =========================================================================

    Nothing new is probed and no daemon is contacted -- this function is pure, over
    text that was already read. The order is strictly most-evidence-first:

      1. the section that DECLARES rpcport=<port>, when exactly one does. The
         strongest reading available: a section naming this port is the section
         that put the daemon on it.
      2. NOTHING, when two or more declare it. Two blocks claiming one port is a
         conf no reading can settle -- only the daemon knows which network it came
         up on, and this function holds no socket. It is the case a conf migrated
         off regtest produces when the old block was left in beside the new one,
         and it is the case where the convention in step 3 is actively wrong: it
         would hand over the credentials from the block bitcoind IGNORES. So the
         top level is read, the sentence names both blocks, and the operator
         removes one or exports the two variables themselves.
      3. the conventional section for the port, when the conf actually HAS that
         section and no section declared the port. Unchanged behavior for a
         conventional deployment, which is what every LTC and GRC read so far has
         been.
      4. the conf's only network section, when it has exactly one. This is the
         operator's BTC: one `[testnet4]` block, nothing else, port declared at the
         top level or in it.
      5. the top level alone, saying so, for a conf with no network section at all.

    A MAINNET SECTION IS NEVER CHOSEN, and the allowlist is not invented here:
    chains/daemon_network.CHAIN_TEST_NETWORKS is the existing per-chain table of
    "what this chain CALLS a network that is safe to lose coins on", and its own
    comment says an allowlist is used precisely so that "a mainnet answer" cannot
    slip through (rule 8 -- a second copy of that set is the bug with a delay on
    it). It is per chain on purpose: `testnet4` is a BTC section name and
    deliberately not an LTC one, because Litecoin Core v0.21.4 predates testnet4
    entirely. discover_ports() only ever probes `test_ports`, so a mainnet section
    could never be the right answer to this question anyway.

    Returns (section, why). The section goes to parse_daemon_conf(); the sentence
    goes on screen, because an operator who gets the wrong credentials has to be
    able to see WHICH block of their own file was read (rule 14).
    """
    sections = conf_sections(text)
    allowed = CHAIN_TEST_NETWORKS.get(chain, frozenset())
    # TOP_LEVEL is in `sections` by construction and is not a network, so it is
    # excluded from every count below -- otherwise "the conf's only network section"
    # would be true of a conf with no sections at all.
    network_sections = sorted(name for name in sections if name != TOP_LEVEL)
    usable = [name for name in network_sections if name in allowed]

    declaring = [name for name in usable if sections[name].get("rpcport", "") == str(port)]
    if len(declaring) == _EXACTLY_ONE:
        return declaring[0], f"[{declaring[0]}] is the only section declaring rpcport={port}"
    if declaring:
        return "", _ambiguous_sections(chain, declaring, port)

    conventional = CONF_SECTION_FOR_PORT.get(port, "")
    if conventional and conventional in usable:
        return conventional, (
            f"[{conventional}] is the conventional network for port {port}, this conf has it, and no "
            f"section declares that port"
        )

    if len(usable) == _EXACTLY_ONE:
        return usable[0], (
            f"[{usable[0]}] is the only {chain} network section in this conf; port {port} does not "
            f"name a network on its own"
        )

    if not usable:
        return "", (
            f"this conf has no {chain} network section -- its sections are "
            f"{_named_sections(network_sections)} -- so only its top level was read"
        )
    return "", _ambiguous_sections(chain, usable, port)


def _ambiguous_sections(chain: str, names: list[str], port: int) -> str:
    """The sentence for a conf whose blocks cannot be told apart from the file alone.

    ONE SPELLING FOR BOTH AMBIGUOUS BRANCHES -- two sections declaring the same
    rpcport, and several sections declaring none -- because they have the same cause
    and the same remedy, and two formatters is two chances to leave the remedy out
    of one of them (rule 8). The remedy is in the sentence rather than in a docstring
    the operator will never see (rule 14).
    """
    return (
        f"this conf has {len(names)} {chain} network sections ({_named_sections(names)}) and the file "
        f"alone does not settle which one put the daemon on port {port}, so ONLY its top level was read "
        f"-- a credential inside one of those blocks was NOT taken. Picking the wrong block writes a "
        f"credential the daemon ignores, and every call then 401s, which reads exactly like a daemon "
        f"that is down. Remove the stale block, or set the two variables by hand"
    )


def _named_sections(names: list[str]) -> str:
    """`[a], [b]` for a listing, or the (none) sentence. Never a blank gap (rule 14)."""
    return ", ".join(f"[{name}]" for name in names) if names else "(none)"

#: GRC IS REFUSED BY DEFAULT AND NEEDS ITS OWN FLAG, and the reason is quoted from
#: chains/daemon_conf.CONF_FALLBACK_NETWORK, which excludes Gridcoin entirely:
#:
#:     "~/.GridcoinResearch is the DEFAULT datadir, and on the operator's host that is a
#:      live staking wallet holding real coins [...] Picking a connection out of the
#:      default datadir's file is how a reader -- or worse, a driver that sends -- ends
#:      up pointed at the staking wallet."
#:
#: THIS TOOL'S CASE IS NARROWER THAN THE ONE THAT TABLE REFUSES, which is why a separate
#: opt-in is defensible rather than an override: that table picks a whole CONNECTION,
#: port included, out of a file nothing told it to read. Here GRC_RPC_PORT has already been established
#: by probing -- 25715 on the operator's host, which network_target.classify() calls TEST
#: -- so only the credentials come from the conf and the port decides which daemon they
#: reach. chains/daemon_network.test_network_verdict() is still the backstop that refuses
#: a mainnet answer before anything is asked of a wallet.
#:
#: It is a SEPARATE flag and not part of --read-daemon-confs because the hazard is
#: different in kind, and a single flag would let an operator accept it without reading
#: this.
GRIDCOIN_CONF_HAZARD = (
    "~/.GridcoinResearch/gridcoinresearch.conf belongs to the wallet running on the DEFAULT "
    "datadir, and a Gridcoin mainnet wallet is typically a live staking wallet holding real "
    "coins. chains/daemon_conf.py excludes Gridcoin from conf resolution for exactly that "
    "reason. Reading only the CREDENTIALS is narrower -- GRC_RPC_PORT is established by "
    "probing, so the port decides which daemon they reach -- but it is still your decision, "
    "not this tool's. Pass --read-gridcoin-conf to accept it. AND IT IS PROBABLY NOT THE FILE "
    "YOU WANT: this host runs three Gridcoin wallets, a testnet daemon keeps its credentials "
    "in `<its own datadir>/testnet/gridcoinresearch.conf`, and those differ from the file "
    "beside them. Pass --conf GRC=<that path> as well; the daemon prints `Using data directory "
    "<datadir>/testnet` in its startup log."
)

#: THIS SENTENCE SAID THE CONVENTIONAL FILE WAS "shared by MAINNET and testnet" until
#: 2026-10-12, and the correction is the reason --conf exists.
#:
#: What is measured: the operator's desk daemon runs with
#: `-datadir=$HOME/.GridcoinResearch-desk`, BOTH `<datadir>/gridcoinresearch.conf` and
#: `<datadir>/testnet/gridcoinresearch.conf` carry rpcuser and rpcpassword (grep -c
#: returned 2 and 2 on 2026-10-12), and regtest/daemons.GRC_CREDENTIALS_ARE_PER_NETWORK
#: records from a measured 401 on 2026-09-29 that the two files hold DIFFERENT values.
#:
#: So "shared" was the wrong word in the way that matters: it reads as "either file will
#: do", and the file this tool reaches for by default is a DIFFERENT WALLET'S -- not a
#: second view of the same one. An operator who accepted the hazard got credentials that
#: cannot authenticate against their testnet daemon, which is a 401 with a sentence
#: pointing away from the cause.
#:
#: WHAT IS NOT CLAIMED HERE, because it has not been measured from here: the order in
#: which gridcoinresearchd itself resolves those two files on a testnet run. That is why
#: the remedy is the daemon's own `Using data directory` line rather than a rule about
#: conf precedence -- rule 17, and the reason credential_check.py refuses to guess a path.

#: THE SHARED DEPOSIT ACCOUNT VARIABLE FOR EACH CHAIN THAT HAS ONE, and this table is
#: narrow for a reason that would cost a customer their money if it were widened.
#:
#: SOL AND XRP ONLY, because they are the only two chains where ONE account receives every
#: swap's deposit and a discriminator -- a memo, a destination tag -- says whose it is.
#: services/swap_service.TAG_ATTRIBUTED_ASSETS is frozenset({"XRP", "SOL"}) and this is the
#: same fact: for those two, any swap's `deposit_address` IS the configured account.
#:
#: NEVER BTC, LTC OR GRC. Each of their swaps gets its OWN freshly derived address, so a
#: `deposit_address` read off one of their rows is ONE CUSTOMER'S address. Writing it into
#: .env as a deposit account would point every future swap on that chain at a stranger's
#: address, and the deposits would be real, confirmed and unrecoverable. That is the single
#: most expensive mistake available in this file, which is why the table is an allowlist
#: rather than a loop over every asset.
#:
#: AND NOT ICP, which looks tag-attributed and is not: its subaccounts are derived per swap
#: from ICP_OWNER_PRINCIPAL plus an index, so there is no shared-account variable to set --
#: the principal, already discovered above, is what plays that role.
SHARED_DEPOSIT_ACCOUNTS = {"SOL": "SOL_DEPOSIT_ACCOUNT", "XRP": "XRP_DEPOSIT_ACCOUNT"}

#: Help text for a variable that CAN be discovered but was not, so the question is
#: answerable. Each names the command that would have discovered it, because an operator
#: looking at "ICP_LEDGER_CANISTER_ID >" with no help has no way to answer and a canister
#: id is not guessable -- every fresh replica issues different ones, which is why nothing
#: in this tree hardcodes one.
FALLBACK_HELP = {
    "ICP_LEDGER_CANISTER_ID": (
        "the ledger canister id. `python3 swap_stack.py status` prints it, or "
        "`docker compose exec -T icp-replica dfx canister id icp_ledger_canister`. The replica did "
        "not answer, so it could not be read here. Leave BLANK to leave ICP unconfigured."
    ),
    "ICP_OWNER_PRINCIPAL": (
        "the desk's own principal. `docker compose exec -T icp-replica dfx identity get-principal`. "
        "The replica did not answer. Leave BLANK to leave ICP unconfigured."
    ),
}

#: Variables with no default and no discovery, in the order an operator can answer them.
#: Each carries what it is and how to find it, because "SOL_DEPOSIT_ACCOUNT=" on its own
#: is a question nobody can answer from the prompt alone (rule 14).
ASKED = [
    ("BTC_RPC_USER", "the rpcuser from your bitcoind's conf"),
    ("BTC_RPC_PASS", "its rpcpassword"),
    ("LTC_RPC_USER", "the rpcuser from your litecoind's conf"),
    ("LTC_RPC_PASS", "its rpcpassword"),
    ("GRC_RPC_USER", "the rpcuser from your gridcoinresearch.conf"),
    ("GRC_RPC_PASS", "its rpcpassword"),
    ("SOL_DEPOSIT_ACCOUNT", "the PUBLIC key every SOL swap deposits into (one shared account + memo)"),
    ("SOL_HOT_WALLET", "the PUBLIC key SOL payouts are sent FROM"),
    ("XRP_DEPOSIT_ACCOUNT", "the classic address every XRP swap deposits into (one account + destination tag)"),
    # A PATH, NOT A CREDENTIAL, which is why this one arming variable is askable and the
    # other two are not. The file it names holds a signing key; this names the file.
    # docker-compose.web.armed-sol.yml mounts it read-only and nothing in this tree reads,
    # prints or copies its contents -- chains/solana_payout_keypair.py exists precisely so
    # the capability check never opens it.
    #
    # ADDED 2026-10-11 BECAUSE ITS ABSENCE MADE AN OPERATOR HAND-EDIT .env. `--arm sol`
    # refuses until this is set, and this tool would not set it, so the only path through
    # was editing by hand the one file that holds GRIDCOIN_WALLET_PASSPHRASE -- which is
    # the hand edit write_env() was written to make unnecessary. The cause was .env.example
    # marking it SECRET: that marker is what makes a prompt no-echo, so it both excluded
    # the variable and would have had the answer typed blind, where a mistyped path is
    # invisible until a payout fails at the send.
    ("SOL_PAYOUT_KEYPAIR_HOST_FILE",
     "the PATH to the keypair file that owns SOL_HOT_WALLET -- the path, not the key. "
     "Nothing here opens it; the overlay mounts it read-only"),
]


def say(line: str = "") -> None:
    print(line, flush=True)


def secret_names() -> set[str]:
    """Every variable `.env.example` marks `# SECRET`, read out of the template.

    THE TEMPLATE IS THE AUTHORITY so there is one list (rule 8). A second hardcoded set
    here would agree on the day it was written, and the drift would land on whether a
    password is echoed to a terminal -- which is the one place in this script where being
    wrong is unrecoverable, because scrollback cannot be unseen.
    """
    found: set[str] = set()
    marked = False
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith(NO_ECHO_MARKER):
            marked = True
            continue
        match = re.match(r"^#?\s*([A-Z_][A-Z0-9_]*)=", stripped)
        if match:
            if marked:
                found.add(match.group(1))
            marked = False
        elif stripped:
            marked = False
    return found


def existing_assignments() -> dict[str, str]:
    """The UNCOMMENTED assignments already in .env, name -> value, quotes STRIPPED.

    Read so that a second run does not re-ask what is already set, and so that nothing
    this script does not own is disturbed. Values are read and never printed.

    =========================================================================
    IT HAD ITS OWN PARSER AND THE TWO DISAGREED ABOUT QUOTING
    =========================================================================

    env_file.assignments_in() strips a matching pair of surrounding quotes, "because
    compose strips them"; this function did not. write_env() below writes every value
    SINGLE-QUOTED -- it has to, since that is the only dotenv encoding an arbitrary
    value survives -- so everything this tool has ever written came back out of here
    wrapped in apostrophes.

    Harmless while every caller only tested truthiness. Measured 2026-10-13, the moment
    one COMPARED: credentials_from_confs() asked whether .env's GRC_RPC_USER matches the
    conf's rpcuser, and `'same-user'` is not `same-user`, so a credential that agreed
    perfectly was reported as DIFFERENT. The "already matches" branch -- the single most
    useful thing to tell somebody chasing a 401 -- could never fire.

    Two call sites had already compensated with `.strip("'\"")` of their own, which is
    how a divergence like this stays invisible: each site looks correct, and the one that
    forgets is the one that is comparing.

    env_file.py's own comment set the condition for merging them: the two parsers "are
    kept separate rather than merged because configure_env is an operator-facing writer
    that must not import a module whose whole purpose is a side effect on os.environ --
    and because a shared parser would be a third thing to keep in step for two callers
    whose needs have not diverged in any way yet. If a third caller appears, that is the
    signal to merge them." The needs diverged instead, which is the stronger signal.

    `assignments_in` IS IMPORTED BY NAME, not the module, so `env_file.load_into_environ()`
    cannot be reached from here even by accident -- this file is registered in
    tests/test_env_preamble.MUST_NOT_LOAD and that register is asserted over this source.
    The parse is pure; the loader is the other function and stays out of reach.
    """
    if not ENV_FILE.exists():
        return {}
    return assignments_in(ENV_FILE.read_text())


def listening_ports() -> set[int]:
    """Every TCP port in LISTEN on this host, from /proc/net/tcp and tcp6.

    /proc AND NOT `ss -ltn`, for the reason swap_stack.py gives for the same read: it
    needs no binary that might not be installed and no root. State 0A is LISTEN; the
    local address is `HEX_IP:HEX_PORT` and only the port is taken, because a daemon bound
    to 127.0.0.1 and one bound to 0.0.0.0 are both reachable from the host.
    """
    found: set[int] = set()
    for name in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            lines = Path(name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if (len(fields) >= _PROC_NET_MIN_FIELDS and fields[3] == _PROC_NET_LISTEN
                    and ":" in fields[1]):
                found.add(int(fields[1].rsplit(":", 1)[1], 16))
    return found


def discover_ports() -> dict[str, tuple[str, str]]:
    """Which test port each Bitcoin-family chain is actually listening on.

    PROBED RATHER THAN ASKED, and the table comes from network_target.CHAIN_PORTS -- the
    one place that already answers "what do I set to reach this chain?", read by the
    workers' banner, the swap page's pair list and create_swap()'s refusal. This is the
    fourth reader and it spells no port number of its own (rule 8).

    MAINNET PORTS ARE NEVER OFFERED. ChainPorts carries the mainnet port in order to
    CLASSIFY a configured one, not to suggest it, and config.py's header records what
    suggesting it cost: the three defaults used to be mainnet, and
    refresh_wallet_inventory() polled the operator's live staking wallet on a loop. Only
    `test_ports` is considered here.

    MORE THAN ONE LISTENING TEST PORT IS NOT RESOLVED HERE. It means two networks of the
    same chain are up, and picking one would be a guess about which the operator means --
    so both are reported and the value is asked for instead.
    """
    live = listening_ports()
    found: dict[str, tuple[str, str]] = {}
    for asset, ports in CHAIN_PORTS.items():
        listening = sorted(ports.test_ports & live)
        # AN EXTERNAL WALLET IS NEVER A CANDIDATE, and this is the mechanism that pointed
        # the terminal at one. Measured 2026-10-13: LTC's test_ports are {19332, 19443},
        # the desk's litecoind was down, the operator's litecoin-qt was up on 19332 --
        # Litecoin's DEFAULT testnet RPC port -- so exactly one test port was listening
        # and this wrote it into .env as LTC_RPC_PORT. `off` then stopped "the daemon on
        # the configured port" and closed their wallet, and every surface spent a day
        # telling them to recover it.
        #
        # SAID, NOT SILENTLY DROPPED (rule 13/14). A chain that goes unconfigured because
        # the only thing listening was somebody else's wallet is the single most useful
        # line in this block, and a quiet skip would read as "nothing is running".
        external = [port for port in listening if is_external_wallet(port)]
        for port in external:
            say(f"    {asset}: {port} is LISTENing and is NOT this stack's daemon -- "
                f"{is_external_wallet(port)[:120]}... NOT OFFERED.")
        candidates = [port for port in listening if not is_external_wallet(port)]
        if len(candidates) == _EXACTLY_ONE:
            # ports.port_variable AND ports.test_hint -- THE FIELD NAMES, READ OFF THE
            # CLASS. The first version of this line said `ports.variable` and
            # `ports.hint`, which are not fields of ChainPorts, and the operator got
            #
            #     AttributeError: 'ChainPorts' object has no attribute 'hint'
            #
            # on the first real run. I had read the TABLE -- four positional arguments
            # per entry -- and inferred the names from their positions instead of
            # reading the NamedTuple that defines them. That is rule 17 exactly: a
            # reason to believe written in the same voice as having checked.
            found[ports.port_variable] = (
                str(candidates[0]),
                f"{asset} answering on {candidates[0]} ({ports.test_hint})",
            )
    return found


def discover_icp() -> dict[str, tuple[str, str]]:
    """The ledger canister id and the desk principal, asked of the running replica.

    INSIDE THE COMPOSE SERVICE, NEVER HOST dfx. HANDOFF.md section 1 records host dfx run
    in icp/ judging the container's root-owned .dfx/local incohesive and attempting to
    DELETE it -- the local replica's state, including the ledger holding the desk's
    balance. Only a permission error stopped it. `docker compose exec` is the correct
    transport and the SERVICE name is `icp-replica`; the CONTAINER name is
    `swap-icp-replica` and exec rejects it.

    A CANISTER ID IS NEVER HARDCODED. Every fresh replica issues different ids, so an id
    in a file is somebody else's -- which is why swap_stack.py asks for them too and says
    so on the line that prints them.

    FAILURE IS NOT AN ERROR HERE. A replica that is not running simply cannot answer, and
    the value is asked for instead. Reported either way, because "could not reach the
    replica" and "the replica has no such canister" send an operator to different places.
    """
    found: dict[str, tuple[str, str]] = {}
    # RESOLVED RATHER THAN TRUSTED TO PATH, which is ruff's S607 answered rather than
    # suppressed: a bare "docker" is whatever PATH finds, and this script runs as the
    # operator with their environment. shutil.which() is also the honest way to report
    # "docker is not installed" as its own answer instead of as a FileNotFoundError from
    # inside a discovery step.
    docker = shutil.which("docker")
    if docker is None:
        say("    docker is not on PATH, so the ICP values cannot be discovered here")
        return found
    for variable, command in ICP_DISCOVERY.items():
        result = subprocess.run(  # noqa: S603 -- fixed argv from this file's own constants, no shell, absolute binary
            [docker, "compose", "exec", "-T", ICP_SERVICE, *command],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False, timeout=60,
        )
        value = result.stdout.strip()
        if result.returncode == 0 and value:
            found[variable] = (value, f"asked of the {ICP_SERVICE} service with `{' '.join(command)}`")
        else:
            reason = (result.stderr or result.stdout).strip().splitlines()
            say(f"    could not discover {variable}: {reason[-1][:140] if reason else 'no output'}")
    return found


def ask(variable: str, what: str, *, secret: bool, default: str = "") -> str:
    """One value from the terminal. A secret is read with NO echo and never printed back.

    /dev/tty RATHER THAN stdin, so this keeps working from a pipe and so a redirected
    stdin cannot supply a credential without the prompt being seen. ST_ANSWERS is the
    test path and is the only way a value arrives without a terminal.
    """
    scripted = os.environ.get("ST_ANSWERS")
    if scripted is not None:
        answers = dict(pair.split("=", 1) for pair in scripted.split(";") if "=" in pair)
        return answers.get(variable, default)

    suffix = f" [{default}]" if default and not secret else ""
    prompt = f"  {variable}{suffix}\n    {what}\n    > "
    if secret:
        value = getpass.getpass(f"  {variable} (NOT echoed)\n    {what}\n    > ")
    else:
        with Path("/dev/tty").open() as tty:
            print(prompt, end="", flush=True)
            value = tty.readline().strip()
    return value or default


def write_env(values: dict[str, str]) -> None:
    """Replace or append each name in .env, carrying every other line through unchanged.

    ATOMIC AND MODE 600, through a temp file in the same directory and a rename, with
    umask set BEFORE the file exists -- a chmod afterwards leaves a window in which a
    file holding RPC passwords is world-readable, and on a key-holding system that window
    is the whole exposure.

    NO .bak. CLAUDE.md rule 2: the backup habit is what put a live
    GRIDCOIN_RPC_PASSWORD on GitHub via .env.bak.2026-03-28_185451. There is one file at
    the end of this.

    SINGLE-QUOTED, because that is the only dotenv encoding an arbitrary value survives
    unchanged -- unquoted loses a trailing `#` to a comment, double-quoted takes `$`, `\\`
    and `"` as syntax. A value containing a single quote is refused by the caller for the
    same reason set_grc_passphrase.sh refuses one: there is no escape for it inside single
    quotes, and a credential stored mangled does not fail here, it fails later as an
    authentication error nobody connects to this step.
    """
    keep = []
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            match = re.match(r"^([A-Z_][A-Z0-9_]*)=", line.strip())
            if match and match.group(1) in values:
                continue
            keep.append(line)

    previous = os.umask(stat.S_IRWXG | stat.S_IRWXO)
    try:
        # mkstemp + a context manager rather than NamedTemporaryFile(delete=False),
        # which is ruff's SIM115 answered rather than suppressed. mkstemp also creates
        # the file 0600 ITSELF, before anything is written to it -- so there is no
        # instant at which a file about to hold RPC passwords is readable by anyone else,
        # which is the property the umask above is belt to.
        handle, temp_name = tempfile.mkstemp(dir=str(REPO_ROOT), prefix=".env.new.")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            for line in keep:
                out.write(line + "\n")
            for name in sorted(values):
                out.write(f"{name}='{values[name]}'\n")
        Path(temp_name).replace(ENV_FILE)
        ENV_FILE.chmod(stat.S_IRUSR | stat.S_IWUSR)
    finally:
        os.umask(previous)


class AccountRow(NamedTuple):
    """One candidate shared account, as the GROUP BY returns it.

    `credited` IS THE COLUMN THAT MATTERS and is why this is a NamedTuple rather than a
    bare tuple: choose_shared_account() reads three of these fields in four branches, and
    rows[1][2] in a decision about where customer deposits go is a line nobody can review.
    """

    address: str
    swaps: int
    credited: int
    newest: str


def choose_shared_account(asset: str, rows: list[AccountRow]) -> tuple[str, str]:
    """Which of these accounts to offer, and why -- or ("", why not).

    =========================================================================
    THE FIRST REAL RUN REPOINTED, AND THE OLD RULE RESOLVED IT SILENTLY
    =========================================================================

    2026-10-11, on the operator's host, first run after the database read landed:

        XRP_DEPOSIT_ACCOUNT  rnjG8n16JinjqkzZj5Jmw6NDMBMzhhNbVv
          <- the deposit address on XRP's 3 most recent swap(s) ... ; 2 DISTINCT accounts
             appear on XRP swaps, so this one has been repointed -- the newest is taken
             and the others are history

    SOL agreed with an independent measurement (tests/valid_addresses.py:249, recorded
    from their host on 2026-10-01, the same 44 characters). XRP had two candidates and
    the tool picked one on recency alone, named it, and moved on.

    RECENCY IS THE RIGHT SIGNAL FOR "WHICH IS CURRENT" and it is not evidence that the
    account WORKS. A swap's deposit_address is written from the configured variable at
    creation time, so a swap created while that variable held a wrong value carries the
    wrong value -- and this configuration has been rewritten repeatedly all week. The
    newest row can be a misconfiguration that was never exercised.

    `credited_at` IS THE CORROBORATION. A swap whose credited_at is set is one where a
    deposit actually arrived at that account and this system saw it and attributed it. No
    reading of the code can establish that; only the chain and the database together can,
    and they already have.

    SO THE TABLE, and the only case that comes back to the operator is the ambiguous one
    (rule 16 -- a deposit address is fund movement):

      one candidate              offer it. Say whether anything has ever been credited to
                                 it, because "the only account on record" and "the account
                                 that has received money" are different claims.
      several, newest IS         offer the newest. Recency says current, the credited count
      credited                   says it works, and they agree.
      several, newest is NOT     REFUSE. Offer nothing, and say which addresses and what
      credited, an older one IS  the evidence is. The newest may be a typo that was never
                                 tested; the older one is proven but may be retired. That
                                 is not derivable from the rows, and guessing it writes a
                                 customer-facing deposit address. It falls through to the
                                 question, where the operator answers it.
      several, NONE credited     offer the newest and say plainly that nothing corroborates
                                 it -- the chain has never completed a deposit under any of
                                 these. Still the best available answer, and labeled as a
                                 guess rather than a finding (rule 17).

    THE REFUSAL IS THE WHOLE POINT. The old behavior had no refusal: every shape of
    disagreement resolved to the newest row. The one shape that cannot be resolved from
    the data now says so.

    AND THE ADDRESS IS DELIBERATELY NOT RE-VALIDATED HERE, which is recorded because the
    next reader will reach for it. modules/address_authority.check_receive_address() is
    the right check for a deposit address -- it refuses a mainnet address while this
    process is on testnet, and it refuses a wrong-chain one, which is not hypothetical:
    on 2026-10-01 a Solana account reached XRP's validator and `POST /api/swaps` returned
    400 with "CUBnQ5... is not a valid XRPL classic address", recorded at
    services/xrp_tag_service.py:255.

    IT IS ALREADY ENFORCED UPSTREAM OF EVERY ROW THIS READS. services/swap_service's
    create_swap() runs that check before the INSERT and writes nothing when it fails --
    tests/test_address_authority.py asserts COUNT(*) == 0 after the refusal. So an
    address that is invalid for its chain cannot BE on a swaps row, and a check here
    would be a second implementation of a rule that already has an owner (rule 8). The
    one case it would add is a row created while this host was pointed at a different
    network, and this tree refuses mainnet at every entry point rather than relying on a
    configuration read.
    """
    if not rows:
        # THE CALLER ALREADY GUARDS THIS (`if not rows: continue`), and it is here anyway
        # because this function is public and a second caller would reach rows[0] on an
        # empty list. A crash inside a configuration tool, on a machine whose stack is
        # already broken, is the one failure mode this whole file is written against.
        # Tested directly rather than through the caller, since the caller cannot produce it.
        return "", "no deposit address on any swap of that asset"
    top = rows[0]
    proven = f"{top.credited} of its {top.swaps} swap(s) were credited, so deposits have arrived there"
    unproven = (f"NOTHING has ever been credited to it -- {top.swaps} swap(s) on record and 0 "
                f"credited deposits, so nothing here corroborates the address")
    if len(rows) == 1:
        return top.address, (f"the only deposit address on {asset} swaps, which IS the shared account "
                             f"for a tag-attributed chain; " + (proven if top.credited else unproven))

    credited_elsewhere = [row for row in rows[1:] if row.credited]
    if not top.credited and credited_elsewhere:
        other = credited_elsewhere[0]
        return "", (
            f"{asset} has {len(rows)} DISTINCT deposit accounts on its swaps and this cannot be "
            f"resolved from them, so NOTHING is offered and the question is asked instead. The "
            f"NEWEST is {top.address} ({top.swaps} swap(s), 0 credited -- it may be a value that "
            f"was configured and never worked). An OLDER one, {other.address}, has {other.credited} "
            f"credited deposit(s), so that account demonstrably receives and is watched -- but it "
            f"may be retired. Which is live is yours to say; a deposit address is where customer "
            f"money goes."
        )

    guess = "" if top.credited else " -- TAKEN ON RECENCY ALONE, as a guess rather than a finding"
    return top.address, (
        f"the deposit address on {asset}'s {top.swaps} most recent swap(s), which IS the shared "
        f"account for a tag-attributed chain; {len(rows)} DISTINCT accounts appear on {asset} swaps, "
        f"so it has been repointed and the newest is taken{guess}; "
        + (proven if top.credited else unproven)
    )


def accounts_from_the_database(already: dict) -> dict[str, tuple[str, str]]:
    """The shared SOL and XRP deposit accounts, read off swaps that already ran.

    =========================================================================
    "i don't know any of that fucking information off hand" -- 2026-10-11
    =========================================================================

    They did not need to. Their database holds 52 swaps, and on a tag-attributed chain
    every swap's `deposit_address` IS the configured shared account -- that is what makes
    the chain tag-attributed. So the value they were being asked to remember is recorded,
    by this system, on rows it wrote itself.

    THE DATABASE IS OPENED READ-ONLY, through the mode=ro URI, and that is a guard rather
    than a convention: sqlite refuses a write on that handle, so no bug in this
    configuration tool can alter the authority it is reading. absorb_db.open_both() uses
    the same URI for the same stated reason.

    IT GOES THROUGH config.database_path(), so it honors the precedence every other process
    does -- SWAP_DB_PATH exported, then .env's SWAP_DB_PATH, then .env's SWAP_DB_DIR. A
    second derivation here is what left the host tools pointed at a moved database for an
    hour this morning (rule 11).

    ONLY SOL AND XRP. See SHARED_DEPOSIT_ACCOUNTS: on BTC, LTC and GRC each swap gets its
    OWN derived address, so a `deposit_address` from one of those rows is one customer's
    address, and writing it into .env would point every future swap on that chain at a
    stranger. The deposits would be real, confirmed and unrecoverable.

    MOST RECENT FIRST, because an account can be repointed and the newest row is the one
    the operator most recently used. If older rows disagree the newest still wins, and the
    reason line says how many distinct accounts were seen so a repoint is visible rather
    than silently resolved.

    NEVER RAISES. A missing database, an unreadable one, or one with no swaps on a chain
    are all ordinary states -- the value is simply asked for instead, and the line says
    which of those it was (rule 14).
    """
    found: dict[str, tuple[str, str]] = {}
    try:
        path = Path(database_path())
    except Exception as error:  # noqa: BLE001 -- config derivation; reported and then asked for instead
        say(f"    the database path could not be derived ({type(error).__name__}), so no account was read")
        return found
    if not path.is_file():
        say(f"    no database at {path}, so no deposit account could be read from one")
        return found

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as error:
        say(f"    {path} could not be opened read-only ({error}), so no account was read")
        return found
    try:
        for asset, variable in sorted(SHARED_DEPOSIT_ACCOUNTS.items()):
            if already.get(variable):
                continue
            try:
                # THE RANKING IS IN SQL (rule 20): it is an aggregate and an ORDER BY over
                # rows already in this database, and a Python sort over the same rows would
                # be the same logic somewhere only its author can inspect. The CASE counts
                # credited swaps per account, which is choose_shared_account()'s evidence.
                rows = [AccountRow(*row) for row in connection.execute(
                    "SELECT deposit_address, COUNT(*) AS swaps,"
                    " SUM(CASE WHEN credited_at IS NOT NULL AND credited_at != '' THEN 1 ELSE 0 END)"
                    "   AS credited,"
                    " MAX(created_at) AS newest"
                    " FROM swaps WHERE from_asset = ?"
                    " AND deposit_address IS NOT NULL AND deposit_address != ''"
                    " GROUP BY deposit_address ORDER BY newest DESC",
                    (asset,),
                ).fetchall()]
            except sqlite3.Error as error:
                # A database created before the `swaps` table existed, or one never
                # initialized. Named rather than broad, and reported rather than raised.
                say(f"    {asset}: {path} could not be queried ({error})")
                continue
            if not rows:
                say(f"    {asset}: no swap in {path.name} carries a deposit address, so there is "
                    f"nothing to read -- the account has to be given")
                continue
            address, why = choose_shared_account(asset, rows)
            if not address:
                # A REFUSAL IS A RESULT AND IS SAID IN FULL (rule 14). It is not merged into
                # `found`, so the variable falls through to the question list exactly as if
                # the database held nothing -- which is the honest state: it holds two
                # answers and no way to choose between them.
                say(f"    {asset}: {why}")
                continue
            found[variable] = (str(address), f"{why} (read from {path.name})")
    finally:
        connection.close()
    return found


def inert_read_flags(args) -> str:
    """Which conf-reading flags were passed WITHOUT --read-daemon-confs, as prose, or "".

    BOTH FLAGS ARE INERT WITHOUT THE READ, and saying which is the difference between a
    run that did nothing and a run that REPORTS it did nothing -- rule 13: "skipped" and
    "success" in the same output is a defect in the output. --conf is named here and not
    only --read-gridcoin-conf, because a stated path that was never opened is exactly the
    state an operator would read as "it used my file."

    A FUNCTION BECAUSE IT IS A DECISION. It went inline first and took main() past
    ruff's PLR0915, which rule 12 reads as orchestration having swallowed one: "the fix
    is to extract the decision so it can be called with seeded inputs, not to raise the
    ceiling." Seeded inputs is also the only way the two-flag wording gets asserted
    without a conf file anywhere.
    """
    if args.read_daemon_confs:
        return ""
    return " and ".join(
        part for part in (
            "--read-gridcoin-conf" if args.read_gridcoin_conf else "",
            f"--conf ({len(args.conf_paths)} stated)" if args.conf_paths else "",
        ) if part
    )


def say_inert_read_flags(unused: str) -> None:
    """Say that nothing was opened. Separate from the decision so the words are testable."""
    say(f"  {unused} DOES NOTHING WITHOUT --read-daemon-confs, so NO CONF WAS OPENED and")
    say("  no credential was read from one. Pass --read-daemon-confs as well, or neither.")
    say("")


def stated_confs(assignments: list[str]) -> tuple[dict[str, Path], list[str]]:
    """Parse --conf ASSET=PATH into {chain: Path}, plus a refusal per malformed one.

    THE DECISION IS "WHICH FILE", AND IT IS THE OPERATOR'S. This function only reads what
    they said; it opens nothing. It is separate from the read so it can be called with a
    seeded list and asserted on without a conf file existing anywhere (rule 10), and so
    that a typo in a path is reported as a typo rather than as "no conf at ...".

    REFUSED, NOT DROPPED. An ASSET this tool has no DAEMON_CONFS entry for cannot have
    its conf read no matter what path is given, so silently ignoring the argument would
    produce a run that read the conventional file while the operator believed it read
    theirs -- the same class of quiet substitution the override exists to stop. Every
    refusal names the thing to fix.

    ~ IS EXPANDED HERE because an operator types the path, and `--conf
    GRC=~/.GridcoinResearch-desk/testnet/gridcoinresearch.conf` reaches argv unexpanded
    when it is quoted or when the shell is not involved. A path beginning with a literal
    `~` directory does not exist on this host and the expansion is what was meant.

    NOT CHECKED FOR EXISTENCE. That is the read's business and it already names the file
    it could not open; checking twice would report the same problem in two voices, and
    the second one would be the one that goes stale.
    """
    stated: dict[str, Path] = {}
    refusals: list[str] = []
    for assignment in assignments:
        if "=" not in assignment:
            refusals.append(
                f"--conf {assignment} has no `=`. The form is ASSET=PATH, e.g. "
                f"--conf GRC=~/.GridcoinResearch-desk/testnet/gridcoinresearch.conf"
            )
            continue
        chain, _, raw = assignment.partition("=")
        chain = chain.strip().upper()
        if not raw.strip():
            refusals.append(f"--conf {chain}= was given no path, so nothing was stated for {chain}")
        elif chain not in DAEMON_CONFS:
            refusals.append(
                f"--conf {chain}=... names a chain whose conf this tool does not read. It reads "
                f"{', '.join(sorted(DAEMON_CONFS))} -- the three that keep rpcuser and rpcpassword "
                f"in a daemon conf. SOL, XRP and ICP have no conf to read."
            )
        else:
            stated[chain] = Path(raw.strip()).expanduser()
    return stated, refusals


def say_stated_confs(stated: dict[str, Path], refusals: list[str]) -> None:
    """What --conf was taken to mean, before anything is opened. `(none)` is a result.

    SAID BEFORE THE READ, because the path is the one input that decides whose credentials
    get written into .env, and an operator who mistyped a datadir needs to see it on the
    line above the result rather than infer it from a 401 an hour later (rule 14:
    announce before, not only after; echo the parameters that decide the answer).
    """
    say("  CONF PATHS YOU STATED")
    if not stated and not refusals:
        say("    (none)  <- the tables below decide, which for GRC means the CONVENTIONAL path")
        say("            and on a host with several Gridcoin datadirs that is the wrong wallet")
        return
    for chain in sorted(stated):
        say(f"    {chain:26} <- {stated[chain]}")
    for refusal in refusals:
        # ONE SPELLING OF A REFUSAL LINE IN THIS FILE, which is say_set_result()'s
        # `REFUSED: ` and not a wrapped block: these sentences are long, and a second
        # indent convention in the same output is the thing a reader notices instead of
        # the refusal.
        say(f"    REFUSED: {refusal}")


def credentials_from_confs(
    discovered: dict, already: dict, *, gridcoin: bool, stated: Mapping[str, Path] | None = None,
) -> tuple[dict[str, tuple[str, str]], set[str]]:
    """rpcuser and rpcpassword out of each daemon's own conf. NO VALUE IS EVER PRINTED.

    =========================================================================
    "ji don't know any of those passwords" -- the operator, 2026-10-11
    =========================================================================

    They were right not to know them. The credentials are already written down, once, in
    each daemon's own conf, and asking a human to copy a password out of a file and type
    it into another one is a step that gets skipped or got wrong. Measured on their host:
    `~/.litecoin/litecoin.conf` and `~/.GridcoinResearch/gridcoinresearch.conf` both carry
    rpcuser and rpcpassword; `~/.bitcoin/bitcoin.conf` does not exist and bitcoind is not
    running, so BTC has nothing to read and stays unconfigured.

    I DECLINED TO BUILD THIS AT FIRST, citing chain-safety's "never move, copy, or read
    back a credential", and that refusal is what left them unable to proceed. The rule is
    about ME: Claude must not pull a credential into a conversation. A tool that runs on
    the operator's machine, reads their conf, writes it into their .env and never prints
    it is a different thing -- I never see it, they never paste it, and it never leaves
    the host. It is OPT-IN so that the move is theirs.

    =========================================================================
    IT USES THE PARSER THAT ALREADY EXISTS
    =========================================================================

    chains/daemon_conf.parse_daemon_conf() handles the two things a hand-rolled reader
    gets wrong, and both are written down there against measurements: credentials sit at
    the TOP level while the port sits in a `[network]` section, and a `#` only starts a
    comment at the START of a line -- "an rpcpassword containing `#` is both legal and
    likely from a generator. Stripping inline would silently truncate it, and the failure
    would arrive as a 401 from the daemon with nothing pointing here."

    NOT rpc_settings_from_conf(), WHICH IS THE NEARBY FUNCTION AND IS TOO STRICT: it
    demands rpcuser, rpcpassword AND rpcport together and raises if any is missing. The
    port is already established by probing here, and a conf that omits rpcport because the
    daemon uses the default is an ordinary conf -- refusing it would lose the credentials
    over a value this tool does not need.

    THE SECTION COMES FROM THE PROBED PORT. 19332 is testnet, 19443 is regtest, and
    chains/daemon_conf.CONF_FALLBACK_NETWORK hardcodes regtest for LTC -- which would read
    the wrong section of the operator's real conf, since their litecoind answers on 19332.

    RETURNS {variable: (value, reason)} with the reason naming the FILE and never the
    value, which is the same split chains/daemon_conf.describe() makes and for the same
    stated reason: "a second formatter is a second chance to interpolate the wrong key".
    """
    found: dict[str, tuple[str, str]] = {}
    replacing: set[str] = set()
    for chain, (_conventional, user_variable, pass_variable) in sorted(DAEMON_CONFS.items()):
        if chain == "GRC" and not gridcoin:
            say(f"    {chain}: NOT READ. {GRIDCOIN_CONF_HAZARD}")
            continue
        # THE `continue` THAT USED TO BE HERE MADE THE RECOMMENDED FIX A NO-OP, THREE
        # TIMES, ON THE DAY THE RECOMMENDATION WAS WRITTEN.
        #
        #     if already.get(user_variable) and already.get(pass_variable):
        #         continue
        #
        # "Do not re-ask what is already set" is right for ASKING and wrong for READING A
        # CONF: the whole point of this flag is to replace a value with the one the daemon
        # actually read, and a credential that is PRESENT AND WRONG is the only state that
        # produces the 401 it exists to fix. The operator's GRC_RPC_USER and GRC_RPC_PASS
        # were both set, both wrong, and credential_check.py said DIFFERENT on both --
        # so this returned before opening the conf, every run.
        #
        # AND IT WAS INVISIBLE, which is why it survived three rounds: the `continue` came
        # before any say(), so the chain left no line, while the summary printed
        # `(none)  <- nothing was read; every line above says why` with nothing above it.
        # Rule 13's skip-that-reads-as-success and rule 14's blank gap in one statement.
        # Every outcome below prints a line now, including the two that write nothing.
        # THE PORT IS READ BEFORE THE FILE IS CHOSEN, which is the ordering the whole
        # conf_candidates() change turns on: a daemon answering on a regtest port is this
        # repository's own harness and its conf is the harness's, and one on a testnet
        # port is the operator's and its conf is the conventional one. Both facts were
        # already measured by discovery; nothing new is probed here.
        port = _probed_port(chain, discovered, already)
        candidates = conf_candidates(chain, port, stated)
        path = next((candidate for candidate in candidates if candidate.is_file()), None)
        if path is None:
            # EVERY PLACE LOOKED IS NAMED, not just the first. An operator told "no conf
            # at ~/.bitcoin/bitcoin.conf" goes and creates one there, and the harness
            # writes to ~/regtest/btc -- so a message naming one of two paths sends them
            # to the wrong file (rule 14).
            where = " or ".join(str(candidate) for candidate in candidates)
            say(f"    {chain}: no conf at {where}, so there is nothing to read")
            continue
        try:
            text = path.read_text()
        except OSError as error:
            # NAMED, not broad: an unreadable conf is a permissions answer, not a crash.
            # A conf owned by another user is an ordinary state on a shared host.
            say(f"    {chain}: {path} could not be read ({type(error).__name__}), so nothing was taken")
            continue
        # THE SECTION COMES FROM THE CONF AND NOT FROM THE PORT since 2026-10-11, and
        # conf_section_for_port() carries the measurement: the operator's bitcoind is on
        # TESTNET4 at rpcport 18443, so CONF_SECTION_FOR_PORT alone sent this read to a
        # `[regtest]` block the daemon itself ignores. The reason is SAID rather than kept,
        # because "which block of my own file did you read" is the only question an
        # operator can act on when the credentials turn out to be the wrong ones (rule 14).
        section, section_why = conf_section_for_port(chain, text, port)
        say(f"    {chain}: {path}: {section_why}")
        settings = parse_daemon_conf(text, network=section)

        missing = [key for key in ("rpcuser", "rpcpassword") if not settings.get(key)]
        if missing:
            say(f"    {chain}: {path} has no {', '.join(missing)}. If it uses rpcauth= instead, that is a "
                f"HASH and cannot be used as a password -- set rpcuser/rpcpassword, or leave the chain off")
            continue

        # WHAT IS IN .env ALREADY, COMPARED THROUGH THE MODULE WHOSE JOB THAT IS.
        # chains/credential_match.compare() answers SAME / DIFFERENT / ONLY_IN_* and
        # cannot report a value -- its verdict type has no field one could occupy. A
        # second comparison spelled here would be rule 8's shape over the one decision
        # in this function where being wrong writes somebody's wrong password back.
        verdicts = credential_match.compare(
            {"user": already.get(user_variable, ""), "password": already.get(pass_variable, "")},
            settings,
        )
        differing = [v for v in verdicts if v.verdict == credential_match.DIFFERENT]
        agreeing = [v for v in verdicts if v.verdict == credential_match.SAME]
        if differing and chain not in (stated or {}):
            # A GUESSED PATH MAY NOT REPLACE A CREDENTIAL, and this is the inverse hazard
            # of the one above rather than caution. This host runs THREE Gridcoin wallets
            # and DAEMON_CONFS knows one path, so replacing from a file nobody named is
            # how an operator who had correctly pointed .env at their desk daemon ends up
            # with the mainnet staking wallet's login. Reported, not written, and the
            # line says how to assert the path.
            say(f"    {chain}: {path} DIFFERS from what .env already has for "
                f"{', '.join(v.conf_key for v in differing)}, and this path came from this "
                f"tool's own table rather than from you -- so nothing was taken. If that IS "
                f"the file your daemon read, say so with --conf {chain}={path} and it will be "
                f"replaced; if it is not, the conf you want is named in the daemon's own "
                f"`Using data directory` log line.")
            continue
        if agreeing and not differing:
            # SAID RATHER THAN SKIPPED. This is the branch the old `continue` collapsed
            # into silence, and "already correct" is the single most useful thing an
            # operator chasing a 401 can be told -- it moves the question to WHICH FILE
            # the daemon read, which is where credential_match.headline() sends them.
            say(f"    {chain}: {path} already MATCHES .env for "
                f"{', '.join(v.conf_key for v in agreeing)}; nothing to change here. A 401 with "
                f"this file matching means the daemon read a DIFFERENT file -- check its "
                f"`Using data directory` log line.")
            continue

        found[user_variable] = (settings["rpcuser"], f"rpcuser read from {path}")
        # THE VALUE IS IN THE TUPLE BECAUSE IT HAS TO BE WRITTEN. It is not in the
        # REASON, and say_dry_run()/say_what_was_written() print names and reasons only.
        found[pass_variable] = (
            settings["rpcpassword"],
            f"rpcpassword read from {path} and NOT shown"
            + (f" (top level plus [{section}])" if section else ""),
        )
        # NAMED AS A REPLACEMENT so main()'s `not already.get(name)` filter does not drop
        # it -- that filter is correct for a discovered DEFAULT and wrong for a value the
        # operator pointed this tool at. It is a SET of names and never of values.
        for verdict in differing:
            replacing.add(user_variable if verdict.env_key == "user" else pass_variable)
        if replacing:
            say(f"    {chain}: REPLACING {', '.join(sorted(replacing))} -- .env has a value for "
                f"each and it is not what {path} says. The stated path is the authority.")
    return found, replacing


def _probed_port(chain: str, discovered: dict, already: dict) -> int:
    """The port this run established for `chain`, or 0. Decides which conf section to read.

    0 RATHER THAN A GUESS when nothing established one: CONF_SECTION_FOR_PORT then misses,
    `section` is empty, and parse_daemon_conf() reads the top level only -- which is where
    credentials conventionally live, so the common case still works and no section is read
    on a guess about the network.
    """
    variable = CHAIN_PORTS[chain].port_variable if chain in CHAIN_PORTS else ""
    raw = str(discovered.get(variable, ("",))[0] or already.get(variable, "") or "").strip("'\"")
    return int(raw) if raw.isdigit() else 0


def asked_for(discovered: dict, already: dict) -> list[tuple[str, str]]:
    """ASKED, plus any RPC port that discovery missed. Nothing falls between the two.

    =========================================================================
    A VARIABLE IN NEITHER LIST WAS SILENTLY ABSENT, FOUND ON THE OPERATOR'S FIRST
    GOOD RUN
    =========================================================================

    COMPUTED AS A COMPLEMENT, NOT AS A SECOND LIST, and that is the whole shape of the
    fix. "Everything this tool can set, minus what was discovered, minus what is already
    in .env" has no gap by construction -- where a hand-maintained second list would
    close the three ports and leave the next discovery-only variable in the same hole.
    I wrote it as that second list first and the ICP values immediately fell through it:
    they are discovered from the replica, so a replica that does not answer left them
    reported-as-unset and never asked for.

    The three Bitcoin-family ports were discovery-only: probed against
    network_target.CHAIN_PORTS, and not in ASKED because probing was expected to answer.
    When it did not, the variable appeared in no list at all. Their run printed

        WOULD WRITE      7 discovered value(s) and 9 asked value(s)

    with BTC_RPC_PORT in neither -- nothing was listening on 18443 or 18332, so their
    bitcoind was down or on another port. BTC would have stayed unset, the container
    would have built no BTC adapter, and the ATM page would have shown BTC as NONE after
    a configuration run that reported success. That is the SAME DEFECT THIS WHOLE TOOL
    EXISTS FOR -- a variable nothing sets and nothing mentions -- reproduced inside the
    fix for it, and it is rule 14's "treat 'skipped' plus 'success' in the same output as
    a defect in the output".

    SO DISCOVERY IS A SHORTCUT, NOT A GATE. A probed port saves the operator a question;
    a probe that finds nothing turns back into the question. The hint comes from
    ChainPorts.test_hint, so the prompt names the ports that chain actually uses rather
    than asking for a number with no help -- and that hint is the same string the
    workers' banner and create_swap()'s refusal print, which is why it lives in that
    table (rule 8).

    ORDER IS ASKED-FIRST, THEN THE MISSED PORTS, so a second run looks the same as the
    first for everything that did not change.
    """
    asked = {name for name, _what in ASKED}
    missed = []
    for variable in every_variable_this_tool_can_set():
        if variable in asked or variable in discovered or already.get(variable):
            continue
        missed.append((variable, FALLBACK_HELP.get(variable, _port_help(variable))))
    return [*ASKED, *missed]


def _port_help(variable: str) -> str:
    """Help text for an RPC port whose probe found nothing, from CHAIN_PORTS' own hint.

    THE HINT IS THE SAME STRING the workers' banner and create_swap()'s refusal print,
    which is why it lives in that table rather than here (rule 8) -- and why its own
    comment records being wrong once: it named only 25779 while the operator's Gridcoin
    test daemon listens on 25715, on the one line whose job is to say what to set.

    FALLS BACK TO A BARE SENTENCE for a variable that is not a port at all, so a name
    added to every_variable_this_tool_can_set() without an entry in FALLBACK_HELP gets a
    usable prompt rather than a KeyError. An unhelpful prompt is a defect; a crash in a
    configuration tool is a worse one.
    """
    for asset, ports in CHAIN_PORTS.items():
        if ports.port_variable == variable:
            return (
                f"{asset}'s RPC port -- nothing is listening on {ports.test_hint}, so its daemon "
                f"is down or on another port. Leave BLANK to leave {asset} unconfigured."
            )
    return f"{variable} -- could not be discovered. Leave BLANK to leave it unset."


#: The one variable that selects which compose files the project uses. Not a chain
#: variable, which is why it is not in every_variable_this_tool_can_set() -- discovery
#: cannot find it and nothing is "still unset" about it.
COMPOSE_FILE_VARIABLE = "COMPOSE_FILE"


def compose_files(already: dict) -> list[str]:
    """The current COMPOSE_FILE as a list, in order, with empties dropped.

    THE SEPARATOR IS A COLON and that is compose's own convention on this platform,
    the same one .env already carries:

        COMPOSE_FILE=docker-compose.yml:docker-compose.web.armed-grc.yml

    Quotes are stripped because .env values written by write_env() are single-quoted and
    a hand-edited line may not be, and both have to parse the same way -- two readers of
    one file disagreeing about its syntax is rule 8's shape in the small.
    """
    raw = (already.get(COMPOSE_FILE_VARIABLE) or "").strip().strip("'\"")
    return [part for part in raw.split(":") if part]


def arm_verdict(chain: str, already: dict) -> tuple[str, str]:
    """The COMPOSE_FILE this chain's overlay should produce, and why -- or ("", why not).

    =========================================================================
    THE OPERATOR HAD TO HAND-EDIT A FILE HOLDING THEIR WALLET PASSPHRASE, AND
    I GAVE THEM A FRAGMENT TO PASTE INSTEAD OF A COMMAND
    =========================================================================

    2026-10-11. Arming ICP is one change to one line of .env. What they got from me was

        :docker-compose.web.armed-icp.yml

    in a code block, which they pasted at a shell prompt, which said "command not
    found". Then they recreated the container, which recreated it UNARMED, because the
    line had never changed. That was the third fragment-in-a-code-block of the day and
    the standing instruction against it is explicit.

    THE REAL DEFECT IS THAT THERE WAS NOTHING TO RUN. .env holds
    GRIDCOIN_WALLET_PASSPHRASE. Asking somebody to edit that file by hand, in a
    terminal, to append a colon-separated fragment to the end of one line, is asking
    for the edit that takes the passphrase with it -- and write_env() already does
    exactly this safely: atomic through a temp file and a rename, mode 600 with the
    umask set before the file exists, every line it does not own carried through byte
    for byte, and a name it owns REPLACED rather than duplicated.

    TWO COMPOSE_FILE LINES IS THE FAILURE A HAND EDIT INVITES. dotenv parsers disagree
    about which wins, so the effective value would depend on the reader -- and compose
    and config.read_env_file() are two different readers of this same file.
    tests/test_configure_env.py::test_a_name_it_owns_is_replaced_and_not_duplicated
    already pins that for every other variable.

    IT REFUSES WHEN THE OVERLAY'S REQUIRED VARIABLE IS UNSET, and that guard is the
    reason this returns a verdict rather than just doing it. compose interpolates EVERY
    variable in EVERY file it is given BEFORE it looks at which service was named, so a
    COMPOSE_FILE naming docker-compose.web.armed-sol.yml with no
    SOL_PAYOUT_KEYPAIR_HOST_FILE set does not break the SOL payout -- it breaks EVERY
    docker compose command in the project, including `up`, `ps` and `config`. That
    defect landed twice in one day on 2026-10-05 and
    tests/test_docker_build_context.py carries the measurement. Writing it into .env
    would make it survive the shell, which is worse than the original: the operator's
    next command fails and nothing they typed in that shell explains why.

    ARMING IS STILL AN ACT. The overlays argue at length that naming the file should
    take a deliberate step rather than being inherited from whatever happens to be
    exported, and `--arm icp` is that step: explicit, named, and in shell history. What
    it changes is that the step PERSISTS -- which is what the operator has asked for
    all day, and what their .env already does for armed-grc.
    """
    overlays = {asset: (filename, variable) for asset, filename, variable in arming_overlays()}
    asset = chain.strip().upper()
    if asset not in overlays:
        return "", (f"{asset} has no arming overlay. There is one per chain that needs a signing "
                    f"credential, and this tree has {', '.join(sorted(overlays)) or '(none)'}.")
    filename, variable = overlays[asset]
    if variable and not already.get(variable):
        return "", (f"{asset}'s overlay {filename} requires {variable} and it is not set in .env. "
                    f"NOTHING was changed, on purpose: compose interpolates every variable in every "
                    f"file it is given before it looks at which service was named, so arming "
                    f"{asset} without it would not break {asset} -- it would break EVERY docker "
                    f"compose command in this project. Set {variable} first.")
    current = compose_files(already)
    if filename in current:
        return "", f"{asset} is already armed: {filename} is in {COMPOSE_FILE_VARIABLE} already."
    if not current:
        # BASE FIRST, because an overlay alone defines no services. A COMPOSE_FILE of
        # just the overlay would leave compose with a `web` that has volumes and no
        # image -- and .env with no COMPOSE_FILE at all is a state a fresh checkout is
        # in, so this is reachable rather than defensive.
        current = ["docker-compose.yml"]
    return ":".join([*current, filename]), (
        f"{asset} armed by appending {filename} to {COMPOSE_FILE_VARIABLE}"
    )


def disarm_verdict(chain: str, already: dict) -> tuple[str, str]:
    """The COMPOSE_FILE with this chain's overlay REMOVED, and why -- or ("", why not).

    =========================================================================
    ARMING HAD NO INVERSE, AND I NEEDED ONE FOR A CHAIN I ARMED ON A FALSE BELIEF
    =========================================================================

    2026-10-11. I armed ICP because I had established that its payout method was
    complete and its transport could sign, and both of those are true. What I had NOT
    established is that anything calls it correctly:
    services/payout_service.broadcast_payout() passed send_to_address(address, amount)
    with two positional arguments, so created_at_time arrived as None and the adapter
    refused before any ledger call. Measured through the real process_pending_payouts():
    zero ledger calls, swaps.status failed, and the customer's deposit already credited.

    So arming made five `* -> ICP` directions offerable against a payout path that
    refused every time.

    THAT CALL SITE WAS FIXED LATER THE SAME DAY and the tenses above are past for that
    reason; the narrative is kept because `--disarm` exists BECAUSE of it, and a
    justification rewritten to describe a world where the problem never happened stops
    justifying anything. broadcast_payout() now has an ICP branch passing the swap's
    recorded created_at as the ledger's idempotency key, and
    tests/test_icp_e2e_both_directions.py's two DEFECT assertions were inverted with it.
    What is still unproven is the NETWORK: no `* -> ICP` payout has completed on any
    network through that path, only against a stub ledger. So `--disarm` is no longer
    the remedy for a path that refuses every time -- it is the inverse of `--arm`, which
    is reason enough for it to exist. The safe move is to stop offering them until the call site is
    fixed -- and the only way to do that was to hand-edit the COMPOSE_FILE line in a
    .env that holds GRIDCOIN_WALLET_PASSPHRASE, which is the exact thing --arm exists to
    avoid. An operation with no inverse is one an operator cannot back out of safely.

    IT IS THE SAME SHAPE AS arm_verdict() AND DELIBERATELY NOT A FLAG ON IT. A boolean
    parameter would make one function hold two decisions whose refusals are different:
    arming refuses when the overlay's variable is unset, and disarming never can --
    removing a file from the list cannot break a compose command, it can only stop a
    posture. Rule 10 wants the decision in the smallest testable piece, and these are
    two decisions.

    THE BASE FILE CAN NEVER BE REMOVED. docker-compose.yml defines the services; a
    COMPOSE_FILE without it leaves compose with a `web` that has volumes and no image.
    Only `docker-compose.web.armed-<chain>.yml` entries are removable here, which is
    structural rather than checked: the name comes from arming_overlays().
    """
    overlays = {asset: (filename, variable) for asset, filename, variable in arming_overlays()}
    asset = chain.strip().upper()
    if asset not in overlays:
        return "", (f"{asset} has no arming overlay, so there is nothing to disarm. This tree has "
                    f"{', '.join(sorted(overlays)) or '(none)'}.")
    filename, _variable = overlays[asset]
    current = compose_files(already)
    if filename not in current:
        return "", (f"{asset} is not armed: {filename} is not in {COMPOSE_FILE_VARIABLE}, so "
                    f"nothing was changed. The container cannot sign {asset} payouts as it is.")
    remaining = [part for part in current if part != filename]
    return ":".join(remaining), (
        f"{asset} disarmed by removing {filename} from {COMPOSE_FILE_VARIABLE}"
    )


def say_disarm_result(chain: str, value: str, why: str, *, apply: bool) -> None:
    """What disarming did or refused to do, and the step that is REQUIRED after it.

    THE RECREATE IS WHAT ACTUALLY DISARMS, and saying so matters more here than on the
    way in: an operator who reads "disarmed" and does not recreate still has a running
    container that holds the mounted credential and the identity it was started with.
    On the arming side forgetting the recreate leaves a chain that cannot pay, which is
    visible as a NONE lamp. Forgetting it here leaves a chain that CAN, which looks
    exactly like success.
    """
    say(f"  DISARMING {chain.strip().upper()}")
    if not value:
        say(f"    REFUSED: {why}")
        return
    say(f"    {why}")
    say(f"    {COMPOSE_FILE_VARIABLE} becomes {value or '(empty)'}")
    if not apply:
        say("    Nothing was written. Re-run with --apply to change .env.")
        return
    say("")
    say("  STILL ARMED IN THE RUNNING CONTAINER. .env selects the compose files for the")
    say("  NEXT command. The container that is serving now was created WITH the overlay,")
    say("  so it still holds the credential and can still sign -- this step is REQUIRED,")
    say("  not optional:")
    say("      docker compose up -d --force-recreate web")
    say("  Then the chain reports it cannot pay out, and the pairs that pay into it go")
    say("  UNAVAILABLE rather than taking a deposit they cannot settle.")


def say_arm_result(chain: str, value: str, why: str, *, apply: bool) -> None:
    """What arming did or refused to do, and the step that is REQUIRED after it."""
    say(f"  ARMING {chain.strip().upper()}")
    if not value:
        say(f"    REFUSED: {why}")
        return
    say(f"    {why}")
    say(f"    {COMPOSE_FILE_VARIABLE} becomes {value}")
    if not apply:
        say("    Nothing was written. Re-run with --apply to change .env.")
        return
    say("")
    say("  NOT YET ARMED IN ANY RUNNING CONTAINER. .env selects the compose files for the")
    say("  NEXT command; the container that is running was created from the old list:")
    say("      docker compose up -d --force-recreate web")
    say("  Arming is a custody posture. Nothing here signed anything, and the payout path")
    say("  for this chain has still never been exercised on a chain.")


def set_verdict(assignments: list[str], secrets: set[str]) -> tuple[dict[str, str], list[str]]:
    """NAME=VALUE pairs to write, and a refusal line for each one that cannot be.

    =========================================================================
    THE OPERATOR PRESSED ENTER PAST BOTH SOL PROMPTS, AND THAT WAS A REASONABLE
    REFLEX
    =========================================================================

    2026-10-11. I had spent the afternoon telling them to press enter past every prompt,
    because for three BTC variables there was nothing to type. Then two prompts appeared
    that did need answers -- SOL_HOT_WALLET and SOL_PAYOUT_KEYPAIR_HOST_FILE, both of
    which they already had -- and five enters went past all five. The run reported
    NOTHING TO WRITE, correctly, and SOL stayed unconfigured.

    AN INTERACTIVE PROMPT IS THE WRONG SHAPE FOR A VALUE THAT IS NOT A SECRET. It cannot
    be pasted as part of a command, cannot be re-run from shell history, and cannot be
    put in a note for next time -- so each attempt is a fresh chance to mistype or skip.
    getpass() earns that cost for a credential. A public key and a file path do not.

    ARGV IS WORLD-READABLE THROUGH /proc, WHICH IS WHY THIS REFUSES A SECRET. The whole
    reason set_grc_passphrase.sh prompts rather than taking an argument is that
    /proc/<pid>/cmdline can be read by any process on the host for as long as the command
    runs. So --set is allowed for exactly the names .env.example does NOT mark SECRET, and
    the refusal says why rather than just declining.

    AN UNKNOWN NAME IS REFUSED TOO, which is typo protection rather than ceremony: `--set
    SOL_HOTWALLET=...` would otherwise write a variable nothing reads, and the operator
    would be left looking at a correctly-written .env and an unconfigured chain. That is
    the silent-absence defect this whole tool exists for, arriving through its own door.

    RETURNS BOTH, so one call answers "what will be written" and "what was refused and
    why" -- a caller cannot report a partial result as a success (rule 13).
    """
    manageable = set(every_variable_this_tool_can_set())
    values: dict[str, str] = {}
    refusals: list[str] = []
    for raw in assignments:
        name, separator, value = raw.partition("=")
        name, value = name.strip(), value.strip()
        if not separator:
            refusals.append(f"{raw!r} is not NAME=VALUE, so nothing was taken from it")
        elif name in secrets:
            refusals.append(
                f"{name} is marked SECRET in .env.example and --set puts a value in argv, which "
                f"/proc/<pid>/cmdline exposes to every process on this host for as long as the "
                f"command runs. Nothing was written for it. Use the prompt, which reads with no "
                f"echo -- or set_grc_passphrase.sh for the Gridcoin one."
            )
        elif name not in manageable:
            refusals.append(
                f"{name} is not a variable this tool manages, so writing it would put a line in "
                f".env that nothing reads. Did you mean one of: "
                f"{', '.join(sorted(n for n in manageable if n.startswith(name[:4]))) or 'none of them'}?"
            )
        elif not value:
            refusals.append(f"{name} was given no value. To leave it unset, omit it entirely.")
        elif "'" in value:
            refusals.append(
                f"{name}'s value contains a single quote, which a single-quoted .env value cannot "
                f"carry in any dotenv dialect. Stored mangled it would fail later as an "
                f"authentication or path error nobody connects to this step."
            )
        else:
            values[name] = value
    return values, refusals


def say_set_result(values: dict[str, str], refusals: list[str], *, apply: bool) -> None:
    """What --set will write and what it refused. Names only for anything secret-adjacent.

    THE VALUES ARE PRINTED BECAUSE NONE OF THEM CAN BE A SECRET -- set_verdict() refuses
    a name marked SECRET before it reaches here, so echoing what was taken is safe and is
    the only way an operator can see a typo in a path before the container is recreated.
    That is the inverse of the rule everywhere else in this file, and it holds only
    because of the guard upstream.
    """
    say("  SETTING")
    for name, value in sorted(values.items()):
        say(f"    {name:30} {value}")
    if not values:
        say("    (none)")
    for refusal in refusals:
        say(f"    REFUSED: {refusal}")
    if values and not apply:
        say("    Nothing was written. Re-run with --apply.")


def change_posture(args, already: dict) -> int | None:
    """Apply --arm or --disarm, or None when neither was given. Returns main()'s exit code.

    EXTRACTED BECAUSE main() WENT OVER THE CEILING, and rule 12 says what that means
    rather than what to silence: "a main() past the ceiling is orchestration that has
    swallowed decisions, which is rule 10's defect wearing a lint code. The fix is to
    extract the decision so it can be called with seeded inputs, not to raise the
    ceiling." Adding --disarm took C901 to 12 and PLR0915 to 52, and the thing it had
    swallowed is this: the decision of which posture change to make, and what its exit
    code means.

    THE TWO BRANCHES WERE NEAR-IDENTICAL, which is the other reason (rule 8). A verdict,
    a reporter, a conditional write and an exit code, twice, differing only in which pair
    of functions is called. One table makes them one path, so a third posture -- if one
    is ever added -- cannot drift from the other two in how it writes or what it returns.

    None RATHER THAN 0 FOR "NEITHER FLAG", because 0 is a real exit code here and main()
    has to tell "nothing to do, carry on with discovery" apart from "the posture change
    succeeded". Returning 0 for both would make `--apply` alone stop before discovering
    anything.

    ARMING AND DISARMING TOGETHER IS REFUSED rather than ordered. They are opposite acts
    on one line of .env, and which one wins is not a choice a tool should make silently
    for an operator: whichever order it picked would be right half the time and
    unexplained both times.
    """
    #: (flag value, verdict function, reporter). One row per posture.
    changes = (
        (args.arm, arm_verdict, say_arm_result),
        (args.disarm, disarm_verdict, say_disarm_result),
    )
    if args.arm and args.disarm:
        say("  --arm AND --disarm IN ONE COMMAND, so nothing was done. They are opposite acts on")
        say("  one line of .env and the order they would be applied in is not something this")
        say("  tool should pick for you. Run them separately.")
        return 1
    for chain, verdict, report in changes:
        if not chain:
            continue
        value, why = verdict(chain, already)
        report(chain, value, why, apply=args.apply)
        if value and args.apply:
            write_env({COMPOSE_FILE_VARIABLE: value})
        return 0 if value else 1
    return None


def say_banner(apply: bool, already: dict) -> None:
    """What this run is about to do, before it does any of it (rule 14).

    THE MODE LINE IS FIRST AND SAYS WHETHER ANYTHING WILL BE WRITTEN, because the
    difference between a discovery run and an applying one is the whole question an
    operator has about a configuration tool, and finding out afterwards is too late.

    THE NAMES IN .env ARE LISTED AND NO VALUE IS READ BACK. An operator needs to know
    what is already set -- otherwise a second run looks like it is about to re-ask for
    everything -- and the values include RPC passwords, which never reach a terminal.
    """
    say("configure_env: chain configuration that survives a new shell and a reboot")
    say(f"  env file        {ENV_FILE}")
    say(f"  mode            {'APPLY -- .env will be written' if apply else 'DISCOVER ONLY -- nothing will be written'}")
    say("  network         every default offered below is TESTNET or DEVNET. A mainnet")
    say("                  value typed over one is your decision and this cannot tell.")
    say("  secrets         read with no echo, never printed, never in argv, never logged.")
    say("")
    say(f"  already in .env {', '.join(sorted(already)) or '(none)'}  <- names only; no value is read back")
    say("")


def discover_all(already: dict) -> dict[str, tuple[str, str]]:
    """Everything the running system can be asked for, with WHY each value was chosen.

    THE REASON TRAVELS WITH THE VALUE because a discovered port is a claim about the
    operator's machine, and `BTC_RPC_PORT 18443` alone gives them no way to check it.
    `18443 because something is listening there` is checkable in one command.

    THE DATABASE IS ONE OF THE SOURCES, which is the point of it being in here rather
    than beside it. `already` is a parameter for exactly one reason: a chain's shared
    deposit account can be repointed, so when .env already carries one, reading a
    DIFFERENT address off an older swap and reporting it as discovered would offer to
    overwrite the live account with a historical one. Every other source here is
    position-independent and is filtered by main()'s `not already.get(name)`; this one
    has to not look. The parameter is what makes that testable with a seeded dict.

    NOT FLAG-GATED, unlike credentials_from_confs(). The hazard that made reading a conf
    opt-in is that a conf holds a password and Gridcoin's is shared with a live staking
    wallet. A deposit address is PUBLIC -- it is printed on the ATM page for a customer
    to send to -- and the handle is opened mode=ro, so there is nothing here to gate.
    """
    say("  DISCOVERING")
    found: dict[str, tuple[str, str]] = {}
    found.update(discover_ports())
    found.update(discover_icp())
    found.update(accounts_from_the_database(already))
    for variable, (value, why) in ENDPOINT_DEFAULTS.items():
        found[variable] = (value, f"the documented {why} endpoint")
    found["ICP_DFX_SERVICE"] = (ICP_SERVICE, "the compose SERVICE name (not the container name)")
    for variable, (value, why) in sorted(found.items()):
        say(f"    {variable:26} {value}")
        say(f"    {'':26} <- {why}")
    if not found:
        say("    (none)  <- nothing could be discovered; every value below will be asked for")
    say("")
    return found


def every_variable_this_tool_can_set() -> list[str]:
    """Every name this tool could put in .env, so "still unset" can be computed honestly.

    DERIVED FROM THE THREE SOURCES rather than listed, because a fourth list would be the
    one that goes stale: ASKED, the RPC ports in CHAIN_PORTS, and the discovery defaults.
    A variable added to any of them is covered here without being added twice (rule 8).
    """
    return [
        *(name for name, _what in ASKED),
        *(ports.port_variable for ports in CHAIN_PORTS.values()),
        *ENDPOINT_DEFAULTS,
        *ICP_DISCOVERY,
        "ICP_DFX_SERVICE",
    ]


def still_unset(values: dict, already: dict) -> list[str]:
    """Every variable this tool can set that is NOT set once `values` is written.

    =========================================================================
    THE BLOCK THIS REPLACES COULD NOT PRINT A NAME, AND PRINTED "(none)" TO AN
    OPERATOR WHO HAD THREE UNANSWERABLE VARIABLES ON SCREEN
    =========================================================================

    say_dry_run() used to compute

        will_be_set = set(values) | {name for name, _what in needed} | set(already)
        still_unset = sorted(set(every_variable_this_tool_can_set()) - will_be_set)

    and print the result under "AFTERWARDS, STILL UNSET", followed by the sentence "the
    chains these belong to stay unconfigured, and the ATM page will show them as NONE."
    That difference is EMPTY FOR EVERY INPUT. asked_for() builds its list as the
    complement of every_variable_this_tool_can_set() -- which is the whole point of
    commit defa3b4, so that no variable can fall between discovery and the questions --
    so a name is always discovered, already in .env, or asked for. Union those three and
    you have the set you subtracted them from.

    MEASURED RATHER THAN ARGUED, 2026-10-11: over 4000 randomized (already, discovered)
    pairs drawn from the 17 variables this tool could set THAT MORNING -- the set is 18
    since SOL_PAYOUT_KEYPAIR_HOST_FILE was added later the same day, and the figure is
    left at what was actually run rather than restated (rule 3: the denominator is the
    set the sampling drew from, not today's).
    A field that cannot vary is rule 14's defect in its strongest form -- it is not a
    number whose meaning is unstated, it is a number with no meaning at all.

    AND IT WAS READ AS GOOD NEWS. The operator's run on 2026-10-11 listed six variables
    under WOULD ASK FOR -- three of them BTC's, on a host with no bitcoin.conf, no
    cookie file and nothing listening on 18332 or 18443, so they were unanswerable -- and
    then said "(none) <- every variable this tool can set is accounted for". "Accounted
    for" was true in the sense that each had been MENTIONED. It reads as "nothing will be
    left unconfigured", which was false for three of them before the run started.

    THE HONEST QUESTION IS ASKED AT TWO DIFFERENT MOMENTS and this answers both, which is
    why it takes `values` rather than `needed`. On the dry run nothing has been answered
    yet, so the answer is "what depends on your answering". After an apply it is "what
    you skipped, or refused, or could not supply". Same derivation, two labels, one place
    (rule 8) -- and on the apply path the result VARIES, which is what makes its `(none)`
    worth printing.

    `already.get(name)` RATHER THAN `name in already`, because existing_assignments() can
    carry a name with an empty value: docker-compose.web.yml passes every chain variable
    as `${VAR:-}`, so an empty string in .env reaches the container as an empty string and
    build_adapters() skips the chain. A name set to nothing is unset for every purpose
    this report has.
    """
    return sorted(name for name in every_variable_this_tool_can_set()
                  if not values.get(name) and not already.get(name))


#: A per-chain arming overlay at the repository root, and the asset it arms. Matched rather
#: than listed, so an overlay added later is reported without this file being edited
#: (rule 8) -- and so a reader cannot be told about an overlay that no longer exists.
ARMED_OVERLAY = re.compile(r"^docker-compose\.web\.armed-([a-z]+)\.yml$")

#: The one required variable inside an overlay, as compose spells a hard requirement.
#: `:?` rather than `:-` is the whole distinction: compose REFUSES to start when a `${VAR:?}`
#: is unset or empty, which is what makes arming an act rather than an inheritance.
OVERLAY_REQUIRES = re.compile(r"\$\{([A-Z][A-Z0-9_]*):\?")


def arming_overlays() -> list[tuple[str, str, str]]:
    """(asset, overlay filename, the one variable it requires), read off the root.

    COMMENT LINES ARE SKIPPED and that is not cosmetic: every one of these files explains
    the "at most one ${VAR:?} per compose file" rule IN PROSE, quoting the literal
    `${VAR:?}` to name it. Reading the raw text finds `VAR` as a requirement of two of the
    three overlays -- which is the same prose-read-as-code defect that put configure_env.py
    into tests/test_daemon_conf.py's chain-resolver register this morning, in a second
    file, within the hour. The lesson keeps costing because the scan keeps being written
    fresh.

    SORTED BY ASSET so two runs read the same, and a diff between them is about arming
    rather than about directory order.
    """
    found = []
    for path in sorted(REPO_ROOT.glob("docker-compose.web.armed-*.yml")):
        match = ARMED_OVERLAY.match(path.name)
        if not match:
            continue
        code = "".join(line for line in path.read_text().splitlines()
                       if not line.lstrip().startswith("#"))
        required = OVERLAY_REQUIRES.findall(code)
        found.append((match.group(1).upper(), path.name, required[0] if required else ""))
    return sorted(found)


def say_arming(already: dict, written: dict) -> None:
    """What this tool does NOT set, and what a chain still needs before it can PAY.

    =========================================================================
    THE 18 VARIABLES ABOVE MOSTLY CONFIGURE THE DEPOSIT LEG. A CHAIN CAN READ
    DEPOSITS PERFECTLY AND BE UNABLE TO SEND A PAYOUT.
    =========================================================================

    Found 2026-10-11, while about to tell the operator that setting SOL_HOT_WALLET would
    finish SOL. It would not. SOL payouts need SOL_PAYOUT_KEYPAIR_HOST_FILE and the
    docker-compose.web.armed-sol.yml overlay; XRP needs XRP_PAYOUT_SECRET_SEED and
    armed-xrp. Neither was in this tool's set, and the report said

        AFTERWARDS  2 of 17 written with no prompt
                    4 of 17 ONLY IF YOU ANSWER

    with nothing to indicate that two chains would still be half-configured after every
    one of them was set. THAT IS THE DEFECT THIS WHOLE TOOL EXISTS FOR, one layer out:
    something the system needs that nothing sets and nothing mentions. Measured against
    .env.example, which declares 92 variables to this tool's 18 -- most of the other 74
    are tunables with defaults, and these are the ones that gate a leg.

    THOSE 17s ARE THE TRANSCRIPT OF THE RUN THAT FOUND THIS AND ARE LEFT ALONE. The set
    was 17 that morning and is 18 now, because SOL_PAYOUT_KEYPAIR_HOST_FILE was added
    to it LATER THE SAME DAY -- the drift is the point (rule 1), and rewriting a recorded
    transcript to agree with today would destroy the only evidence of what the operator
    actually saw. Every count outside the indented block is the present one.

    AND "IT WILL NEVER SET THEM" WAS THIS PARAGRAPH'S NEXT SENTENCE, WHICH IS NOW FALSE
    OF ONE OF THE TWO. The line that matters is not payout-versus-deposit, it is PATH
    versus CREDENTIAL, and it was only ever the credential half that chain-safety forbids:
    "Never move, copy, or read back a key. Not .env, not a keypair JSON, not a WIF."

      SOL_PAYOUT_KEYPAIR_HOST_FILE   a PATH. The tool sets it, as of 2026-10-11, and the
                                     justification is at every_variable_this_tool_can_set()'s
                                     entry for it. Naming where a keypair lives is not
                                     reading one, and the alternative was the operator
                                     hand-editing the .env that holds
                                     GRIDCOIN_WALLET_PASSPHRASE -- which is a worse place
                                     to put a human with a text editor.
      XRP_PAYOUT_SECRET_SEED         a CREDENTIAL. Never set, never read back, never
                                     echoed. This block reports its PRESENCE and nothing
                                     else.
      GRIDCOIN_WALLET_PASSPHRASE     the same, and the reason the one above matters.

    So a configuration tool that armed a payout path would still be the inheritance the
    overlays were separated to prevent -- and naming a keypair's location is not that.
    What arms SOL is the overlay plus a keypair file this tool never opens.

    SO THIS BLOCK REPORTS AND DOES NOT OFFER. PRESENCE ONLY, NEVER A VALUE -- the same
    rule the "already in .env" line follows, and here it matters more: one of these is a
    wallet passphrase and one is an XRP secret seed.

    =========================================================================
    `written` IS A SECOND PARAMETER BECAUSE THIS BLOCK CONTRADICTED THE ONE FOUR
    LINES ABOVE IT, IN THE OPERATOR'S OWN TERMINAL
    =========================================================================

    Measured on their host 2026-10-11, one run of `--set ... --apply`:

        WROTE            2 variable(s) into .../.env, mode 600
                         SOL_HOT_WALLET, SOL_PAYOUT_KEYPAIR_HOST_FILE
        ...
        SOL   docker-compose.web.armed-sol.yml
              needs SOL_PAYOUT_KEYPAIR_HOST_FILE  <- NOT set in .env

    existing_assignments() is read ONCE at the top of main(), before anything is
    written, so `already` is a snapshot of .env as it was when the run started. Every
    caller after a write was handing this function a stale one. The operator read "NOT
    set" about a variable the same screen had just reported writing, and the next
    reasonable thing to do with that is set it again.

    THIS IS THE DEFECT I FIXED IN credentials_from_confs() EARLIER THE SAME DAY, where
    the output said "LTC_RPC_USER <- read from the conf", then "skipped LTC_RPC_USER",
    then "WROTE ... LTC_RPC_USER". Rule 13's "treat 'skipped' plus 'success' in the same
    output as a defect in the output" -- reproduced in new code within hours of my
    writing that sentence into a commit message.

    REQUIRED RATHER THAN DEFAULTED TO {}, which is the point. A default would let a
    future call site inherit the bug silently; a required parameter makes every site
    state what was written, and the dry-run site passing {} is a true statement rather
    than an omission.
    """
    # THE STATE AFTER THE WRITE, not the snapshot from before it. `written` wins,
    # which is the right precedence: it is what is on disk now.
    current = {**already, **written}
    overlays = arming_overlays()
    compose = current.get("COMPOSE_FILE", "")
    say("")
    say("  PAYOUTS ARE ARMED SEPARATELY, and this tool sets none of it. The variables above")
    say("  configure the DEPOSIT leg; a chain can read deposits and still be unable to send.")
    if not overlays:
        say("    (none)  <- no docker-compose.web.armed-*.yml at the repository root, so no")
        say("            chain can sign a payout from this checkout at all")
        return
    for asset, filename, variable in overlays:
        active = "named in COMPOSE_FILE" if filename in compose else "NOT in COMPOSE_FILE"
        say(f"    {asset:5} {filename}")
        if not variable:
            # AN OVERLAY CAN LEGITIMATELY REQUIRE NOTHING, and printing "needs " with an
            # empty name would be rule 14's blank gap in the one block whose job is to
            # say what is missing. ICP is that case: its signing identity is created by
            # dfx inside the replica container and lives on a named volume, so the
            # overlay mounts that volume read-only and there is no host path to name.
            # Naming the file on the command line is the whole act.
            say(f"    {'':5} needs no variable -- it mounts a credential the project already")
            say(f"    {'':5} holds, so naming the file IS the arming; overlay {active}")
            continue
        held = "set in .env" if current.get(variable) else "NOT set in .env"
        say(f"    {'':5} needs {variable}  <- {held}; overlay {active}")
    settable = [variable for _asset, _filename, variable in overlays if variable]
    # WHICH OF THESE THIS TOOL CAN SET, and the distinction is value versus location.
    # This block used to say "none of these is settable here: each names a signing
    # credential", and that was true of two of the four. SOL's names a FILE and ICP's
    # names a dfx identity; the credential is what they point AT. Saying all four were
    # credentials is what kept the askable one out of ASKED and sent an operator to
    # hand-edit .env for a path.
    askable = [variable for variable in settable if variable in {name for name, _w in ASKED}]
    withheld = [variable for variable in settable if variable not in askable]
    if askable:
        say(f"    {len(askable)} of these this tool CAN set, because they name a location rather")
        say(f"    than a credential: {', '.join(askable)}. Run --apply and answer the prompt.")
    if withheld:
        say(f"    {len(withheld)} it will never set -- {', '.join(withheld)} IS the credential, and")
        say("    this tool never reads, copies or writes one. Arming is an act you take on the")
        say("    command line, which is why it is a separate file per chain.")


def say_dry_run(needed: list, values: dict, secrets: set, already: dict) -> None:
    """What an --apply run would ask for and write. Rule 14: `(none)` is a result.

    A SECRET IS NAMED AND ITS PURPOSE IS NOT, which is the one place this output is
    deliberately less helpful: the purpose strings for the credentials say which daemon
    conf to read them from, and a dry run is the output most likely to be pasted into a
    chat or an issue.
    """
    say("  WOULD ASK FOR")
    for name, what in needed:
        say(f"    {name:26} {'SECRET -- not echoed' if name in secrets else what}")
    if not needed:
        say("    (none)  <- everything not discovered is already in .env")
    say("")
    say(f"  WOULD WRITE      {len(values)} discovered value(s) and {len(needed)} asked value(s)")
    say("  Nothing was written. Re-run with --apply to be prompted and have .env updated.")
    say("")
    # THE TWO GROUPS AN OPERATOR HAS TO TELL APART, which the block this replaces did not
    # distinguish: a value this tool has already found, and a value that exists only if
    # they can answer a question. For the run that produced this fix, three of the second
    # group were BTC's on a host with no bitcoind, so they were never going to be set --
    # and the old block's "(none)" said the opposite.
    total = len(every_variable_this_tool_can_set())
    pending = still_unset(values, already)
    say(f"  AFTERWARDS       {len(values)} of {total} written with no prompt  <- discovered above;")
    say("                   nothing left to decide about these")
    say(f"                   {len(pending)} of {total} ONLY IF YOU ANSWER  <- press enter past one and it")
    say("                   stays unset; the chain that needs it then reads NONE on the ATM page")
    for name in pending:
        say(f"    {name}")
    if not pending:
        say("    (none)  <- nothing is waiting on an answer; --apply writes and asks nothing")


def collect_answers(needed: list, secrets: set) -> dict[str, str]:
    """Ask for each value that could not be discovered. Skipping one is allowed and said.

    A SKIPPED VALUE IS NOT AN ERROR. An operator configuring three chains out of six
    should not have to invent a Solana account, and the consequence is stated per
    variable rather than left to be discovered on the ATM page: the chain that needs it
    stays unconfigured, which is exactly what the lamps already report.

    A SINGLE QUOTE IS REFUSED for the reason set_grc_passphrase.sh refuses one: a
    single-quoted dotenv value cannot carry one in any dialect, and a credential stored
    mangled does not fail here -- it fails later as an authentication error nobody
    connects to this step.
    """
    say("  ASKING FOR WHAT COULD NOT BE DISCOVERED")
    if not needed:
        say("    (none)")
    answers: dict[str, str] = {}
    for name, what in needed:
        value = ask(name, what, secret=name in secrets)
        if not value:
            say(f"    skipped {name} (left unset; the chain that needs it stays unconfigured)")
            continue
        if "'" in value:
            say(f"    REFUSED {name}: it contains a single quote, which a single-quoted .env value")
            say("            cannot carry in any dotenv dialect. Nothing was written for it -- stored")
            say("            mangled it would fail later as an authentication error nobody connects")
            say("            to this step. Say so and the mechanism changes.")
            continue
        answers[name] = value
    return answers


def say_still_unset(values: dict, already: dict) -> None:
    """What this tool can set and did not, after a run that wrote. `(none)` is a result.

    ONE FORMATTER FOR BOTH CALLERS -- the write path and main()'s nothing-to-write exit.
    Two spellings of this block is rule 8's shape, and the second one would be the one
    that forgot the denominator.

    THE DENOMINATOR IS STATED beside the count (rule 3). "3 unset" invites the reading
    "3 out of a handful"; "3 of 17" says how much of the surface is dark. The count that
    produced this fix was six of seventeen, and the block it replaces printed none.
    """
    pending = still_unset(values, already)
    total = len(every_variable_this_tool_can_set())
    if not pending:
        say(f"  STILL UNSET      (none)  <- all {total} variables this tool can set are set")
        return
    say(f"  STILL UNSET      {len(pending)} of {total} this tool can set, because they were not")
    say("                   discovered and were not answered")
    for name in pending:
        say(f"    {name}")
    say("                   the chains these belong to stay unconfigured and read NONE on")
    say("                   the ATM page. That is a result, not a failure of this run.")


def say_what_was_written(values: dict, already: dict) -> None:
    """The result, what is STILL unset after it, and the step that is REQUIRED after that.

    AN ALREADY-RUNNING CONTAINER KEEPS THE ENVIRONMENT IT STARTED WITH, so writing .env
    changes nothing a serving process can see until the container is recreated. Omitting
    that line would leave an operator looking at six NONE lamps on a correctly
    configured machine -- which is the exact state this tool was written for.

    THE "STILL UNSET" BLOCK BELONGS HERE AND WAS ONLY EVER IN THE DRY RUN, which is the
    second half of the defect still_unset() documents. The dry run had a block that could
    not print a name; the apply run -- the one that actually produces skips -- had no
    block at all. collect_answers() prints a line per skip as it happens, so the
    information scrolled past one variable at a time and nothing added it up. An operator
    who pressed enter past three BTC prompts saw three "skipped" lines, then a WROTE
    block, and nothing saying BTC was still unconfigured.

    Rule 13 is the sharper version of why: "treat 'skipped' plus 'success' in the same
    output as a defect in the output." A WROTE line that counts only what succeeded, in
    a run that also skipped, is that defect -- and here the `(none)` VARIES, so it is a
    result rather than a constant dressed as one.
    """
    say("")
    say(f"  WROTE            {len(values)} variable(s) into {ENV_FILE}, mode 600")
    say(f"                   {', '.join(sorted(values))}")
    say("                   every other line carried through unchanged; no .bak was made")
    say("  persists         across new shells and reboots, which is the whole point")
    say("")
    say_still_unset(values, already)
    say("")
    say("  NOT YET PROVEN   writing .env says nothing about what the CONTAINER received. An")
    say("                   already-running container keeps the environment it started with,")
    say("                   so the next step is REQUIRED rather than optional:")
    say("                       docker compose up -d --force-recreate web")
    say("                   then open /admin/wallets and read the lamps, or:")
    say("                       docker compose exec -T web printenv | grep -c '^BTC_RPC_PORT='")


def build_parser() -> argparse.ArgumentParser:
    """Every flag this tool takes. SEPARATE FROM main() so the help text is testable.

    It was inline until 2026-10-12, and the reason it moved is a sentence somewhere
    else: regtest/daemons.GRC_CREDENTIALS_ARE_PER_NETWORK now tells an operator to run
    `configure_env.py --read-daemon-confs --read-gridcoin-conf --conf GRC=<path>`, and a
    remedy naming a flag that does not exist is the wrong-comment bug in the one
    sentence somebody acts on while already stuck on a 401. The gate that stops that is
    tests/test_swap_readiness.test_the_401_guidance_recommends_a_check_that_answers_the_right_question(),
    which greps this parser's own help -- and it could not be written while the parser
    only existed inside a function that also writes .env.

    credential_check.py already had build_parser() for the same reason. Two tools whose
    flags are named in prose, one spelling of "where the flags live" (rule 8).
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="WRITE .env. Without it this discovers and reports and writes nothing.")
    parser.add_argument(
        "--read-daemon-confs", action="store_true",
        help=("Read rpcuser and rpcpassword out of each daemon's OWN conf instead of asking for "
              "them. No value is printed: the reason line names the FILE and says the password was "
              "read and not shown. Gridcoin needs --read-gridcoin-conf as well; see its hazard."),
    )
    parser.add_argument(
        "--conf", metavar="ASSET=PATH", action="append", default=[], dest="conf_paths",
        help=("The conf file a daemon ACTUALLY read, for one chain. Repeatable. An override is "
              "the whole list for that chain -- there is no fallback behind it. Needed whenever "
              "a daemon was started with a -datadir that is not the conventional one, which on "
              "this host is every Gridcoin daemon: the credentials live in that datadir's "
              "`testnet/` subdirectory and differ from the mainnet file. The daemon prints "
              "`Using data directory <datadir>/testnet` in its own startup log."),
    )
    parser.add_argument(
        "--read-gridcoin-conf", action="store_true",
        help=("Also read Gridcoin's credentials. SEPARATE because the hazard differs in kind: "
              "~/.GridcoinResearch/gridcoinresearch.conf belongs to the wallet on the DEFAULT "
              "datadir, and a Gridcoin mainnet wallet is typically a live staking wallet. Pair it "
              "with --conf GRC=<your datadir>/testnet/gridcoinresearch.conf, which is where a "
              "testnet daemon's credentials live and where they differ. Requires "
              "--read-daemon-confs."),
    )
    parser.add_argument(
        "--set", metavar="NAME=VALUE", action="append", default=[], dest="assignments",
        help=("Write one variable without a prompt. Repeatable. REFUSED for any name marked "
              "SECRET in .env.example, because argv is world-readable through /proc -- those "
              "still go through the no-echo prompt. Needs --apply to write."),
    )
    parser.add_argument(
        "--disarm", metavar="CHAIN", default="",
        help=("Remove that chain's docker-compose.web.armed-<chain>.yml from COMPOSE_FILE in .env, "
              "so the container can no longer sign that chain's payouts. Needs --apply to write, "
              "and a --force-recreate afterwards: the RUNNING container keeps the credential it "
              "was started with."),
    )
    parser.add_argument(
        "--arm", metavar="CHAIN", default="",
        help=("Add that chain's docker-compose.web.armed-<chain>.yml to COMPOSE_FILE in .env, so "
              "the container can SIGN that chain's payouts. Needs --apply to write. REFUSED when "
              "the overlay's own required variable is unset, because compose would then fail every "
              "command in the project rather than just that chain's payout."),
    )
    return parser


def main() -> int:
    """Discover, ask, write. ORCHESTRATION ONLY -- every decision is in a function above.

    ruff's C901 took this to 12 when it held the banner, the discovery report, the dry
    run and the ask loop inline, and rule 12 reads that as orchestration having swallowed
    decisions: "the fix is to extract the decision so it can be called with seeded inputs,
    not to raise the ceiling." Each extracted piece is now callable with a seeded dict,
    which is what lets the output be asserted on without a terminal.
    """
    parser = build_parser()
    args = parser.parse_args()

    secrets = secret_names()
    already = existing_assignments()
    say_banner(args.apply, already)

    # ARMING IS ITS OWN RUN AND RETURNS, rather than being folded into discovery. It
    # changes a custody posture and nothing else, so mixing it with a credential sweep
    # would make one command do two unrelated things to .env -- and the operator reading
    # the output could not tell which half did what (rule 14).
    if args.assignments:
        values, refusals = set_verdict(args.assignments, secrets)
        say_set_result(values, refusals, apply=args.apply)
        if values and args.apply:
            write_env(values)
            say_what_was_written(values, already)
        say_arming(already, values if args.apply else {})
        return 1 if refusals else 0

    posture = change_posture(args, already)
    if posture is not None:
        return posture

    discovered = discover_all(already)

    # FROM THE CONFS, AND ONLY WHEN ASKED. Merged into `discovered` so credentials flow
    # through the same dry-run report, the same "still unset" computation and the same
    # write -- a second path for them would be a second chance to print one.
    # BOUND BEFORE THE BRANCH, because the filter below reads it on every path. The
    # first draft bound it only inside the `if`, so a plain `configure_env.py` -- no
    # --read-daemon-confs -- raised UnboundLocalError. Three existing tests caught it,
    # which is what they are for: the cost of this change landing on a run that does not
    # read a conf at all was exactly the case I was not thinking about.
    replacing: set[str] = set()
    if args.read_daemon_confs:
        say("  READING CREDENTIALS FROM EACH DAEMON'S OWN CONF")
        stated, conf_refusals = stated_confs(args.conf_paths)
        say_stated_confs(stated, conf_refusals)
        from_confs, replacing = credentials_from_confs(
            discovered, already, gridcoin=args.read_gridcoin_conf, stated=stated,
        )
        for variable, (_value, why) in sorted(from_confs.items()):
            say(f"    {variable:26} <- {why}")
        if not from_confs:
            say("    (none)  <- nothing was read; every line above says why")
        discovered.update(from_confs)
        say("")
    elif unused_flags := inert_read_flags(args):
        say_inert_read_flags(unused_flags)

    # `or name in replacing` IS THE THIRD CLAUSE AND IT WAS MISSING, which is the second
    # half of the no-op proven on 2026-10-13: even once the conf was read, this dropped
    # every name already in .env -- so a replacement the operator asked for by stating the
    # path was discarded one line after being found. `replacing` holds NAMES only.
    values = {name: value for name, (value, _why) in discovered.items()
              if not already.get(name) or name in replacing}
    # `name not in values` IS THE THIRD CLAUSE AND IT WAS MISSING, which produced output
    # that contradicted itself on the first real run of --read-daemon-confs:
    #
    #     LTC_RPC_USER  <- rpcuser read from ~/.litecoin/litecoin.conf
    #     skipped LTC_RPC_USER (left unset; the chain that needs it stays unconfigured)
    #     WROTE  ... LTC_RPC_USER ...
    #
    # Read from the conf, then asked for anyway, then reported as skipped, then written.
    # Every line was true of the step that printed it and the block as a whole said three
    # incompatible things. That is rule 13's "treat 'skipped' plus 'success' in the same
    # output as a defect in the output" -- and the prompt it produced was worse than the
    # wording: an operator pressing enter past a value that was already found has been
    # asked a question with no right answer.
    needed = [(name, what) for name, what in asked_for(discovered, already)
              if not already.get(name) and name not in values]

    if not args.apply:
        say_dry_run(needed, values, secrets, already)
        # NOTHING WAS WRITTEN, and {} says so rather than omitting the argument.
        say_arming(already, {})
        return 0

    values.update(collect_answers(needed, secrets))
    if not values:
        say("")
        say("  NOTHING TO WRITE. .env is unchanged.")
        # AND WHAT THAT LEAVES DARK, on the one exit most likely to be read as harmless.
        # "Nothing to write" on a run where every prompt was skipped is the same output
        # as "nothing to write" on a run where everything was already correct, and those
        # are opposite states (rule 14).
        say_still_unset(values, already)
        say_arming(already, {})
        return 0

    write_env(values)
    say_what_was_written(values, already)
    say_arming(already, values)
    return 0


if __name__ == "__main__":
    sys.exit(main())
