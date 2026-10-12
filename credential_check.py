#!/usr/bin/env python3
"""Do this process's RPC credentials match the conf a daemon read? Prints no value, ever.

Role: file (entry point; the operator runs this)
Reads: the process environment through config.Config, and ONE conf file whose path you
      give it. Both contain a password; neither value can reach the output -- see
      chains/credential_match.py, whose verdict type has no field a value could occupy.
Writes: nothing. No .env, no database, no log.
Can move funds: no. It opens no socket and builds no adapter.
Mainnet-safe: yes, and it REFUSES a mainnet-port chain by name rather than merely
      declining to act on it -- the same guard chain_daemons.refuse_to_touch() applies,
      because reading the mainnet staking wallet's conf is itself the hazard there.

WHY THIS EXISTS, 2026-10-12, AND MY WRONG CHECK IS THE REASON.

The operator's Gridcoin desk daemon answered `401 Authorization Required` from the
container AND from the host on 127.0.0.1. Gridcoin hardcodes 127.0.0.0/8 as allowed
before the allow list is consulted, so a loopback 401 rules out rpcallowip, the bind,
the bridge and the firewall together: it is the credentials.

I then had them run `grep -cE '^(rpcuser|rpcpassword)'` over both conf files and told
them "a 0 for the testnet file is the whole answer." It came back 2 and 2, and the check
was answering the wrong question -- PRESENCE, where the open proposition was EQUALITY.
regtest/daemons.GRC_CREDENTIALS_ARE_PER_NETWORK had already said the net-specific file
"carries DIFFERENT credentials from the mainnet file beside it."

Equality cannot be checked by eye without reading a password out loud, so it is checked
here instead and only a verdict word comes out.

THE CONF PATH IS AN ARGUMENT AND IS NOT GUESSED. This host runs THREE Gridcoin wallets
across three datadirs (OPEN_FINDINGS finding 2) and chains/daemon_conf.
CONF_FALLBACK_NETWORK deliberately excludes GRC for exactly that reason. Guessing which
file to compare against is the mistake that cost five rounds on 2026-10-09; the daemon's
own startup log prints `Using data directory <datadir>/testnet` every run and that is
the only reliable source.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT / "swap_terminal"))

# .env BEFORE config, because config.Config is a class body evaluated once at import:
# a load afterwards changes nothing and looks like it worked. See env_preamble's header.
import env_preamble  # noqa: F401, E402  # isort: skip  -- MUST stay above `from config import Config`; E402 because REPO_ROOT is assigned before the sys.path line, which ruff does not accept as preamble -- the same reason every import below carries one

import chain_daemons  # noqa: E402
from chains import credential_match  # noqa: E402
from chains.daemon_conf import DaemonConfError, parse_daemon_conf  # noqa: E402
from config import Config  # noqa: E402
from report_block import labeled, wrapped  # noqa: E402


def refusal(asset: str, settings: dict) -> str:
    """Why this comparison must not be made, or "".

    MAINNET BY NAME, the way chain_daemons.refuse_to_touch() refuses to stop one and
    chains/daemon_launch.propose() refuses to suggest starting one. Here the hazard is
    slightly different and no weaker: the mainnet Gridcoin wallet is unlocked for
    staking, and opening its conf to compare a password is reading key-adjacent material
    out of a live wallet's directory for a diagnostic. The three verbs now agree.
    """
    port = settings.get("port")
    if port in chain_daemons.MAINNET_RPC_PORTS:
        return (
            f"REFUSED: {asset}_RPC_PORT is {port}, which is "
            f"{chain_daemons.MAINNET_RPC_PORTS[port]}. This will not open a mainnet wallet's "
            f"conf to compare a credential, for the reason nothing here will stop or start one."
        )
    return ""


def run(args) -> int:
    """Orchestration only: refuse, parse, compare, print (rule 10)."""
    asset = args.asset.upper()
    settings = (Config.RPC.get(asset) or {})
    path = Path(args.conf).expanduser()

    print("credential check -- does this process's RPC login match the conf a daemon read?")
    print(labeled("asset", asset))
    print(labeled("conf", str(path)))
    print(labeled("prints", "VERDICTS ONLY. No value from either side can reach this output; "
                            "chains/credential_match.FieldVerdict has no field to hold one."))
    print(labeled("asks nothing", "no socket is opened and no adapter is built, so this cannot "
                                  "be confused with a reachability probe -- use `swap_stack.py "
                                  "chains` for that"))
    print()

    if not settings:
        print(labeled("REFUSED", f"{asset} has no entry in Config.RPC, so there is nothing to "
                                 f"compare. Nothing was read."))
        return 2
    stop = refusal(asset, settings)
    if stop:
        for line in wrapped("", stop):
            print(line)
        return 2

    try:
        conf_values = parse_daemon_conf(path.read_text(encoding="utf-8"), network=args.network)
    except OSError as error:
        # THE PATH AND THE ERRNO, NEVER THE CONTENTS. A conf that could not be read is a
        # path problem, and this is the one place a traceback could otherwise carry a
        # line of the file into the output.
        print(labeled("REFUSED", f"{path} could not be read ({type(error).__name__}: "
                                 f"{error.strerror or 'no reason given'}). Nothing was compared."))
        return 2
    except DaemonConfError as error:
        print(labeled("REFUSED", f"{path} did not parse as a daemon conf: {error}"))
        return 2

    verdicts = credential_match.compare(settings, conf_values)
    for verdict in verdicts:
        print(labeled(verdict.conf_key, f"{verdict.verdict}  <- {verdict.label}: "
                                        f"{asset}_RPC_{verdict.env_key.upper()} vs "
                                        f"{verdict.conf_key} in that file"))
    print()
    for line in wrapped("verdict", credential_match.headline(verdicts)):
        print(line)
    # EXIT 0 ONLY WHEN EVERY FIELD MATCHES, so this is scriptable; 1 when something
    # differs, and 2 for a refusal or an unreadable file -- which is NOT a verdict about
    # the credentials and must not read as one (rule 13).
    return 0 if {v.verdict for v in verdicts} == {credential_match.SAME} else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare this process's RPC credentials against a daemon's conf file. "
                    "Prints verdicts only -- never a value. Exits 0 when every field matches, "
                    "1 when one differs, 2 when nothing was compared.",
        epilog="The conf PATH is required and never guessed: this host runs three Gridcoin "
               "wallets across three datadirs, and the daemon's own startup log prints "
               "`Using data directory <datadir>/testnet` every run, which is the only "
               "reliable way to know which file it read.",
    )
    parser.add_argument("--asset", required=True, help="the chain, e.g. GRC")
    parser.add_argument("--conf", required=True, metavar="PATH",
                        help="the conf file the daemon actually read")
    parser.add_argument(
        "--network", default="",
        help="the conf SECTION to read, for a chain that uses them: `test`, `regtest` or "
             "`testnet4`. Gridcoin needs none -- it uses a separate FILE per network rather "
             "than sections, so point --conf at the net-specific file instead.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
