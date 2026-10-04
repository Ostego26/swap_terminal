"""Is the desk's hot wallet distinct from the operator's own? One decision per chain.

Role: submodule (the function layer -- every function here is pure, takes
      already-read answers as arguments, and returns a verdict plus the sentence
      that explains it)
Reads: nothing. No environment, no file, no database, no socket. Every input is
      an argument, which is what lets a test assert every combination without a
      daemon (rule 10: the thing that decides is the smallest testable piece).
Writes: nothing
Can move funds: no. Nothing here signs, sends, derives a key, or names a
      passphrase. It does not even open the socket whose answers it judges --
      wallet_custody.py does that and hands the answers in.
Mainnet-safe: yes, trivially: it has no I/O at all. The mainnet REFUSAL lives in
      network_target.may_read_a_wallet(), one layer out, because refusing has to
      happen before a socket opens and this module never opens one.

WHY THIS EXISTS, AND WHAT IT CANNOT DO

The operator's instruction, 2026-10-03: "these two hot wallets are used by the
swap terminal machine NOT THE USER." This is a CUSTODIAL desk -- the customer
deposits to an address the desk owns and the desk pays out of its own inventory
-- so the deposit leg must move coins OUT of the customer's wallet and INTO the
desk's, and the payout leg must move coins OUT of the desk's and INTO the
customer's. Neither happens if the desk's wallet and the operator's own wallet
are the same wallet.

ON THIS HOST THEY ARE, AND THE CONFIGURATION IS WHY. Measured 2026-10-03 against
this tree, in an environment with nothing exported:

    BTC_RPC_WALLET -> ''        config.py:447
    LTC_RPC_WALLET -> ''        config.py:455
    GRC_RPC_WALLET -> ''        config.py:520

chains/base.RPCAdapter.url appends `/wallet/<name>` ONLY when that value is
non-empty, so an empty one addresses `http://host:port` with no wallet path --
which is the daemon's DEFAULT wallet, the same wallet an operator's own
`gridcoinresearchd getnewaddress` reaches. Verified by construction the same day:

    RPCAdapter(wallet="")         -> http://127.0.0.1:25715
    RPCAdapter(wallet="desk_hot") -> http://127.0.0.1:25715/wallet/desk_hot

THE HONEST LIMIT, AND IT IS THE WHOLE REASON THIS MODULE IS SHAPED THE WAY IT IS.
No RPC field, and nothing on any chain, says whose money a coin is. There is no
`isdesks` beside `ismine`. So this module can establish WHICH WALLET an endpoint
serves and whether that wallet is the one a bare CLI call reaches, and it CANNOT
establish that the coins in a named wallet were never the operator's own. Every
verdict that could be mistaken for the stronger claim says so in its own
sentence, and what_this_cannot_establish() states the limit once at the top of
the report, because a diagnostic that implies a proof it does not have is worse
than no diagnostic -- rule 17's "a hypothesis in the register of a measurement",
arriving as a green verdict.

FIVE STATES, NOT TWO, FOR THE SAME REASON services/payout_capacity.FundingVerdict
HAS THREE: "could not ask" is not an answer, and collapsing it into either answer
is how a gate comes to report "fine" for a question it never asked. The shape is
deliberately that function's and modules/address_authority.check_address()'s,
rather than a third vocabulary for one idea (rule 8).

ONE WALLET PER DAEMON IS NOT ALWAYS A DEFECT, AND THE VOCABULARY HAS TO SAY SO.
XRP and SOL deposits are attributed by a tag or a memo against ONE shared desk
account -- services/swap_service.payout_source_account() reads the same variable
as deposit_account() through TAG_ATTRIBUTION, and its docstring explains why at
length. So for those chains "the deposit account and the payout account are the
same" is the design and BY_DESIGN is the verdict, not a finding. The finding on
those chains is a payout whose DESTINATION is the desk's own account, which is a
different question and has its own function.
"""

from __future__ import annotations

from typing import NamedTuple

#: The desk's wallet or account was established as a DIFFERENT one from the
#: daemon's default wallet (BTC/LTC/GRC) or from the account on the other side of
#: the swap (SOL). It is NOT a statement about whose coins are in it.
SEPARATED = "SEPARATED"

#: Established that one wallet or account serves both the desk and whatever the
#: check compared it against. This is the state the operator's host is in on
#: BTC, LTC and GRC today, and it is what makes a deposit a self-transfer.
NOT_SEPARATED = "NOT SEPARATED"

#: The question does not apply: one desk account serving both directions is the
#: custodial design on the tag-attributed chains. Never a defect.
#:
#: "BY DESIGN" AND NOT "ONE DESK ACCOUNT, BY DESIGN", WHICH IS WHAT IT SAID FIRST.
#: Every state here is printed in report_block's label column, which is
#: LABEL_WIDTH = 16 wide, and the longer spelling rendered as
#:
#:     ONE DESK ACCOUN 0 of 6  (none)
#:
#: in wallet_custody.py's own tally -- a word cut mid-syllable with nothing saying
#: a tool did it, which is exactly the ambiguity report_block.clipped() exists to
#: refuse. The sentence beside the state already says "one desk account ... which
#: is the custodial design", so the column loses nothing by being short enough to
#: fit. tests/test_custody_separation.py pins every state against LABEL_WIDTH.
BY_DESIGN = "BY DESIGN"

#: Two values that have to name one account name two different ones. Always a
#: defect, and on XRP it is one that refuses at SIGNING time -- after a deposit
#: is confirmed and irreversible.
MISCONFIGURED = "MISCONFIGURED"

#: The desk's address is filed under its OWN pre-0.17 account, distinct from the
#: default account an operator's bare CLI and GUI reach. The question is ANSWERED
#: and answered affirmatively -- so this IS in wallet_custody.GOOD_STATES -- at
#: the strength this daemon family allows, which is weaker than SEPARATED and the
#: word is different for exactly that reason.
#:
#: WHY NOT JUST CALL IT SEPARATED. On BTC and LTC, SEPARATED means the DAEMON
#: REFUSES to cross the boundary: with two wallets loaded, a bare wallet RPC gets
#: rpc code -19 and cannot land in desk_hot by accident. A pre-0.17 account
#: enforces NOTHING. Coin selection ignores accounts, so the Gridcoin GUI will
#: spend a desk UTXO to fund an operator's send without asking, and one wallet is
#: one keyset behind one passphrase. Rendering both as SEPARATED would put a
#: weaker guarantee under a word an operator has already learned means the strong
#: one -- which is the vocabulary-stretching mistake DESK_OWNS exists because of,
#: made a second time.
#:
#: WHAT IT DOES ESTABLISH, and it is not nothing: every address the desk derived
#: is attributable to the swap it was derived for, by the daemon itself rather
#: than by this application's records, so desk coins and operator coins can be
#: told apart inside one wallet.dat. That is the honest answer to the operator's
#: 2026-10-04 requirement -- "we need a solution where we can do all of this, but
#: use one gui/wallet ... when it comes to gridcoin" -- and it needed no second
#: daemon, no second datadir, no config change and no restart, because
#: services/swap_service.py:475 has been deriving into `swap_{swap_id}` all along.
ATTRIBUTED = "ATTRIBUTED"

#: The payout wallet DOES hold the key for the deposit address it was asked
#: about. Correct for a custodial desk, and never a finding.
#:
#: ITS OWN WORD RATHER THAN SEPARATED/NOT SEPARATED, BECAUSE THAT VOCABULARY IS
#: INVERTED FOR THIS ONE QUESTION and the first draft got it wrong. "Does the
#: payout wallet hold the key for this swap's own deposit address" wants YES on a
#: custodial desk -- the desk derived that address with getnewaddress in the
#: wallet it pays out of -- so rendering the yes as NOT SEPARATED put a correct
#: answer in the same bucket as the defect and made the exit code non-zero for it.
#: The brief for this work says it in one line: one desk account is CORRECT for a
#: custodial desk, do not report it as a defect. Two questions, two vocabularies,
#: is the honest shape; one vocabulary stretched over both is how a report comes
#: to disagree with the design it is inspecting.
DESK_OWNS = "DESK OWNS IT"

#: The payout wallet does NOT hold the key for the deposit address it was asked
#: about. Always a defect on a custodial desk: a deposit it cannot see is a
#: deposit it cannot spend.
NOT_THE_DESKS = "NOT THE DESK'S"

#: Nobody answered. NEVER a green verdict by default: the reason is always
#: carried, and a caller that renders this as "fine" has reintroduced the defect
#: this module exists to remove.
#:
#: IT USED TO READ "Nobody answered, or the question cannot be asked on this
#: chain", AND THAT SECOND CLAUSE MOVED OUT to CANNOT_BE_ASKED on 2026-10-04.
#: Kept as a sentence rather than deleted because the drift is the point (rule 1):
#: the two clauses have opposite remedies, this state carried both for a day, and
#: wallet_custody.py's closing block told the operator to change a variable for a
#: line that has none. What is still TRUE here is a daemon that is down, a refused
#: login, an unloaded wallet, or a question whose INPUT was not supplied (no
#: --swap, no second endpoint named) -- every one of which a different run can
#: answer. A question the RPC surface cannot carry is the other state.
NOT_ESTABLISHED = "NOT ESTABLISHED"

#: The question cannot be asked on this daemon family AT ALL, and asking again
#: with different configuration changes nothing. Not a pass -- it is kept out of
#: wallet_custody.GOOD_STATES so the exit code stays non-zero -- but a DIFFERENT
#: failure from NOT_ESTABLISHED, and the difference is the whole reason it exists.
#:
#: SPLIT OUT OF NOT_ESTABLISHED ON 2026-10-04, because that state's own docstring
#: four lines up conflated two things in one sentence: "Nobody answered, or the
#: question cannot be asked on this chain." Those have opposite remedies. A daemon
#: that did not answer is fixed by starting it, fixing a login, or exporting a
#: variable; a daemon whose RPC surface does not CARRY the field is fixed by
#: nothing an operator can type.
#:
#: MEASURED, AND IT IS WHY THIS IS A DEFECT RATHER THAN A NICETY. On the
#: operator's host on 2026-10-04, `wallet_custody.py --swap s_539d922e9ef0a5d8`
#: printed 8 checks, 2 of them NOT ESTABLISHED, and closed with
#:
#:     Each line names the variable to change.
#:
#: That sentence is FALSE for the GRC wallet line: getwalletinfo carries no
#: `walletname` field to compare against, so there is no variable. An operator
#: reading that footer goes looking for one, which is the round trip rule 14
#: exists to prevent: the screen told them to do something impossible.
#:
#: THE REASON WHY IS IN GRIDCOIN_NO_WALLETNAME_EVIDENCE just above, spelled once.
#: This docstring cited a help-text grep until later the same day, when a control
#: run refuted that grep -- see that constant for the numbers.
#:
#: THE ONLY ROUTE THAT CAN ANSWER IT is a SECOND Gridcoin daemon with its own
#: datadir on its own port, because one-wallet-per-datadir means a second wallet
#: REQUIRES a second datadir. That is the same single missing thing the
#: `GRC operator daemon` line is blocked on, which is why the two GRC lines on
#: that run were one blocker rendered as two. The `why` carried with this state
#: says so, and says it names a daemon rather than a variable.
#:
#: FITS report_block.LABEL_WIDTH at 15 characters, pinned by
#: tests/test_custody_separation.py like every other state -- the constraint that
#: turned "ONE DESK ACCOUNT, BY DESIGN" into "BY DESIGN".
CANNOT_BE_ASKED = "CANNOT BE ASKED"

#: WHY Gridcoin cannot answer which wallet an endpoint serves, in ONE place.
#:
#: SPELLED SIX TIMES UNTIL 2026-10-04 -- here, twice in wallet_custody.py, twice
#: more below, and once in gridcoin_credentials.py -- and all six cited the SAME
#: REFUTED MEASUREMENT, which is rule 8's "a bug with a delay on it" with the
#: delay expired. The claim every one of them made:
#:
#:     gridcoinresearchd -testnet help | grep -iE '^(createwallet|loadwallet|listwallets|unloadwallet)'
#:       -> (none of the multiwallet RPCs exist on this build)
#:
#: THAT COMMAND ESTABLISHES NOTHING, and a control run on the operator's host on
#: 2026-10-04 is what showed it. The same grep, anchored the same way, over RPCs
#: this repo calls in PRODUCTION against that daemon:
#:
#:     grep -icE '^(listunspent|validateaddress|getwalletinfo|sendtoaddress)'  -> 0
#:     grep -icE '^(getaccountaddress|setaccount|listaccounts|sendfrom|move)'  -> 0
#:     grep -icE '^(createwallet|loadwallet|listwallets|unloadwallet)'         -> 0
#:
#: Zero for all three, including the line that must be non-zero: chains/gridcoin
#: calls `listunspent` (modules/atomic_grc_client.py:375) and `validateaddress`
#: answered a live query minutes earlier. So `^` matches nothing against this
#: daemon's help output, every conclusion drawn from that grep was a FALSE
#: NEGATIVE, and the second line above is no evidence about accounts either.
#:
#: WHAT ACTUALLY ESTABLISHES IT is behavioral, which is what this repo's
#: verification principle asks for and what the grep was standing in for. Three
#: readings, all from the daemon rather than from its help text:
#:
#:   getwalletinfo     reply carries no `walletname` field at all
#:                     (wallet_custody.py's own run, 2026-10-04)
#:   getaddressinfo    Method not found (rpc code -32601)
#:                     (chains/base.py records this, measured 2026-10-03)
#:   validateaddress   returns a pre-0.17 `account` field
#:                     (operator's host, 2026-10-04: an address came back with
#:                      account="Beacon Address for CPID ...")
#:
#: Each is a pre-0.17 wallet and together they are why there is no `walletname`
#: to compare. The CONCLUSION survived the refutation; the cited evidence did not.
GRIDCOIN_NO_WALLETNAME_EVIDENCE = (
    "this daemon family predates multiwallet, established from the daemon's BEHAVIOR rather than "
    "from its help text: getwalletinfo's reply carries no `walletname` field at all, "
    "getaddressinfo answers Method not found (rpc code -32601), and validateaddress returns a "
    "pre-0.17 `account` field. One wallet per datadir, no -rpcwallet, no /wallet/<name> endpoint"
)

#: Every state, so a caller can render a legend and a test can assert the set is
#: closed. Ordered worst-known-first is deliberately NOT done: these are not
#: ranked, because NOT_ESTABLISHED is not "between" two answers -- it is the
#: absence of one.
#:
#: EVERY ONE OF THEM FITS report_block.LABEL_WIDTH, which is what lets
#: wallet_custody.py print the state as the label column with no truncation.
#: tests/test_custody_separation.py pins that, because the alternative is a state
#: cut mid-word with nothing saying a tool did it -- which is what
#: "ONE DESK ACCOUNT, BY DESIGN" did before it became "BY DESIGN".
STATES = (SEPARATED, NOT_SEPARATED, BY_DESIGN, DESK_OWNS, NOT_THE_DESKS, MISCONFIGURED,
          NOT_ESTABLISHED, CANNOT_BE_ASKED, ATTRIBUTED)


class CustodyVerdict(NamedTuple):
    """One state from STATES, and the sentence an operator acts on.

    `why` is never empty, including for SEPARATED. That is the difference from a
    bare boolean and it is the point: a SEPARATED verdict has to carry what it
    did NOT establish, or a reader takes it for the stronger claim.
    """

    state: str
    why: str


def wallet_label(asset: str, configured_wallet: str) -> str:
    """How to NAME the wallet an endpoint addresses, in a status line. One spelling.

    MERGED FROM TWO SITES ON 2026-10-03 (rule 8). Both of these rendered the same
    phrase for the same question, and neither said what it meant:

        workers/common.endpoint_lines()      wallet = rpc["wallet"] or "(default wallet)"
        services/admin_view._endpoint_text() wallet = getattr(...) or "(default wallet)"

    "(default wallet)" is accurate and tells an operator nothing. It is the one
    line on a worker's startup banner and on the admin page's Chains table where
    the custody question is visible, and it rendered as a parenthetical that reads
    like a default being fine. What it actually means is that this endpoint has no
    `/wallet/<name>` path, so the daemon routes the call to whichever wallet it
    serves by default -- the same wallet an operator's own CLI gets with no
    `-rpcwallet` argument.

    So the phrase now names the VARIABLE that would separate them, because the
    operator reads the screen and not config.py (rule 14), and it stays short
    enough for a banner: the long form is wallet_custody.py's job.
    """
    name = (configured_wallet or "").strip()
    if name:
        return name
    return f"(default -- {asset}_RPC_WALLET unset, so this is the wallet a bare CLI call reaches)"


def script_chain_verdict(
    asset: str,
    configured_wallet: str,
    walletinfo: object,
    read_error: str = "",
    loaded_wallets: tuple[str, ...] | None = None,
) -> CustodyVerdict:
    """BTC, LTC and GRC: which wallet does the desk's endpoint actually serve?

    THE INPUTS ARE ANSWERS, NOT A CONNECTION. `walletinfo` is whatever
    `getwalletinfo` returned, `read_error` is the string form of whatever it raised
    instead, and `loaded_wallets` is `listwallets`. Both RPCs are reads.
    wallet_custody.py makes the calls -- after network_target.may_read_a_wallet()
    has refused a non-test port -- so that this decision can be asserted on with
    seeded inputs rather than against a daemon (rule 10, and the repository's
    "verify by behavior" principle needs a function it can seed).

    WHY `walletname` AND NOT THE CONFIG VALUE. Asserting on config text would be
    the defect CLAUDE.md names last: "never accept 'the code contains a check for
    X' as evidence X is enforced." `GRC_RPC_WALLET` being set says what this
    process ASKED for; `getwalletinfo().walletname` is what the daemon IS SERVING
    on that endpoint, and the two can disagree -- which is its own finding and
    gets MISCONFIGURED rather than a guess.

    `listwallets` IS WHAT MAKES THE POSITIVE VERDICT MORE THAN A NAME, and this is
    the part a config read could never reach. Bitcoin Core refuses a bare wallet
    RPC with rpc code -19 ("Wallet file not specified") when more than one wallet
    is loaded, so a daemon with the desk wallet AND another wallet loaded is one
    where an operator's bare `getnewaddress` cannot silently land in the desk's
    wallet -- the daemon itself enforces the separation. A daemon with ONE loaded
    wallet, even a named one, routes a bare call straight to it. Same name, two
    very different states, and only the second answer distinguishes them.

    EMPTY `configured_wallet` IS NOT_SEPARATED WHATEVER THE WALLET IS CALLED. With
    no `/wallet/<name>` in the URL the daemon picks, and so does the operator's
    CLI; whether that wallet happens to have a name is not the question.
    """
    if read_error:
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"{asset}: getwalletinfo did not answer ({read_error}), so WHICH wallet this endpoint "
            f"serves was not established. This is not a clean verdict and must not be read as one -- "
            f"a daemon that is down, a refused login and an unloaded wallet all land here, and none "
            f"of them says anything about custody"
        ))
    if not isinstance(walletinfo, dict):
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"{asset}: getwalletinfo returned {type(walletinfo).__name__} rather than an object, so "
            f"which wallet this endpoint serves was not established. Treat this as a defect in the "
            f"caller or in the daemon's reply, not as a configuration problem"
        ))
    if "walletname" not in walletinfo:
        # CANNOT_BE_ASKED AND NOT NOT_ESTABLISHED, and this branch is why that
        # state exists. The old sentence here already SAID the distinction --
        # "asking again changes nothing, which is a different problem from a
        # daemon that did not answer" -- while returning the state that means
        # exactly "a daemon did not answer", so wallet_custody.py tallied it
        # beside a line that a second run fixes and closed with "Each line names
        # the variable to change". There is no variable; see CANNOT_BE_ASKED.
        return CustodyVerdict(CANNOT_BE_ASKED, (
            f"{asset}: getwalletinfo answered, but its reply carries no `walletname` field at all, "
            f"so which wallet this endpoint serves CANNOT be established from this daemon -- not "
            f"now and not with different configuration. {GRIDCOIN_NO_WALLETNAME_EVIDENCE}, and so "
            f"no `walletname` to compare. THERE IS NO VARIABLE TO CHANGE. Two routes could answer "
            f"it and neither is a setting: a SECOND {asset} daemon with its own datadir on its own "
            f"port (one wallet per datadir means a second wallet REQUIRES a second datadir -- and "
            f"copy block data only, never wallet.dat), which is the same missing thing the "
            f"operator-daemon line is blocked on; or the pre-0.17 `account` field, which the `desk "
            f"account` line BELOW now reads and which needs neither. That second route is MEASURED "
            f"rather than proposed as of 2026-10-04 -- a desk deposit address on the operator's "
            f"host came back under account 'swap_s_539d922e9ef0a5d8' while 88 operator addresses "
            f"sat in the default account -- so the separation was already present in this one "
            f"wallet and nothing was reading it -- the `desk account` line carries it whenever a "
            f"swap is in scope. THIS line stays "
            f"CANNOT BE ASKED because the WALLET question specifically has no answer here"
        ))
    serving = str(walletinfo["walletname"])
    asked_for = (configured_wallet or "").strip()
    shown = serving or "(unnamed)"
    if not asked_for:
        return CustodyVerdict(NOT_SEPARATED, (
            f"{asset}: {asset}_RPC_WALLET is unset, so this process addresses the daemon with no "
            f"/wallet/<name> path and the daemon routes to the wallet it serves by DEFAULT -- "
            f"{shown}. That is the same wallet an operator's own CLI reaches with no -rpcwallet, so "
            f"the desk's deposit addresses and the operator's own coins are in ONE wallet: a "
            f"customer deposit from that wallet is a self-transfer and moves no custody. Set "
            f"{asset}_RPC_WALLET to a wallet created for the desk"
        ))
    if serving != asked_for:
        return CustodyVerdict(MISCONFIGURED, (
            f"{asset}: this process asked for wallet {asked_for!r} and the daemon says it is serving "
            f"{shown!r}. Those have to be one wallet and they are two names, so which wallet the "
            f"desk's addresses and payouts belong to was NOT established -- do not treat either name "
            f"as the answer until the daemon and {asset}_RPC_WALLET agree"
        ))
    others = tuple(name for name in (loaded_wallets or ()) if name != serving)
    if others:
        enforced = (
            f" The daemon also has {len(others)} other wallet(s) loaded ({', '.join(n or '(unnamed)' for n in others)}), "
            f"so a bare wallet RPC with no -rpcwallet is REFUSED by the daemon itself (rpc code -19, "
            f"'Wallet file not specified') rather than silently landing here. That is the strongest "
            f"separation an RPC read can show."
        )
    else:
        enforced = (
            " WEAKER THAN IT LOOKS: listwallets reports this as the ONLY loaded wallet, so a bare "
            "wallet RPC with no -rpcwallet still routes here -- an operator's own `getnewaddress` "
            "lands in the desk's wallet. Load the operator's wallet alongside it, or keep the "
            "operator's coins on a different daemon."
            if loaded_wallets is not None else
            " listwallets was not read, so whether a bare CLI call also reaches this wallet was not "
            "established."
        )
    return CustodyVerdict(SEPARATED, (
        f"{asset}: {asset}_RPC_WALLET={asked_for} and the daemon confirms it is serving {shown}, "
        f"reached through /wallet/{asked_for}.{enforced} WHAT THIS DOES NOT ESTABLISH: that the coins "
        f"in this wallet were never the operator's own. No RPC field says whose money a coin is, so a "
        f"named wallet separates the KEYS and nothing here can audit the funding"
    ))


def deposit_address_verdict(
    asset: str, swap_id: str, deposit_address: str, owns: bool | None, ownership_why: str = ""
) -> CustodyVerdict:
    """Does the payout wallet hold the key for THIS swap's own deposit address?

    `owns` IS chains/base.RPCAdapter.address_ownership()'s THREE-VALUED ANSWER,
    passed through rather than recomputed: True, False, or None for "nobody
    answered". That method already carries the 2026-10-01 measurement for why the
    reason is a return value -- a transport failure read as "this chain has no
    `ismine` field" and was reported as a fact. `ownership_why` is its `why`.

    ismine=True IS CORRECT HERE AND IS NOT THE DEFECT. On a custodial desk the
    deposit address is derived by `getnewaddress` in the desk's own wallet
    (services/swap_service.deposit_account() -> derive_deposit_address()), so the
    desk owning it is the design. What it does NOT establish is that the deposit
    moved custody: when the payout wallet IS the daemon's default wallet, a
    deposit sent FROM that same wallet is a self-transfer. That is the state
    script_chain_verdict() reports, and it is why these two lines are read
    together rather than either one alone.

    ismine=False IS the defect, and a loud one: the swap's deposit address is not
    in the wallet this process pays out of, so a deposit into it cannot be spent
    by the payout path. The commonest cause is a wallet variable changed after the
    swap row was written.

    DESK_OWNS AND NOT_THE_DESKS RATHER THAN SEPARATED AND NOT_SEPARATED, because
    the separation vocabulary is INVERTED for this question and the first draft of
    this function used it. See DESK_OWNS's own comment for the measurement: a
    correct answer landed in the same bucket as the defect and made the exit code
    non-zero for being right.
    """
    label = f"{asset} swap {swap_id}" if swap_id else asset
    if owns is None:
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"{label}: whether the payout wallet holds the key for its deposit address "
            f"{deposit_address} was NOT established ({ownership_why or 'no reason was returned'}). "
            f"This is 'nobody answered', NOT 'not ours' -- if the reason above is a connection error "
            f"the question is still answerable from a shell with the right variables exported"
        ))
    if owns:
        return CustodyVerdict(DESK_OWNS, (
            f"{label}: the payout wallet reports ismine=true for its own deposit address "
            f"{deposit_address}, which is CORRECT for a custodial desk -- the desk derived that "
            f"address in its own wallet. It does NOT establish that the deposit moved custody: where "
            f"the wallet OR account holding this address is the same one the operator's own CLI and "
            f"GUI reach, a deposit sent from there is a self-transfer and the only thing that moved "
            f"was the fee. The other {asset} lines in this report say which of those applies"
        ))
    return CustodyVerdict(NOT_THE_DESKS, (
        f"{label}: the payout wallet reports ismine=false for its own deposit address "
        f"{deposit_address}. On a custodial desk that is a DEFECT rather than separation: the desk "
        f"derived this address with getnewaddress in the wallet it pays out of, so a deposit it "
        f"cannot see is a deposit it cannot spend. Check whether {asset}_RPC_WALLET changed after "
        f"this swap row was written"
    ))


#: How the desk's own deposit-address verdict translates back into the tri-state
#: `ismine` answer the cross-daemon comparison needs.
#:
#: DERIVED FROM THE STATE RATHER THAN READ TWICE (rule 8). wallet_custody.py has
#: already asked the DESK's endpoint `ismine` for this swap's deposit address and
#: rendered the answer through deposit_address_verdict(), so the cross-daemon line
#: translates that recorded state back instead of opening a second socket to the
#: same daemon and asking the same question. Two reads of one fact are two chances
#: for one report to disagree with itself.
#:
#: NOT_ESTABLISHED and every other state map to None through .get(), which is the
#: honest answer: "the desk's half was not read" is not "the desk does not own it".
DESK_OWNERSHIP_FROM_STATE = {DESK_OWNS: True, NOT_THE_DESKS: False}


def cross_daemon_ownership_verdict(  # noqa: PLR0913, PLR0917 -- checked: these six are the question. The two chains of custody (which address, from which swap), the endpoint being named in the sentence, and the two daemons' answers plus the operator daemon's reason. Bundling them into an object would add a type without removing an argument, which is the same decision chains/base.RPCAdapter.__init__ records.
    asset: str,
    swap_id: str,
    deposit_address: str,
    operator_endpoint_label: str,
    operator_owns: bool | None,
    operator_why: str = "",
    desk_owns: bool | None = None,
) -> CustodyVerdict:
    """GRC: does the OPERATOR's own daemon hold the key for the DESK's deposit address?

    THE ONLY SEPARATION QUESTION GRIDCOIN CAN ANSWER, and the reason is measured
    rather than assumed. On the operator's Gridcoin v5.5.1.0 testnet daemon,
    2026-10-03:

        (see GRIDCOIN_NO_WALLETNAME_EVIDENCE -- this cited a help-text grep that a
         control run refuted on 2026-10-04; the evidence is behavioral now)
          -> (none of the multiwallet RPCs exist on this build)

    So Gridcoin has ONE wallet per datadir: no `-rpcwallet`, no `/wallet/<name>`
    endpoint, no `walletname` field. script_chain_verdict() therefore answers
    NOT ESTABLISHED on GRC and always will -- not for want of configuration, but
    because the field it reads does not exist on this daemon family. Six of
    wallet_custody.py's seven checks answered on 2026-10-03 and that was the
    seventh.

    SO THE QUESTION IS ASKED FROM THE OTHER DIRECTION, AND THE EVIDENCE IS
    BEHAVIORAL RATHER THAN CONFIGURAL. With no name to ask for, ask the daemon
    that holds the OPERATOR's coins whether the DESK's deposit address is `ismine`.
    A `false` from it is an observation about a KEY -- the operator's wallet does
    not hold the key the desk derived -- which is strictly stronger than any
    reading of a config value, and it is available on the pre-0.17 RPC surface
    Gridcoin actually has (`validateaddress` carries `ismine`; measured 2026-10-01
    in chains/base.address_ownership()'s own docstring).

    THE DESK ADDRESS COMES FROM THE DATABASE AND NEVER FROM A WALLET WRITE. It is
    the swap's own `deposit_address` -- for swap s_539d922e9ef0a5d8,
    moaSBv8gcwXRnmQhxJJAjUvXMd542jsNNz, derived by the terminal in the GRC wallet
    it was pointed at. `getnewaddress` would have answered the same question and is
    a WALLET WRITE: it derives and stores a key. A read-only diagnostic that wrote
    a key to ask whether a wallet is separate would be paying for the answer with
    the thing it is auditing.

    FOUR DISTINCT ANSWERS, and the two that matter are opposite proofs:

      operator says false, desk says true or was not read -> SEPARATED. The daemon
          holding the operator's coins does not hold this key.
      operator says false AND desk says false -> NOT THE DESK'S. Separated from the
          operator, and a defect anyway: an address NEITHER wallet holds is a
          deposit nobody can spend. Not folded into SEPARATED, because a green line
          over an unspendable deposit is the shape of failure this whole module
          exists to refuse.
      operator says true -> NOT SEPARATED. The operator's own wallet holds the key
          the desk derived. Two independently created wallets do not share a key,
          so this is one wallet -- or a wallet.dat that was COPIED, which is the
          hazard docs/hot_wallet_separation_runbook.md states first and loudest,
          and which no other check in this tree could detect.
      nobody answered, or there is no GRC deposit leg -> NOT ESTABLISHED, with the
          reason. Never a green default.

    WHAT A `false` DOES NOT ESTABLISH, and both limits are in the sentence rather
    than in this docstring, because the operator reads the screen (rule 14):

      - that no OTHER wallet of the operator's holds the key. One daemon was asked,
        about one datadir. A second datadir, a hardware wallet, a watch-only
        import, an old backup -- none of them were asked and none of them could be.
      - that the desk's coins were never the operator's. Funding is unaudited here
        exactly as it is everywhere else in this module: no chain and no RPC says
        whose money a coin is.
    """
    label = f"{asset} swap {swap_id}" if swap_id else asset
    if not deposit_address:
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"{asset}: no {asset} deposit address was available, so no cross-daemon ownership "
            f"question was asked and no second socket was opened. This check needs --swap with a "
            f"{asset} DEPOSIT leg: the desk address it asks about is the swap's own "
            f"`deposit_address` from swap_terminal.db, because the alternative -- getnewaddress -- "
            f"is a WALLET WRITE and a read-only tool must not pay for an answer with a new key"
        ))
    if operator_owns is None:
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"{label}: whether the OPERATOR's own daemon holds the key for the desk's deposit "
            f"address {deposit_address} was NOT established "
            f"({operator_why or 'no reason was returned'}). "
            f"This is 'nobody answered', NOT 'not theirs' -- and on Gridcoin it is the "
            f"ONLY separation question available, because this daemon family has no multiwallet "
            f"RPCs and so no `walletname` to compare. Nothing about GRC custody follows from this "
            f"line in either direction"
        ))
    if operator_owns:
        both = (
            " The desk's own endpoint answers ismine=true for it too, so BOTH daemons hold the key: "
            "that is one wallet reached through two endpoints, or a wallet.dat that was copied."
            if desk_owns
            else " The desk's own half of this comparison was not read, so whether BOTH hold it is "
                 "not established -- but one `ismine=true` from the operator's daemon is already the "
                 "finding, whatever the desk answers."
        )
        return CustodyVerdict(NOT_SEPARATED, (
            f"{label}: the OPERATOR's own daemon at {operator_endpoint_label} reports ismine=true for "
            f"the desk's deposit address {deposit_address}, so the operator's wallet holds the key the "
            f"desk derived.{both} Two independently created wallets do not share a key -- a customer "
            f"deposit into this address moves coins from the operator's wallet to the operator's "
            f"wallet, which is a self-transfer that costs a fee and moves no custody. Point the desk "
            f"at a daemon with its own -datadir (docs/hot_wallet_separation_runbook.md), and NEVER "
            f"copy wallet.dat into it: copying the wallet copies these keys and reproduces exactly "
            f"this state"
        ))
    if desk_owns is False:
        return CustodyVerdict(NOT_THE_DESKS, (
            f"{label}: NEITHER daemon holds the key for this swap's deposit address "
            f"{deposit_address} -- the operator's own daemon at {operator_endpoint_label} answers "
            f"ismine=false, and so does the desk's own endpoint. The operator half is the separation "
            f"this check looks for, but a deposit address the DESK cannot see is a deposit the desk "
            f"cannot spend, which is a defect in its own right and the louder of the two. Check "
            f"whether the desk's GRC endpoint changed datadir after this swap row was written"
        ))
    confirmed = (
        " The desk's own endpoint answers ismine=true for it, which is CORRECT for a custodial desk "
        "and is the other half of this proof: the key is in the desk's wallet and not in the "
        "operator's."
        if desk_owns
        else " The desk's own half of this comparison was not read, so that the DESK can spend this "
             "deposit is not established here -- see the deposit-address line above for that question."
    )
    return CustodyVerdict(SEPARATED, (
        f"{label}: the OPERATOR's own daemon at {operator_endpoint_label} reports ismine=false for "
        f"the desk's deposit address {deposit_address}, so the wallet holding the operator's coins "
        f"does NOT hold the key the desk derived.{confirmed} That is behavioral evidence about a KEY "
        f"rather than a reading of a config value, and on Gridcoin it is the only separation evidence "
        f"available: this daemon family has no multiwallet RPCs, so there is no `walletname` to ask "
        f"for. WHAT THIS DOES NOT ESTABLISH: that no OTHER wallet of the operator's holds this key -- "
        f"ONE daemon and ONE datadir were asked, and a second datadir, a restored backup or a "
        f"watch-only import was not and could not be. Nor that the desk's coins were never the "
        f"operator's: funding is unaudited, here as everywhere in this report"
    ))


def xrp_desk_account_verdict(
    configured_account: str, derived_account: str, derive_error: str = ""
) -> CustodyVerdict:
    """XRP: does XRP_DEPOSIT_ACCOUNT name the account the payout seed controls?

    ONE DESK ACCOUNT IS THE DESIGN AND IS NOT A FINDING. An XRP customer's deposit
    lands in XRP_DEPOSIT_ACCOUNT and an XRP payout is debited from it --
    services/swap_service.payout_source_account() reads the SAME variable through
    the SAME table as deposit_account(), and says why at length. Reporting that as
    a custody defect would be this tool disagreeing with the design it is
    inspecting.

    THE CASE THAT COSTS MONEY IS "SET TO SOMETHING ELSE", which is why the
    comparison is a line of its own. chains/xrp_signing.derive_and_check() refuses
    a seed paired with an account it does not control -- but it refuses at PAYOUT
    time, after the customer's deposit is confirmed and irreversible.
    xrp_payout_account.agreement_line() is the operator-facing spelling of the
    same comparison and names the same refusal; this is the verdict form, for a
    report that has four other chains to print beside it.

    THE SEED IS NEVER AN INPUT HERE. This function takes the DERIVED ACCOUNT, a
    public value, so the seed cannot reach it, cannot be printed from it, and
    cannot appear in a pasted report. xrp_payout_account.derived_account() is what
    reads the environment and it never puts the seed in its own output either.
    """
    configured = (configured_account or "").strip()
    derived = (derived_account or "").strip()
    if derive_error:
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"XRP: the desk's own account could not be derived ({derive_error}), so whether "
            f"XRP_DEPOSIT_ACCOUNT names an account this terminal can sign for was NOT established. "
            f"The seed itself is never printed by this tool"
        ))
    if not configured:
        return CustodyVerdict(NOT_ESTABLISHED, (
            "XRP: XRP_DEPOSIT_ACCOUNT is unset, so there is no desk account to compare against "
            "anything. Every XRP swap is refused while it is empty, so this is a configuration gap "
            "rather than a custody finding"
        ))
    if not derived:
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"XRP: XRP_DEPOSIT_ACCOUNT={configured}, and no account was derived from a signing seed "
            f"in this process, so whether this terminal holds the key for it was NOT established. "
            f"Export XRP_PAYOUT_SECRET_SEED in the shell you run this from, or accept that the "
            f"pairing is unverified here"
        ))
    if configured == derived:
        return CustodyVerdict(BY_DESIGN, (
            f"XRP: XRP_DEPOSIT_ACCOUNT={configured} is exactly the account the payout seed controls. "
            f"One desk account takes deposits IN and has payouts debited OUT of it, which is the "
            f"custodial design (services/swap_service.payout_source_account()), NOT a defect. The "
            f"custody question on this chain is whether a PAYOUT left it -- see the payout line"
        ))
    return CustodyVerdict(MISCONFIGURED, (
        f"XRP: XRP_DEPOSIT_ACCOUNT={configured} but the payout seed in this process controls "
        f"{derived}. Deposits would land in one account and payouts would be signed for another, so "
        f"chains/xrp_signing.derive_and_check() refuses the payout -- at PAYOUT time, after the "
        f"customer's deposit is confirmed and irreversible. Fix the variable, not the check"
    ))


def xrp_payout_destination_verdict(desk_account: str, swap_id: str, payout_address: str) -> CustodyVerdict:
    """XRP: did this swap's payout actually leave the desk's account?

    THE CHECK THE 2026-10-03 SWAP NEEDED. A payout whose destination is the desk's
    own account moves nothing: the Payment is validated, the fee is paid, the
    ledger shows tesSUCCESS, and the balance comes back to where it started. That
    renders identically to a successful payout in every surface this tree has.

    WHAT IT CANNOT ESTABLISH, and the limit is absolute rather than a gap to fill
    later: whether an address the desk does NOT control belongs to the customer.
    The XRP Ledger has no owner field, chains/xrp.XRPAdapter.owns_address()
    returns None by design for exactly that reason, and a desk cannot tell its
    operator's second faucet account from a stranger's. So the honest verdict for
    "it left" is that it left, and nothing more.
    """
    desk = (desk_account or "").strip()
    paid = (payout_address or "").strip()
    label = f"XRP swap {swap_id}" if swap_id else "XRP"
    if not desk or not paid:
        missing = "the desk's account" if not desk else "this swap's payout address"
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"{label}: {missing} is empty, so whether the payout left the desk's account was NOT "
            f"established. Nothing was read from the ledger"
        ))
    if desk == paid:
        return CustodyVerdict(NOT_SEPARATED, (
            f"{label}: the payout address IS the desk's own account {desk}, so this payout moved no "
            f"custody -- it debited and credited one account and paid a fee to do it. The ledger "
            f"reports tesSUCCESS for it and every surface in this tree renders it as paid"
        ))
    return CustodyVerdict(SEPARATED, (
        f"{label}: the payout address {paid} is NOT the desk's account {desk}, so this payout left "
        f"the desk. WHAT THIS DOES NOT ESTABLISH: that {paid} is the customer's. The XRP Ledger has "
        f"no owner field and this terminal cannot tell a customer's account from a second account "
        f"the operator holds, so 'it left the desk' is the whole of the claim"
    ))


def solana_account_verdict(deposit_account: str, hot_wallet: str) -> CustodyVerdict:
    """SOL: are SOL_DEPOSIT_ACCOUNT and SOL_HOT_WALLET set, and are they two accounts?

    DIFFERENT FROM XRP, which is why it is a different function rather than the
    same one with a flag. XRP has ONE variable by design and
    services/swap_service.SHARED_ACCOUNT_PAYOUT_ASSETS is frozenset({"XRP"}) alone;
    Solana has TWO -- a deposit account that a memo attributes against, and a hot
    wallet the payout is debited from -- so on this chain two accounts is the
    configured shape and one account is a choice somebody made.

    config.py's own comment carries the custody difference that makes this worth a
    line: "an XRP account is funded past a base reserve and holds nothing else; a
    Solana deposit account is an ordinary keypair's public key, and whoever holds
    that key holds every deposit between arrival and payout."

    NO NETWORK READ AT ALL. Both values are public keys from config, compared as
    strings. Whether either account exists on a cluster, and what it holds, is
    sol_payout_preview.py's question and deliberately not this one -- a custody
    report that needs a reachable cluster reports nothing on an unreachable one.
    """
    deposit = (deposit_account or "").strip()
    hot = (hot_wallet or "").strip()
    if not deposit and not hot:
        return CustodyVerdict(NOT_ESTABLISHED, (
            "SOL: neither SOL_DEPOSIT_ACCOUNT nor SOL_HOT_WALLET is set, so there are no accounts to "
            "compare. No SOL swap can be created or paid in this state, so this is a configuration "
            "gap rather than a custody finding"
        ))
    if not deposit or not hot:
        missing = "SOL_DEPOSIT_ACCOUNT" if not deposit else "SOL_HOT_WALLET"
        present = "SOL_HOT_WALLET" if not deposit else "SOL_DEPOSIT_ACCOUNT"
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"SOL: {missing} is unset and {present} is set, so whether the deposit account and the "
            f"payout account are two different accounts was NOT established. Set both -- they are "
            f"separate custody decisions and neither has a default"
        ))
    if deposit == hot:
        return CustodyVerdict(NOT_SEPARATED, (
            f"SOL: SOL_DEPOSIT_ACCOUNT and SOL_HOT_WALLET are the SAME account ({deposit}). Unlike "
            f"XRP, Solana has two variables here precisely so they can be two accounts, so one "
            f"account is a choice rather than the design: every deposit lands in the account that "
            f"pays every payout, and whoever holds that one key holds both"
        ))
    return CustodyVerdict(SEPARATED, (
        f"SOL: deposits land in {deposit} and payouts are debited from {hot} -- two different "
        f"accounts. WHAT THIS DOES NOT ESTABLISH: whose keypairs those are. Both values are public "
        f"keys read from config, nothing on a Solana cluster says who holds a key, and this tool "
        f"never opens a keypair file"
    ))


def what_this_cannot_establish() -> tuple[str, ...]:
    """The limits of the whole report, stated BEFORE any verdict is printed.

    AT THE TOP RATHER THAN IN A FOOTNOTE, because the failure this guards against
    is a reader taking a SEPARATED line for proof that the desk's coins were never
    the operator's. That claim is not available from any RPC on any of these five
    chains, and a diagnostic whose limits are at the bottom has already been
    misread by the time they are reached.

    A TUPLE OF LINES RATHER THAN ONE PARAGRAPH, so the entry point can print them
    through report_block without re-wrapping prose, and so a test can assert that
    the ownership limit is one of them rather than searching a blob.
    """
    return (
        "no chain and no RPC says WHOSE money a coin is. There is no `isdesks` beside `ismine`, so "
        "nothing below can establish that the coins in a wallet were never the operator's own.",
        "a NAMED wallet separates the KEYS the desk signs with from the default wallet an operator's "
        "CLI reaches. It says nothing about how that wallet was funded.",
        "an address this desk does not control is not thereby the CUSTOMER's. The desk cannot tell a "
        "customer's account from a second account the operator holds, on any of these five chains.",
        # NAMES BOTH NON-PASSING ABSENCES, and it named only the first until
        # 2026-10-04. A reader who had been told NOT ESTABLISHED is the state for
        # "we could not tell" would read CANNOT BE ASKED as something else --
        # possibly as a pass, which is the one reading this whole module exists to
        # prevent.
        f"{NOT_ESTABLISHED} and {CANNOT_BE_ASKED} are never a pass, and they are different "
        f"absences: the first is a daemon that did not answer or an input not supplied, which "
        f"another run can fix, and the second is a question this daemon family cannot carry at "
        f"all, which no configuration fixes. Both print their reason and both exit non-zero -- a "
        f"green verdict is never the default here.",
    )


def gridcoin_account_label(swap_id: str) -> str:
    """The pre-0.17 account a desk deposit address for `swap_id` is filed under.

    ONE SPELLING, DERIVED FROM THE ONE THAT ALREADY EXISTS. services/swap_service
    .py:475 builds `label = f"swap_{swap_id}"` and hands it to
    chains/base.RPCAdapter.get_new_address(), which calls `getnewaddress <label>`.
    On a pre-0.17 daemon that first argument IS the account, so this function and
    that line must agree forever or this check silently reports MISCONFIGURED for
    every correctly-derived address. tests/test_custody_separation.py asserts the
    two against each other rather than against a literal, which is what rule 8
    asks when one rule has two sites and only one of them can be the authority.
    """
    return f"swap_{swap_id}"


def gridcoin_account_verdict(
    asset: str,
    swap_id: str,
    deposit_address: str,
    ownership,
) -> CustodyVerdict:
    """Is the desk's deposit address in its OWN account, inside the one wallet?

    THIS IS THE ANSWER TO "one gui/wallet" FOR GRIDCOIN, and it replaces a route
    that required hardware the operator does not want to run. Before 2026-10-04
    the only GRC separation route this tree knew was a SECOND daemon with a second
    datadir, because `getwalletinfo` carries no `walletname` on this build and
    that was the only field anything looked at. The operator asked for one wallet
    instead, and the measurement that followed showed the separation was already
    there:

        validateaddress <swap s_539d922e9ef0a5d8's deposit address>
          -> "account": "swap_s_539d922e9ef0a5d8"
        validateaddress <the wallet's CPID beacon address>
          -> "account": "Beacon Address for CPID 09ff..."
        getaddressesbyaccount ""
          -> 88 addresses, and NEITHER of the above among them

    THE ADDRESSES THEMSELVES ARE NOT TRANSCRIBED HERE, deliberately, and the
    ACCOUNT values are -- because the accounts are the finding and the addresses
    are already in `swaps.deposit_address` where a reader can get them without
    trusting a comment. tests/test_address_literals_are_valid.py counts every
    address-shaped literal in the tree.

    MEASURED, BECAUSE THE FIRST VERSION OF THIS PARAGRAPH OVERSTATED IT: removing
    these two addresses changed that count by ZERO. The scanner reads string
    literals in CODE, so prose in a docstring was never in scope -- of fourteen
    address mentions this change first added, five were counted (all of them real
    string literals in the test file, replaced with derived fixtures from
    tests/valid_addresses.py) and nine were not. The count was 60 of its 60
    ceiling at 164e1d0 and is 60 now, so this work contributed none of the drift
    its own note records (48 on 2026-09-27, so twelve arrived from elsewhere).
    These two came out on rule 8 grounds alone -- the measurement belongs in one
    place and an address a reader cannot verify is not worth transcribing.

    Three accounts in one wallet.dat: the desk's per-swap accounts, the default
    account holding the operator's own 88, and the beacon's. Gridcoin has been
    filing them apart since the first swap and nothing was reading it.

    `ownership` IS A chains/base.AddressOwnership, NOT A CONNECTION. The caller
    makes the RPC; this function decides. That is rule 10 -- the decision is the
    smallest testable piece and the tests seed it with no daemon at all -- and it
    is what lets the repository's "verify by behavior" principle have something to
    assert on.

    WHY AN EMPTY `account` IS NOT ONE ANSWER. "" is ambiguous between "the DEFAULT
    account, which is where the operator's own addresses live" and "this daemon has
    no accounts at all", and those are opposite verdicts -- NOT SEPARATED is a
    defect to act on, CANNOT BE ASKED names no variable. The discriminator is WHICH
    RPC answered, which `ownership.why` carries: `validateaddress` is the pre-0.17
    method and DOES return `account`, so "" from it means the default account;
    `getaddressinfo` is the 0.18+ method, has no `account` field at all (it has
    `labels`), and "" from it means the question was never asked. Reading "" as one
    thing is how a report comes to disagree with the daemon it inspected.

    THE ACCOUNT ENFORCES NOTHING AND THE VERDICT SAYS SO. See ATTRIBUTED's note:
    coin selection ignores accounts, so the GUI can spend a desk UTXO, and one
    wallet is one keyset behind one passphrase. A caller that renders ATTRIBUTED
    as the same guarantee BTC's SEPARATED carries has undone the reason the two
    words are different.
    """
    expected = gridcoin_account_label(swap_id)
    answered_by_pre_017 = "validateaddress" in (ownership.why or "")
    limit = (
        f"WHAT THIS DOES NOT ESTABLISH: a pre-0.17 account enforces NOTHING. Coin selection "
        f"ignores accounts, so the {asset} GUI can spend a desk UTXO to fund your own send "
        f"without asking, and one wallet is one keyset behind one passphrase. This is "
        f"attribution, not the daemon-enforced refusal BTC and LTC get from rpc code -19"
    )

    if ownership.verdict is None:
        return CustodyVerdict(NOT_ESTABLISHED, (
            f"{asset} swap {swap_id}: nobody answered for {deposit_address} ({ownership.why}), so "
            f"which account it is filed under was not established. This is NOT 'not separated' -- "
            f"a down daemon and a refused login both land here and neither says anything about "
            f"custody"
        ))
    if not ownership.verdict:
        return CustodyVerdict(NOT_THE_DESKS, (
            f"{asset} swap {swap_id}: the wallet reports ismine=false for {deposit_address}, which "
            f"is this swap's own deposit address. A deposit the desk cannot see is a deposit it "
            f"cannot spend, so this is a defect regardless of any account -- and it means the "
            f"address in the database was not derived by this wallet"
        ))
    if not ownership.account:
        if answered_by_pre_017:
            return CustodyVerdict(NOT_SEPARATED, (
                f"{asset} swap {swap_id}: {deposit_address} is in the DEFAULT account (validateaddress "
                f"answered with an empty `account`), which is where a bare getnewaddress and the "
                f"{asset} GUI both land. The desk's deposit addresses and the operator's own coins "
                f"are in one account as well as one wallet, so nothing distinguishes them. Expected "
                f"account {expected!r} -- services/swap_service.py derives with "
                f"`getnewaddress {expected}` and a pre-0.17 daemon files that as the account, so an "
                f"empty one here means that derivation did not happen or was undone"
            ))
        return CustodyVerdict(CANNOT_BE_ASKED, (
            f"{asset} swap {swap_id}: the reply for {deposit_address} carries no `account` field "
            f"({ownership.why}), so which account it is filed under cannot be established from this "
            f"daemon. getaddressinfo is the 0.18+ method and has no `account` field at all -- it "
            f"carries `labels` instead -- so this is a daemon NEWER than the accounts API rather "
            f"than one missing a setting. THERE IS NO VARIABLE TO CHANGE; on such a daemon the "
            f"separation question is answered by a named WALLET instead, which is what BTC and LTC "
            f"do here"
        ))
    if ownership.account == expected:
        return CustodyVerdict(ATTRIBUTED, (
            f"{asset} swap {swap_id}: {deposit_address} is filed under account {ownership.account!r}, "
            f"which is this swap's own and NOT the default account the operator's bare CLI and GUI "
            f"reach. The daemon itself attributes it -- services/swap_service.py derived it with "
            f"`getnewaddress {expected}` and a pre-0.17 daemon reads that first argument as the "
            f"account -- so desk coins and operator coins are distinguishable inside one wallet.dat, "
            f"with no second daemon and no second datadir. {limit}"
        ))
    return CustodyVerdict(MISCONFIGURED, (
        f"{asset} swap {swap_id}: {deposit_address} is filed under account {ownership.account!r} but "
        f"this swap's addresses should be under {expected!r}. Two values that have to name one "
        f"account name two different ones, so something relabeled it after derivation -- a "
        f"`setaccount`, or an address reused from elsewhere in the wallet. Until it is reconciled, "
        f"an account-based reading of which coins are the desk's is wrong for this address"
    ))
