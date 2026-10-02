"""Configuration for the Flask swap terminal, read from the environment once.

Role: submodule (configuration constants; holds no decision of its own)
Reads: the process environment at IMPORT time -- SWAP_DB_PATH, the per-chain
       RPC credentials and endpoints, fee and tolerance settings
Writes: nothing
Can move funds: no by itself, but it CARRIES the values that decide what
       moves: DEFAULT_FEE_BPS, the *_NETWORK_FEE_RESERVE figures, the
       *_MIN_CONFIRMATIONS thresholds and ALLOWED_PAIRS. Changing any of those
       changes what gets sent or when, which makes them the operator's (rule
       16), not something to adjust in passing.
Mainnet-safe: yes to import, and since 2026-09-26 yes to RUN with nothing set.
       THE DEFAULTS USED TO POINT AT MAINNET -- 8332 Bitcoin, 9332 Litecoin,
       15715 Gridcoin -- so a checkout with no environment was configured for
       mainnet daemons. The operator reported the consequence on 2026-09-26:
       "we're still pulling from grc mainnet wallet and not the testnet wallet."
       They were right. Nothing in the serving path loads a .env (not wsgi.py,
       not gunicorn.conf.py, not this file), so an unset GRC_RPC_PORT fell
       through to 15715 and payout_service.refresh_wallet_inventory(), which
       calls get_balance() on every adapter every cycle, polled the operator's
       live staking wallet on a loop.

       All three now default to UNCONFIGURED_PORT (0) and chains/registry.py
       skips an unconfigured chain, so a missing setting REFUSES instead of
       guessing the most expensive possible answer. This is not a new
       convention: SOL is constructed only when SOL_RPC_URL is set, and XRP's
       url defaults to empty. The three oldest chains were the three not
       following the rule their own file states twice.

       To reach a chain, set its port: BTC_RPC_PORT=18443 and LTC_RPC_PORT=19443
       for regtest (18332/19332 for testnet), GRC_RPC_PORT=25779 for the
       Gridcoin test chain. These must be in the PROCESS environment -- exported,
       or supplied by whatever starts gunicorn -- because nothing here reads a
       .env, and adding load_dotenv() to a module read at import is the
       import-time side effect rule 12 names as a measured past defect.

Everything here is evaluated when the module is imported, because `Config` is a
class body. That is why tests/conftest.py sets SWAP_DB_PATH before importing
anything: setting it afterwards is too late, the value is already baked in.
"""

import os
from pathlib import Path
from typing import ClassVar

from network_target import UNCONFIGURED_PORT

BASE_DIR = Path(__file__).resolve().parent

# A SET-BUT-EMPTY ENVIRONMENT VARIABLE MEANS ABSENT, NOT "".
#
# Measured 2026-09-26, on the operator's machine, from a command I gave them. They
# ran a generator that wrote an env file from a shell that did not have the values,
# so it wrote `export GRC_RPC_PORT=''` -- five empty exports. Sourcing that file
# made things WORSE than having nothing set:
#
#     ValueError: invalid literal for int() with base 10: ''
#
# raised from line 204 of this file, at IMPORT time, so open_swap.py, both workers
# and app.py all died on the traceback before any of them could say what was wrong.
# os.getenv returns "" for a variable that is set to nothing, the two-argument
# default never applies, and int("") raises.
#
# 51 reads in this file, 27 of them typed. Any single empty variable took down every
# entry point -- and an empty variable is an ORDINARY thing: a generator like mine,
# an `export FOO=` in a shell script, a CI template with a blank field, a .env line
# with nothing after the `=`. The whole design of this file since this morning is
# that a missing setting REFUSES legibly and names itself (chains/registry.py skips
# the chain, the swap page badges the pair DISABLED and prints the variable). An
# empty value was the one way to get a traceback instead of that sentence.
#
# .strip() as well as the emptiness test, because `export GRC_RPC_PORT=" "` is the
# same mistake with a space in it, and int(" ") raises identically.
def _env(name: str, default: str = "") -> str:
    """os.getenv, except that a set-but-empty value falls back to `default`."""
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value


def _env_int(name: str, default: str) -> int:
    """_env, as an int. The default is a STRING so the call site reads like the
    _env() it replaced, and so there is exactly one spelling of each default."""
    return int(_env(name, default))


def _env_float(name: str, default: str) -> float:
    return float(_env(name, default))


class Config:
    SECRET_KEY = _env("SECRET_KEY", "swap-terminal-dev")
    DB_PATH = _env("SWAP_DB_PATH", str(BASE_DIR / "swap_terminal.db"))
    QUOTE_TTL_SECONDS = _env_int("QUOTE_TTL_SECONDS", "600")
    RATE_CACHE_SECONDS = _env_int("RATE_CACHE_SECONDS", "30")
    DEFAULT_FEE_BPS = _env_int("DEFAULT_FEE_BPS", "150")
    AMOUNT_TOLERANCE_PCT = _env_float("AMOUNT_TOLERANCE_PCT", "0.01")
    SMALL_SWAP_MANUAL_REVIEW_USD = _env_float("SMALL_SWAP_MANUAL_REVIEW_USD", "5000")
    BTC_MIN_CONFIRMATIONS = _env_int("BTC_MIN_CONFIRMATIONS", "2")
    LTC_MIN_CONFIRMATIONS = _env_int("LTC_MIN_CONFIRMATIONS", "2")
    GRC_MIN_CONFIRMATIONS = _env_int("GRC_MIN_CONFIRMATIONS", "6")
    # SOL_MIN_CONFIRMATIONS IS NOT A COUNT OF BLOCKS. Solana has commitment
    # LEVELS -- processed / confirmed / finalized -- and this is a rung on the
    # ladder in chains/solana_units.COMMITMENT_RANKS, where 3 = finalized.
    # The name matches the other three because services/deposit_service.py
    # reads `swap["min_confirmations"]` for every chain, and the alternative
    # was a per-chain branch in the one function that decides whether a deposit
    # is creditable. The vocabulary lives in one place instead (rule 11), and
    # SolanaAdapter.__init__ REFUSES a value that is not a rung -- an operator
    # who copies Gridcoin's 6 here would otherwise stall every SOL swap
    # forever, silently, because no deposit can reach rank 6.
    SOL_MIN_CONFIRMATIONS = _env_int("SOL_MIN_CONFIRMATIONS", "3")
    BTC_NETWORK_FEE_RESERVE = _env_float("BTC_NETWORK_FEE_RESERVE", "0.00002")
    LTC_NETWORK_FEE_RESERVE = _env_float("LTC_NETWORK_FEE_RESERVE", "0.001")
    GRC_NETWORK_FEE_RESERVE = _env_float("GRC_NETWORK_FEE_RESERVE", "0.01")
    # ClassVar annotations: these are shared configuration read by every
    # request, not per-instance defaults. Config is never instantiated --
    # app.py copies its uppercase attributes into app.config -- so the
    # mutable-default hazard RUF012 warns about does not arise, and saying
    # so in the type is better than suppressing the check.
    # ENABLING A PAIR IS LIVE POSTURE and is the operator's call (rule 16).
    # XRP<->GRC was added 2026-09-26 on their explicit instruction ("let's take
    # ripple to grc"), after -- and only after -- the mechanism underneath it was
    # measured rather than assumed:
    #
    #   deposits    XRP is attributed by DestinationTag on ONE shared account,
    #               not by a per-swap address. The allocator's uniqueness is a
    #               database constraint, tags are never reused, and
    #               deposit_service.attributable_events() filters an event's tag
    #               against the swap's own -- without which every customer's
    #               payment would credit whichever swap was being refreshed.
    #   payouts     chains/xrp.py::send_to_address() signed, submitted and had
    #               validated a real testnet payment (hash E118CA96...,
    #               tesSUCCESS) before this line changed.
    #   pricing     services/pricing.py carries an XRP id; a partial CoinGecko
    #               response raises rather than deriving a rate from a missing leg.
    #
    # WHAT THIS STILL DOES NOT DO, and the distinction matters: a pair being
    # allowed does not mean a swap can be created. create_swap() refuses an XRP
    # swap while XRP_DEPOSIT_ACCOUNT is unset, which is the custody decision and
    # has no default. So this line opens the gate; the operator's account setting
    # is what puts anything through it.
    # WHAT A PAIR NEEDS BEFORE IT BELONGS HERE, because this set alone is not
    # enough and the tree has said so twice:
    #
    #   an adapter        chains/registry.build_adapters() must construct both
    #                     chains, which needs their *_RPC_* settings
    #   a USD price       services/pricing.IDS must carry both, or create_quote()
    #                     accepts the swap and then fails on a missing price
    #   a FEE RESERVE     config.<TO_ASSET>_NETWORK_FEE_RESERVE must exist, or the
    #                     quote refuses: a reserve is a PRICING decision and is
    #                     never defaulted to zero, because zero quotes a payout
    #                     the destination chain will not deliver
    #
    # THE FOUR ADDED 2026-09-30 all satisfy those, and each pays out to an asset
    # whose reserve already exists (BTC 0.00002, LTC 0.001):
    #
    #   ("BTC", "LTC"), ("LTC", "BTC")   both proven by atomic_swap.py's own
    #                                    BTC<->LTC coverage
    #   ("XRP", "BTC"), ("XRP", "LTC")   XRP as the INPUT. XRP<->LTC completed
    #                                    OK=15 FAIL=0 on 2026-09-29 through
    #                                    atomic_swap_xrp.py, and BTC shares every
    #                                    function of that path
    #
    # THE TWO NOT ADDED, AND WHY -- ("BTC", "XRP") and ("LTC", "XRP") pay out in
    # XRP, and XRP_NETWORK_FEE_RESERVE DOES NOT EXIST. Adding them would enable a
    # pair that refuses every quote.
    #
    # WHICH IS ALREADY TRUE OF ("GRC", "XRP"), enabled 2026-09-26 and unquotable
    # since: quote_service.network_fee_reserve() raises for a missing reserve, and
    # the operator saw exactly that from their browser --
    #
    #     No quote: 'XRP_NETWORK_FEE_RESERVE'
    #
    # -- which is the bare KeyError repr that docstring was rewritten to prevent.
    # The message is better now; the pair is still broken. Setting that number is
    # a pricing decision and the operator's (rule 16), so it is REPORTED here
    # rather than guessed, and tests/test_allowed_pairs_are_serviceable.py fails
    # on it so it cannot be forgotten again.
    ALLOWED_PAIRS: ClassVar[set[tuple[str, str]]] = {
        ("GRC", "BTC"),
        ("BTC", "GRC"),
        ("GRC", "LTC"),
        ("LTC", "GRC"),
        ("XRP", "GRC"),
        ("GRC", "XRP"),
        ("BTC", "LTC"),
        ("LTC", "BTC"),
        ("XRP", "BTC"),
        ("XRP", "LTC"),
        # SOL -> GRC, enabled 2026-10-01 at the operator's request. ONE DIRECTION ONLY, and
        # the asymmetry is the whole point rather than an oversight.
        #
        # All three prerequisites the test below enforces are MET for this direction, checked
        # rather than assumed:
        #
        #   adapters     chains/registry builds SOL whenever SOL_RPC_URL is set, and GRC
        #                always. Measured on the operator's host: adapters ['GRC', 'SOL'].
        #   a USD price  services/pricing.IDS carries "SOL": "solana" (added 2026-09-29,
        #                inert until now) and "GRC": "gridcoin-research".
        #   a fee        the reserve is the TO asset's, and GRC_NETWORK_FEE_RESERVE exists
        #   reserve      (0.01). This is why the reverse direction is absent: there is no
        #                SOL_NETWORK_FEE_RESERVE, and inventing one is a pricing decision
        #                that is not mine (rule 16) -- adding ("GRC", "SOL") without it
        #                reproduces the GRC->XRP failure this section already records, a
        #                KeyError rendered into a customer's browser.
        #
        # AND ("GRC", "SOL") WOULD NEED A SEND PATH THAT DOES NOT EXIST. chains/solana.py
        # cannot sign -- it holds no keypair and imports nothing that could -- so a swap whose
        # TO asset is SOL could be quoted, could take a deposit, and could never be paid out.
        # That is worse than a refused quote: it strands a customer's coins in a swap the
        # terminal cannot complete.
        #
        # STILL NEEDS SOL_DEPOSIT_ACCOUNT to be set before a SOL swap can be CREATED --
        # swap_service refuses while it is empty, and solana_chain_check.py now says so in its
        # summary. A pair being allowed and a swap being creatable are two different gates.
        ("SOL", "GRC"),
        # SOL -> BTC and SOL -> LTC, enabled 2026-10-02 at the operator's request ("can we
        # please enable all trading pairs"). TWO of the nine unenabled pairs, not nine, and
        # the arithmetic for why is recorded here so nobody re-reads the request as unfinished.
        #
        # Measured against this file and services/pricing.IDS before adding anything:
        #
        #     asset  USD price  fee reserve
        #     BTC    yes        2e-05
        #     GRC    yes        0.01
        #     LTC    yes        0.001
        #     SOL    yes        MISSING
        #     XRP    yes        MISSING
        #
        # These two need NOTHING in config. SOL is the source, so the missing
        # SOL_NETWORK_FEE_RESERVE does not apply -- the reserve is the TO asset's, and both
        # BTC and LTC have one. Both destinations can sign (bitcoind and litecoind hold the
        # key), both assets are priced, and SOL takes deposits through the memo path that is
        # already live for SOL -> GRC.
        #
        # They read UNREACHABLE until BTC_RPC_* / LTC_RPC_* are exported in the shell that
        # starts the server, and that is the honest reading rather than a defect: the pair is
        # willing, the chain is absent. create_swap() refuses meanwhile.
        #
        # THE OTHER SEVEN ARE NOT A CONFIG CHANGE AND ARE NOT MINE. Every one has SOL or XRP
        # as its DESTINATION:
        #
        #     BTC -> SOL   GRC -> SOL   LTC -> SOL   XRP -> SOL
        #     BTC -> XRP   LTC -> XRP   SOL -> XRP
        #
        # and each needs TWO things that do not exist, not one:
        #
        #   a fee reserve   SOL_NETWORK_FEE_RESERVE / XRP_NETWORK_FEE_RESERVE. Inventing one
        #                   is a pricing decision and the operator's (rule 16).
        #                   tests/test_allowed_pairs_are_serviceable.py refuses to guess and
        #                   fails by name until the number exists -- which is the mechanism
        #                   that keeps "we enabled a pair nobody can trade" visible.
        #   a send path     chains/solana.py holds no keypair and imports nothing that could
        #                   sign. chains/xrp.py holds no signing key and
        #                   services/payout_service.py calls send_to_address() without the
        #                   arming token. So even WITH a reserve, a swap into either could be
        #                   quoted, could take a deposit, and could never be paid out.
        #
        # That second one is why these seven are not merely unfinished config. The paragraph
        # above already records it for ("GRC", "SOL") in the operator's own words -- "it
        # strands a customer's coins in a swap the terminal cannot complete" -- and ("GRC",
        # "XRP") is in this set TODAY as the live proof: it is allowed, it reads CANNOT
        # COMPLETE on both surfaces, and it has been broken since 2026-09-26.
        ("SOL", "BTC"),
        ("SOL", "LTC"),
    }
    # XRP. No default URL: a rippled endpoint is either your own server or a
    # public cluster, and guessing one would point this at somebody else's
    # machine. Unset means no XRP adapter is constructed at all.
    XRP_RPC_URL = _env("XRP_RPC_URL", "")
    # Must be 1. The XRP Ledger does not reorganize, so a payment is either in
    # a validated ledger or it is not -- there is no depth to accumulate, and
    # chains/xrp_units.py REFUSES any other value at construction rather than
    # letting every XRP deposit sit below an unreachable threshold forever.
    XRP_MIN_CONFIRMATIONS = _env_int("XRP_MIN_CONFIRMATIONS", "1")

    # THE ACCOUNT XRP DEPOSITS ARE PAID INTO, and it is a CUSTODY decision, which
    # is why it has no default and why an empty value refuses rather than
    # improvising. Every XRP swap shares this one account and is told apart by an
    # integer DestinationTag, so getting it wrong does not misroute one deposit --
    # it misroutes all of them, to an account this terminal may not hold the key
    # for. There is no safe guess, so there is no default (the same reasoning as
    # network_target.UNCONFIGURED_PORT, one level up: a value that decides where
    # money lands is not something to infer).
    #
    # Set it to an account you control. services/swap_service.py refuses to create
    # an XRP swap while it is empty, which is the failure you want: no swap, rather
    # than a swap whose deposit instruction points nowhere.
    XRP_DEPOSIT_ACCOUNT = _env("XRP_DEPOSIT_ACCOUNT", "").strip()

    # THE ACCOUNT SOL DEPOSITS ARE PAID INTO. Every word above applies unchanged -- one shared
    # account, no default, an empty value refuses rather than improvising -- with one difference
    # worth naming: the discriminator is a MEMO INSTRUCTION rather than an integer field on the
    # transaction, read by chains/solana_memo.py. services/swap_service.TAG_ATTRIBUTION carries
    # which is which, so neither this comment nor that table is the only place it is written.
    #
    # ADDED 2026-09-29, AND ITS ABSENCE WAS A REAL GAP FOR ABOUT AN HOUR. TAG_ATTRIBUTION was
    # wired to read `SOL_DEPOSIT_ACCOUNT` from config before config defined it, so
    # `config.get("SOL_DEPOSIT_ACCOUNT")` returned None and every SOL swap would have refused
    # with "SOL_DEPOSIT_ACCOUNT is not set" NO MATTER WHAT THE OPERATOR EXPORTED. That failure
    # reads as a configuration problem on their side and is missing code on ours -- the worst
    # shape a refusal can have, because the person who can see it cannot fix it.
    #
    # THE DIFFERENCE FROM XRP THAT MATTERS FOR CUSTODY: an XRP account is funded past a base
    # reserve and holds nothing else; a Solana deposit account is an ordinary keypair's public
    # key, and whoever holds that key holds every deposit between arrival and payout. Under the
    # brief-escrow model the operator described, that window is the whole exposure.
    SOL_DEPOSIT_ACCOUNT = _env("SOL_DEPOSIT_ACCOUNT", "").strip()

    RPC: ClassVar[dict[str, dict[str, object]]] = {
        "BTC": {
            "user": _env("BTC_RPC_USER", ""),
            "password": _env("BTC_RPC_PASS", ""),
            "host": _env("BTC_RPC_HOST", "127.0.0.1"),
            "port": _env_int("BTC_RPC_PORT", str(UNCONFIGURED_PORT)),
            "wallet": _env("BTC_RPC_WALLET", ""),
            "timeout": _env_float("BTC_RPC_TIMEOUT", "30"),
        },
        "LTC": {
            "user": _env("LTC_RPC_USER", ""),
            "password": _env("LTC_RPC_PASS", ""),
            "host": _env("LTC_RPC_HOST", "127.0.0.1"),
            "port": _env_int("LTC_RPC_PORT", str(UNCONFIGURED_PORT)),
            "wallet": _env("LTC_RPC_WALLET", ""),
            "timeout": _env_float("LTC_RPC_TIMEOUT", "30"),
        },
        # SOLANA'S ENTRY HAS DIFFERENT KEYS, AND THAT IS THE POINT.
        # The three above are Bitcoin JSON-RPC connections: user, password,
        # host, port, wallet, timeout. A Solana RPC endpoint is ONE URL with no
        # HTTP authentication and no wallet path, so forcing it into that shape
        # would mean four empty strings and a reassembly step that can silently
        # produce "http://:0". Each dict is built for the __init__ signature it
        # is splatted into -- which is the contract RPCAdapter already had --
        # and chains/registry.py is the single place that does the splatting.
        #
        # THERE IS DELIBERATELY NO DEFAULT URL. The three above default to
        # MAINNET ports, which this file's header says out loud. Doing the same
        # here would mean picking somebody's public cluster and defaulting to
        # mainnet on it; an unset URL instead makes every call refuse with a
        # message saying so, which is the loud version of the same fact.
        #
        # SOL_HOT_WALLET is a PUBLIC key. Nothing in chains/solana.py reads,
        # loads or derives a private key, and there is no environment variable
        # here for a keypair path -- unlike the Node bridge's
        # SOLANA_PAYER_KEYPAIR_PATH, which is what signs over there.
        "SOL": {
            "url": _env("SOL_RPC_URL", ""),
            "commitment": _env("SOL_RPC_COMMITMENT", "processed"),
            "timeout": _env_float("SOL_RPC_TIMEOUT", "30"),
            # The SPL mint to operate on, e.g. wGRC. Empty means native SOL.
            "mint": _env("SOL_SPL_MINT", ""),
            "hot_wallet": _env("SOL_HOT_WALLET", ""),
            "min_commitment_rank": _env_int("SOL_MIN_CONFIRMATIONS", "3"),
        },
        "GRC": {
            "user": _env("GRC_RPC_USER", ""),
            "password": _env("GRC_RPC_PASS", ""),
            "host": _env("GRC_RPC_HOST", "127.0.0.1"),
            "port": _env_int("GRC_RPC_PORT", str(UNCONFIGURED_PORT)),
            "wallet": _env("GRC_RPC_WALLET", ""),
            "timeout": _env_float("GRC_RPC_TIMEOUT", "30"),
        },
        # Its own shape again, matching XRPAdapter.__init__. An XRP endpoint is
        # one URL with no HTTP auth, no wallet path and no port of its own --
        # the same reason Config.RPC["SOL"] does not use the Bitcoin six.
        "XRP": {
            "url": XRP_RPC_URL,
            "min_confirmations": XRP_MIN_CONFIRMATIONS,
            "timeout": _env_float("XRP_RPC_TIMEOUT", "30"),
        },
    }
