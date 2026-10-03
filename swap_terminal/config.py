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
    # THE FOUR RESERVES, AND AS OF 2026-10-03 TWO OF THEM ARE MEASURED.
    #
    # What this number is: what the desk EXPECTS one payout on this chain to cost
    # it. It is NOT taken out of the customer's payout -- `sendtoaddress(address,
    # amount)` delivers `amount` exactly and the fee comes from the wallet's own
    # inputs, measured to the last digit on the operator's host 2026-10-01
    # (services/quote_service.get_network_fee_reserve() carries that arithmetic).
    # It is a BOOKKEEPING figure, and it is what makes a swap's margin knowable.
    #
    # Until today all three that existed were bare defaults with no provenance
    # between them, and the operator asked the obvious question: what should they
    # be? show_payout_fees.py was built to answer it from their own host rather
    # than from anybody's recollection, and it did.
    #
    #   BTC  0.00002   UNMEASURED. Nothing has ever been paid out in BTC, and
    #                  Bitcoin's fee is a market rather than a constant, so a fixed
    #                  figure here is wrong most days by construction. This is the
    #                  one that still needs work, and the work is a fee-rate read
    #                  (estimatesmartfee) rather than a number typed in.
    #   LTC  0.001     UNMEASURED for the same reason -- no LTC payout has ever
    #                  been made. Left as it was rather than guessed at.
    #   GRC  0.001     MEASURED on the operator's host 2026-10-03, 7 of 7 broadcast
    #                  payouts, `gettransaction` on each: mean 0.00100000 with low
    #                  and high IDENTICAL. Gridcoin charges a flat 0.001.
    #                  WAS 0.01, which was ten times the fee -- and the 2026-10-01
    #                  hand reading of a single wallet that first said so turned out
    #                  to generalize to every GRC payout this desk has ever made.
    #                  Changed at the operator's explicit instruction, 2026-10-03:
    #                  "set src to measured 0.001 -- 7/7, zero variance, your host".
    #   XRP  0.00001   MEASURED on the operator's host 2026-10-03, read from their
    #                  own rippled: server_info.validated_ledger.base_fee_xrp, 10
    #                  drops. Printed by show_payout_fees.py as "READ FROM THE
    #                  SERVER, not the tree". It had NO value at all before this,
    #                  which is why every quote paying out in XRP refused.
    #
    # THE XRP FIGURE IS A FLOOR, NOT A CEILING, and the distinction is the chain's
    # rather than this desk's: the fee actually paid is autofilled by xrpl-py at
    # submit time and RISES WITH LOAD. 10 drops is what an unloaded ledger charges
    # and has charged for years. A fee escalation would cost more than this books,
    # which is the opposite direction from GRC's old 0.01 and is the direction that
    # understates a cost rather than overstating it.
    BTC_NETWORK_FEE_RESERVE = _env_float("BTC_NETWORK_FEE_RESERVE", "0.00002")
    LTC_NETWORK_FEE_RESERVE = _env_float("LTC_NETWORK_FEE_RESERVE", "0.001")
    GRC_NETWORK_FEE_RESERVE = _env_float("GRC_NETWORK_FEE_RESERVE", "0.001")
    # ADDING THIS LINE IS A POSTURE CHANGE AND IT IS THE OPERATOR'S, MADE 2026-10-03.
    #
    # Until now ("GRC", "XRP") was in ALLOWED_PAIRS and every quote for it refused
    # here, which is what the operator saw from their browser on 2026-10-02. With a
    # reserve, that quote PRICES -- and on a host where XRP_PAYOUT_SECRET_SEED is
    # exported the swap can then be created and paid. So this line turns GRC -> XRP
    # from a pair that refused into a pair that trades.
    #
    # tests/test_allowed_pairs_are_serviceable.py predicted its own failure here and
    # said what to do about it: "When XRP_NETWORK_FEE_RESERVE is set, this test fails
    # and KNOWN_UNQUOTABLE should be emptied -- a tolerated break that outlives its
    # fix is a lie in the test suite." Done in the same commit (rule 19).
    #
    # WHAT THIS DOES NOT DO: ("BTC", "XRP") and ("LTC", "XRP") are still NOT in
    # ALLOWED_PAIRS. The reserve was one of two things they needed and it is no
    # longer the blocker; enabling them is a separate decision and still the
    # operator's.
    XRP_NETWORK_FEE_RESERVE = _env_float("XRP_NETWORK_FEE_RESERVE", "0.00001")
    # SOL, MEASURED ON THE OPERATOR'S HOST 2026-10-03, and it is the first time
    # anything in this tree checked the figure against a cluster.
    #
    #   0.000005000 SOL (5000 lamports), getFeeForMessage at commitment finalized,
    #   over the 150-byte MESSAGE of a one-signature native transfer, priced
    #   against a real blockhash from getLatestBlockhash.
    #
    # chains/solana_units.py:513's SIGNATURE_FEE_LAMPORTS = 5_000 calls itself "a
    # reference value; getFeeForMessage is the authority" -- and the authority
    # agrees with it. That is worth distinguishing from a constant nobody checked:
    # the number did not change, the EVIDENCE for it did, and only one of those two
    # states can be relied on.
    #
    # PER SIGNATURE, NOT PER BYTE, so a one-signature transfer costs this regardless
    # of size. A priority fee would be ON TOP and this desk sends none.
    #
    # WHY THIS LINE IS NOT WHAT MAKES A *->SOL PAIR WORK, said here because the
    # reserve is the cheapest of the prerequisites and the easiest to mistake for
    # the whole job. With a reserve and nothing else, a quote PRICES and the swap
    # then refuses, which is the quote-then-cannot-pay sequence
    # tests/test_allowed_pairs_are_serviceable.py spent three days declining to
    # open for XRP. What a SOL payout additionally needs is the signing path armed
    # (chains/solana.py's can_spend, and the keypair path behind it) and a funded
    # SOL_HOT_WALLET. The page refuses the pair until those hold, which is what
    # makes adding this safe rather than a promise.
    SOL_NETWORK_FEE_RESERVE = _env_float("SOL_NETWORK_FEE_RESERVE", "0.000005")
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
        # THE THREE DIRECTIONS THAT PAY OUT IN SOL, enabled 2026-10-03 on the
        # operator's instruction: "whoa we have to be able to swap TO SOL too".
        #
        # UNTIL TODAY SOL COULD ONLY EVER BE AN INPUT, and the reason was an ABSENCE
        # rather than a setting: chains/solana.py held no keypair, imported nothing
        # that could sign, and send_to_address() raised. That is no longer true --
        # the signing path exists, SolanaAdapter.can_spend is derived from
        # SOL_PAYOUT_KEYPAIR_PATH the way XRP's is derived from its seed, and
        # services/payout_service.broadcast_payout() passes the arming token. So the
        # asymmetry this set carried for four days has ended and these three close it.
        #
        # ALL FOUR PREREQUISITES ARE MET FOR EACH, checked rather than assumed:
        #
        #   adapters    chains/registry builds SOL whenever SOL_RPC_URL is set, GRC
        #               always, and BTC/LTC when their credentials are exported. A
        #               chain that is absent reads OFFLINE rather than trading.
        #   a USD price services/pricing.IDS carries SOL, GRC, BTC and LTC.
        #   a reserve   SOL_NETWORK_FEE_RESERVE exists as of today and is MEASURED --
        #               0.000005, read from the operator's own cluster through
        #               getFeeForMessage. It is the TO asset's that matters, and SOL
        #               is the TO asset in all three.
        #   a payout    SOL can sign when armed, and REFUSES when it is not. That is
        #               the condition these three rest on and the one worth naming
        #               twice.
        #
        # WHAT HAPPENS ON AN UNARMED HOST, WHICH IS EVERY CHECKOUT AND EVERY TEST RUN:
        # can_spend is False, chains/registry.why_cannot_pay_out() names
        # SOL_PAYOUT_KEYPAIR_PATH and SOL_HOT_WALLET, and
        # services/pair_view.pair_serviceability() marks all three UNAVAILABLE -- so
        # the customer form never offers them and no deposit is taken against a
        # payout that cannot fire. Enabling a pair is not arming it; these two
        # switches are deliberately separate.
        #
        # AND THE BROADCAST IS STILL A PROPOSAL (rule 16). No transaction from this
        # path has ever reached a cluster -- api.devnet.solana.com answers 403 from
        # the environment this was written in, re-measured 2026-10-03 -- so the
        # serialization is verified byte-for-byte against @solana/web3.js and the
        # BROADCAST is verified against nothing. The path also refuses off devnet by
        # genesis hash, with no flag that turns that off, so mainnet cannot even be
        # previewed against. The first real send is the operator's and should be the
        # smallest amount that clears the rent floor the preview prints.
        ("GRC", "SOL"),
        ("BTC", "SOL"),
        ("LTC", "SOL"),
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
        # SOL_HOT_WALLET is a PUBLIC key, and nothing in chains/solana.py reads,
        # loads or derives a private key -- still true, and now checked rather
        # than claimed: tests/test_solana_adapter.py::
        # test_the_module_references_no_keypair_anywhere tokenizes that file and
        # refuses the names Keypair, secret_key, from_secret_key, sign,
        # sign_message and partial_sign.
        #
        # THE REST OF THIS COMMENT WAS HALF RIGHT AND IS CORRECTED, 2026-10-02.
        # It read: "there is no environment variable here for a keypair path --
        # unlike the Node bridge's SOLANA_PAYER_KEYPAIR_PATH, which is what
        # signs over there."
        #
        # THE CLAUSE ABOUT THE NODE BRIDGE WAS AND IS TRUE, established by
        # reading it rather than by recalling it: grc-sol-swap/abstergo_exchange/
        # server.js:143 loads that keypair, and sendSolPayout() at :245 builds a
        # SystemProgram.transfer and calls sendAndConfirmTransaction with it --
        # a real signed transfer, which its own module header declares ("Can
        # move funds: YES"). (services/solana.js does NOT sign: it imports
        # Keypair and never uses it, and passes `owner: userAddress` as a string
        # to a Serum order with `price: 1, // This should be dynamic`. A reader
        # looking there for the signer finds nothing, which is why the sentence
        # names server.js.)
        #
        # THE CLAUSE ABOUT THIS TREE IS THE ONE THAT WENT WRONG, because a
        # Python payout path landed. SOL_PAYOUT_KEYPAIR_PATH is now read -- by
        # chains/solana_signing.py, at call time, after an exact-string arming
        # token has already matched, and on devnet only. It is deliberately NOT
        # a field in this dict: Config reads the environment at class-definition
        # time, so a key path here would be baked into every process that
        # imports config, including the read-only deposit watcher. Nothing in
        # this file names a key for any chain, which is the property worth
        # keeping; "no such variable exists anywhere" is not, and was the half
        # that stopped being true.
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
