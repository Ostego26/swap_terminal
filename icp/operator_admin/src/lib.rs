//! The operator's posture authority, with a console it serves itself.
//!
//! Role: canister (the operator admin surface the generated Candid UI could not be)
//! Reads: its own stable state. No chain, no ledger, no outcall, no timer.
//! Writes: its own stable state, and only from a CONTROLLER principal. Every write
//!       appends an audit entry naming the caller, the field, and both values.
//! Can move funds: NO. It holds no key, calls no ledger, makes no outcall, and has
//!       no method that produces a signature. The worst a wrong value here can do
//!       is make this canister disagree with the terminal -- which today it always
//!       does, because nothing reads it yet. See NOT WIRED below.
//! Mainnet-safe: it cannot spend and it cannot reach anything. Whether its CONTENTS
//!       are safe is a different question and the operator's: these are the numbers
//!       that decide what trades.
//!
//! =========================================================================
//! WHY THIS EXISTS. OPERATOR, TWICE: "the operator page should be moved to the
//! canister's candid UI"
//! =========================================================================
//!
//! They asked on 2026-10-07 and again on 2026-10-08, and got a measurement instead
//! of a canister both times. The measurement was right as far as it went:
//!
//!   THE DASHBOARD CANNOT MOVE. Every number on /admin comes from
//!   runtime/swap_terminal.db or from a loopback JSON-RPC call to a daemon on
//!   127.0.0.1. A canister can do neither -- no filesystem, and an outcall cannot
//!   reach loopback. Balances, swap rows, payout rows, the obligation floor and the
//!   peg check are all on the wrong side of that line and stay in Flask.
//!
//!   THE AUTHORITY CAN. Which pairs may trade, the fee, how many confirmations each
//!   chain needs, whether a worker is armed -- those are CONFIGURATION the terminal
//!   reads, and a canister is a better place to keep them than an environment
//!   variable: one writer, caller-gated, with an audit trail by construction rather
//!   than by remembering to log.
//!
//! And then the honest objection, which is what this file is the answer to: the
//! GENERATED Candid UI is a method list. It renders argument types and raw variants,
//! with no layout, no labels, no ordering and no explanation -- so "move the
//! operator page there" would have traded a readable page for a worse one.
//!
//! =========================================================================
//! SO THE CANISTER SERVES ITS OWN CONSOLE, AND THAT CHOICE HAS A REASON
//! =========================================================================
//!
//! `http_request` is a query method the replica's HTTP gateway routes GETs to, so
//! this canister answers http://<canister-id>.localhost:4943 with real HTML it
//! renders itself -- the same Pip-Boy palette as swap_terminal/static/styles.css,
//! the same state vocabulary, every figure labeled with what it decides.
//!
//! WHAT IT IS NOT: an asset canister with a JavaScript frontend. That was the other
//! option and it was rejected for two measured reasons. It needs @dfinity/agent,
//! which means npm and a build step -- and this repository has neither; styles.css's
//! own header says "NO BUILD STEP AND NO FRAMEWORK. This is a Flask app served by
//! gunicorn; there is no npm toolchain here and this file adds none." And a page
//! that loads an agent from a CDN makes a local admin console depend on the public
//! internet, which is the opposite of what a loopback-bound replica is for.
//!
//! THE CONSOLE IS READ-ONLY, AND THAT IS A SECURITY DECISION RATHER THAN A LIMIT.
//! Requests arriving through the HTTP gateway are ANONYMOUS -- the gateway does not
//! carry the caller's principal -- so a form that wrote would be an unauthenticated
//! write path into the thing that decides what trades. Writes therefore stay on the
//! Candid/agent path where the operator's own identity signs the call, and the
//! console's job is to make that call obvious: every field renders the exact
//! `dfx canister call` line that changes it, ready to paste.
//!
//! =========================================================================
//! NOT WIRED, AND THAT IS RULE 16'S LINE RATHER THAN AN OMISSION
//! =========================================================================
//!
//! Nothing in swap_terminal/ reads this canister. The terminal still takes
//! ALLOWED_PAIRS, DEFAULT_FEE_BPS and the *_MIN_CONFIRMATIONS from its environment.
//! Making it read from here instead changes what gets traded and whether a pair may
//! trade at all, which is live posture and the operator's call -- and it introduces
//! a failure mode that has to be designed rather than discovered: what posture does
//! the terminal hold when the replica is down? Fail closed (trade nothing) and a
//! stopped container stops the desk; fail open (last known) and the authority is a
//! cache. That is a decision with money behind it.
//!
//! So this canister is the surface, deployable and inspectable today, with the
//! values seeded from the terminal's current configuration at install. The console
//! says so at the top of every page, because a page that looks authoritative and is
//! not is worse than no page.

use candid::{CandidType, Principal};
use serde::Deserialize;
use std::cell::RefCell;
use std::fmt::Write as _;

// =============================================================================
// THE STATE
// =============================================================================

/// One tradeable direction. A PAIR AND NOT A SET, because direction decides which
/// wallet pays: ("BTC","GRC") spends the desk's GRC, and ("GRC","BTC") spends its
/// BTC. The terminal's Config.ALLOWED_PAIRS is a set of ordered tuples for exactly
/// that reason, and this mirrors its shape so a future reader comparing the two is
/// comparing like with like.
#[derive(CandidType, Deserialize, Clone, Debug, PartialEq, Eq)]
pub struct Pair {
    pub from: String,
    pub to: String,
}

/// Everything this canister is the authority for, if anything ever reads it.
#[derive(CandidType, Deserialize, Clone, Debug, Default)]
pub struct Posture {
    pub pairs: Vec<Pair>,
    /// Basis points the desk keeps, subtracted from the payout. 150 = 1.5%.
    pub fee_bps: u16,
    /// Per-asset confirmation thresholds, as (asset, blocks).
    ///
    /// A LIST AND NOT A MAP because candid has no map type and a vec of tuples is
    /// what every ICP interface uses for this; `confirmations_for()` below is the
    /// one reader, so the linear scan is over at most a handful of entries.
    pub confirmations: Vec<(String, u32)>,
    /// Whether each payout chain is armed. Absent means NOT armed: this list says
    /// what is turned on, never what is turned off, so a forgotten entry fails
    /// closed.
    pub armed: Vec<String>,
}

/// One change, kept forever. APPEND-ONLY AND NEVER PRUNED: this is the record of
/// who moved the numbers that decide what trades, which CLAUDE.md rule 7 puts in
/// the same category as the evidence ledger -- "never delete evidence to save
/// space". A canister that outgrows its memory holding this is a canister that
/// changed posture tens of thousands of times, and that is a fact somebody should
/// have to look at rather than lose.
#[derive(CandidType, Deserialize, Clone, Debug)]
pub struct AuditEntry {
    /// Nanoseconds since the epoch, from `ic_cdk::api::time()`.
    pub at: u64,
    /// The principal that made the call. NOT a name: a name is something this
    /// canister would have to be told and could be told wrongly.
    pub by: String,
    pub field: String,
    pub was: String,
    pub now: String,
    /// The caller's own one-line reason. Required, because a posture change with no
    /// stated reason is the thing nobody can reconstruct six weeks later.
    pub note: String,
}

#[derive(Default)]
struct State {
    posture: Posture,
    audit: Vec<AuditEntry>,
}

thread_local! {
    static STATE: RefCell<State> = RefCell::new(State::default());
}

// =============================================================================
// THE DECISIONS, as functions, callable with no replica (rule 10)
// =============================================================================

/// An asset code this canister will accept. 2-6 ASCII uppercase letters or digits.
///
/// DELIBERATELY NOT AN ALLOWLIST OF THE SIX CHAINS THE TERMINAL SERVES. This
/// canister is not the place that knows which chains have adapters -- the terminal
/// is, and `chains/registry.build_adapters()` refuses an unconfigured one by name.
/// An allowlist here would be a second copy of that vocabulary (rule 8) which could
/// only ever be more stale than the first, and whose disagreement would show up as
/// a pair the console accepted and the terminal ignored.
///
/// What it DOES refuse is anything that could not be an asset code at all, because
/// those are typos rather than disagreements: lowercase, punctuation, empty, or
/// long enough to be a sentence.
pub fn validate_asset(code: &str) -> Result<(), String> {
    if code.len() < 2 || code.len() > 6 {
        return Err(format!(
            "{code:?} is {} characters; an asset code is 2 to 6. Nothing was changed",
            code.len()
        ));
    }
    if !code.chars().all(|c| c.is_ascii_uppercase() || c.is_ascii_digit()) {
        return Err(format!(
            "{code:?} is not uppercase ASCII letters and digits. The terminal keys every \
             adapter, reserve and threshold on the uppercase form, so a lowercase entry here \
             would silently match nothing. Nothing was changed"
        ));
    }
    Ok(())
}

/// A direction this canister will accept.
pub fn validate_pair(pair: &Pair) -> Result<(), String> {
    validate_asset(&pair.from)?;
    validate_asset(&pair.to)?;
    if pair.from == pair.to {
        return Err(format!(
            "{} -> {} is the same asset on both sides, which is not a swap. Nothing was changed",
            pair.from, pair.to
        ));
    }
    Ok(())
}

/// The desk's fee, in basis points.
///
/// THE CEILING IS A GUARD AGAINST A TYPO, NOT A POLICY, AND THE FIRST NUMBER I
/// PICKED DID NOT DO THAT JOB. It was 2000 (20%), justified in this very comment as
/// catching the one-keystroke slip from 150 to 1500 -- which 2000 lets straight
/// through. The test asserting 1500 is refused failed on the constant, which is the
/// cheapest possible way to find out that a guard and its stated reason disagree.
///
/// So the ceiling is set from the thing it has to catch: it must be BELOW 1500,
/// because 150 -> 1500 is the slip, and above any fee this desk would plausibly
/// charge. 1000 bps is 10% -- six and a half times the current 150 -- which leaves
/// room for a real change while refusing the extra digit.
///
/// IT IS NOT A BUSINESS LIMIT AND THE REFUSAL SAYS SO. If the desk really charges
/// more than 10%, this constant is what to change, in a commit somebody reads,
/// rather than something to work around at a prompt.
pub const FEE_BPS_CEILING: u16 = 1000;

pub fn validate_fee_bps(bps: u16) -> Result<(), String> {
    if bps > FEE_BPS_CEILING {
        return Err(format!(
            "{bps} bps is {:.1}%, above this canister's ceiling of {FEE_BPS_CEILING} bps \
             ({:.0}%). That ceiling exists to catch a misplaced digit, not to set policy -- if \
             the desk really charges more than that, the ceiling is what to change, in a commit \
             somebody reviews. Nothing was changed",
            f64::from(bps) / 100.0,
            f64::from(FEE_BPS_CEILING) / 100.0,
        ));
    }
    Ok(())
}

/// A confirmation threshold.
///
/// ZERO IS REFUSED AND THAT IS THE ONE THAT MATTERS. A threshold of 0 credits a
/// deposit the moment it is seen in a mempool, before any block -- so a customer
/// could be paid out of a transaction that is then replaced, and the desk would
/// have sent real coins for coins that never arrived. The terminal's own
/// ICP_MIN_CONFIRMATIONS is 1 and means FINAL rather than a depth, which is the
/// smallest honest value any chain here uses.
pub fn validate_confirmations(asset: &str, blocks: u32) -> Result<(), String> {
    validate_asset(asset)?;
    if blocks == 0 {
        return Err(format!(
            "0 confirmations for {asset} would credit a deposit that is in no block yet, so a \
             payout could go out against a transaction that is later replaced. The smallest \
             honest value is 1. Nothing was changed"
        ));
    }
    if blocks > 1000 {
        return Err(format!(
            "{blocks} confirmations for {asset} is not a threshold, it is a halt: no deposit \
             would ever credit. Nothing was changed"
        ));
    }
    Ok(())
}

/// The note that must accompany every change.
pub fn validate_note(note: &str) -> Result<(), String> {
    let trimmed = note.trim();
    if trimmed.len() < 4 {
        return Err(
            "every change needs a one-line reason of at least 4 characters, which goes in the \
             audit entry beside the old and new values. A posture change nobody wrote a reason \
             for is the one nobody can reconstruct later. Nothing was changed"
                .to_string(),
        );
    }
    if trimmed.len() > 300 {
        return Err(format!(
            "the reason is {} characters; 300 is the ceiling. It is a line, not a document",
            trimmed.len()
        ));
    }
    Ok(())
}

/// HTML-escape. EVERY string that reaches the console goes through this.
///
/// NOT PARANOIA ABOUT A LOOPBACK PAGE. The audit log stores a caller-supplied
/// `note`, and the console renders it; without escaping, a note containing a script
/// tag would execute in the operator's browser on a page served from a canister
/// origin that can also call this canister's update methods. The replica binds
/// 127.0.0.1 only, which limits who can reach the page -- it does not limit what a
/// note already in the log can do once the page is opened.
pub fn escape(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for c in raw.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#39;"),
            _ => out.push(c),
        }
    }
    out
}

/// Nanoseconds since the epoch, as a readable UTC timestamp.
///
/// HAND-ROLLED BECAUSE A DATE CRATE IS A DEPENDENCY THIS DOES NOT EARN. chrono and
/// time both pull a tree of features into a wasm module that pays for its own size
/// in cycles, for one line of display. The arithmetic is the civil-from-days
/// algorithm, which is exact for every date this canister will ever render.
pub fn format_time(nanos: u64) -> String {
    let secs = nanos / 1_000_000_000;
    let days = (secs / 86_400) as i64;
    let tod = secs % 86_400;
    // Civil from days, epoch-shifted to 0000-03-01 so leap years fall at the end.
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    format!(
        "{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z",
        tod / 3600,
        (tod % 3600) / 60,
        tod % 60
    )
}

/// The confirmation threshold for `asset`, or None if none is set.
///
/// None RATHER THAN A DEFAULT, which is the same choice the terminal makes for a
/// network fee reserve and for the same reason: a defaulted threshold is a number
/// nobody chose, attached to a decision about somebody's money. A caller that gets
/// None must refuse, not guess.
pub fn confirmations_for(posture: &Posture, asset: &str) -> Option<u32> {
    posture
        .confirmations
        .iter()
        .find(|(name, _)| name == asset)
        .map(|(_, blocks)| *blocks)
}

pub fn pair_text(pair: &Pair) -> String {
    format!("{} -> {}", pair.from, pair.to)
}

pub fn posture_summary(posture: &Posture) -> String {
    format!(
        "{} pair(s), {} bps, {} threshold(s), {} armed",
        posture.pairs.len(),
        posture.fee_bps,
        posture.confirmations.len(),
        posture.armed.len()
    )
}

// =============================================================================
// THE CONSOLE
// =============================================================================

/// The palette, carried from swap_terminal/static/styles.css rather than invented.
///
/// COPIED DELIBERATELY, AND THE DUPLICATION IS NAMED HERE BECAUSE RULE 8 ASKS FOR
/// IT AT BOTH SITES. A canister cannot read a file, so it cannot import that
/// stylesheet; the alternatives were to serve a page that looks like nothing else
/// in this system, or to hold these eight values twice and say so. The second is
/// the smaller cost, and the thing that would drift -- a hex code -- is a hex code,
/// not a decision. If the terminal's theme changes, this is the other place.
///
/// The values and their reasons are in styles.css: phosphor green on near-black,
/// red for halted, amber as the second phosphor so `slow` and `halted` are not the
/// same colour, violet for "nobody has established this".
const CSS: &str = "\
:root{--bg:#04080a;--ink:#3cff6e;--ink-soft:#27b84e;--line:#124049;--surface:#07131a;\
--accent:#38bdf8;--ok:#3cff6e;--halted:#ff3b30;--slow:#ffb000;--unknown:#a78bfa;\
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--glow:0 0 2px rgba(60,255,110,.45)}\
*{box-sizing:border-box}\
body{margin:0;padding:1rem;background:var(--bg);color:var(--ink);font:14px/1.55 var(--mono);\
text-shadow:var(--glow)}\
main{max-width:70rem;margin:0 auto}\
h1{font-size:1.25rem;margin:0 0 .25rem;letter-spacing:.08em}\
h2{font-size:1rem;margin:1.75rem 0 .5rem;color:var(--accent);letter-spacing:.06em;\
border-bottom:1px solid var(--line);padding-bottom:.25rem}\
p{max-width:62ch;color:var(--ink-soft);margin:.4rem 0}\
table{border-collapse:collapse;width:100%;margin:.5rem 0}\
th,td{text-align:left;padding:.35rem .6rem;border-bottom:1px solid var(--line);vertical-align:top}\
th{color:var(--accent);font-weight:400;text-transform:uppercase;font-size:.78rem;letter-spacing:.07em}\
td.n{text-align:right;font-variant-numeric:tabular-nums}\
code{background:var(--surface);border:1px solid var(--line);padding:.1rem .35rem;\
display:inline-block;color:var(--ink);white-space:pre-wrap;word-break:break-all}\
.cmd{display:block;margin:.3rem 0 0;padding:.45rem .6rem;color:var(--ink-soft)}\
.note{border:1px solid var(--line);border-left:5px solid var(--slow);background:var(--surface);\
padding:.7rem .9rem;margin:.75rem 0}\
.note.hard{border-left-color:var(--halted)}\
.note b{color:var(--slow)}\
.note.hard b{color:var(--halted)}\
.badge{border:1px solid currentColor;border-radius:2px;padding:0 .4rem;font-size:.75rem;\
letter-spacing:.06em}\
.ok{color:var(--ok)}.off{color:var(--unknown);border-style:dotted}\
footer{margin-top:2rem;padding-top:.6rem;border-top:1px solid var(--line);color:var(--ink-soft);\
font-size:.8rem}\
@media(max-width:40rem){body{padding:1rem .75rem}th,td{padding:.3rem .35rem}}";

/// The banner every page carries. A PAGE THAT LOOKS AUTHORITATIVE AND IS NOT IS
/// WORSE THAN NO PAGE, which is the whole reason this string is not optional and
/// not at the bottom: an operator who changes a value here and expects the desk to
/// follow has been misled by a convincing screen.
const NOT_WIRED: &str = "Nothing in the terminal reads this canister yet. It still takes \
ALLOWED_PAIRS, DEFAULT_FEE_BPS and the per-chain thresholds from its own environment, so a value \
changed here changes what THIS CANISTER says and nothing else. Wiring the terminal to read from \
here changes what gets traded and introduces a posture question with no default -- what the desk \
holds when the replica is down -- so it is a decision, not a follow-up commit.";

fn row(label: &str, value: &str, means: &str, command: &str) -> String {
    let mut out = String::new();
    let _ = write!(
        out,
        "<tr><th>{}</th><td>{}</td><td>{}",
        escape(label),
        value,
        escape(means)
    );
    if !command.is_empty() {
        let _ = write!(out, "<code class=\"cmd\">{}</code>", escape(command));
    }
    out.push_str("</td></tr>");
    out
}

/// The whole console, as a string. THE RENDERER IS A PURE FUNCTION so every page it
/// can produce is assertable in `cargo test` -- the empty state, a state with a
/// hostile note in the audit log, a state with no thresholds. A renderer that
/// needed a replica could only be checked by deploying it and looking.
pub fn render_console(posture: &Posture, audit: &[AuditEntry], canister: &str) -> String {
    let mut h = String::with_capacity(8 * 1024);
    let _ = write!(
        h,
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">\
         <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">\
         <title>Operator posture</title><style>{CSS}</style></head><body><main>\
         <h1>OPERATOR POSTURE</h1>\
         <p>Served by canister <code>{}</code>. This page is READ-ONLY: requests through the \
         HTTP gateway arrive anonymous, so a form here would be an unauthenticated write into \
         the values that decide what trades. Every field below carries the signed call that \
         changes it.</p>\
         <div class=\"note hard\"><b>NOT WIRED.</b> {}</div>",
        escape(canister),
        escape(NOT_WIRED)
    );

    // ---- the fee ----
    h.push_str("<h2>Fee</h2><table><tr><th>Field</th><th>Value</th><th>What it decides</th></tr>");
    h.push_str(&row(
        "fee_bps",
        &format!(
            "<td class=\"n\">{}</td><td>{:.2}%",
            posture.fee_bps,
            f64::from(posture.fee_bps) / 100.0
        )
        .replace("<td class=\"n\">", "")
        .replace("</td><td>", " &nbsp;"),
        "Subtracted from every payout, so the desk keeps it by sending that much less. \
         The ceiling here is a typo guard, not a policy.",
        &format!("dfx canister call {canister} set_fee_bps '(150 : nat16, \"why\")'"),
    ));
    h.push_str("</table>");

    // ---- the pairs ----
    let _ = write!(
        h,
        "<h2>Tradeable directions &nbsp;<span class=\"badge ok\">{}</span></h2>\
         <p>A direction, not a pair: BTC&nbsp;-&gt;&nbsp;GRC spends the desk's GRC, and the \
         reverse spends its BTC. An absent direction is one that cannot be quoted.</p>",
        posture.pairs.len()
    );
    if posture.pairs.is_empty() {
        h.push_str("<p><span class=\"badge off\">NONE</span> &nbsp;No direction is enabled, so \
                    nothing could be quoted if this were wired. That is a result, not a \
                    missing page.</p>");
    } else {
        h.push_str("<table><tr><th>From</th><th>To</th></tr>");
        for pair in &posture.pairs {
            let _ = write!(
                h,
                "<tr><td>{}</td><td>{}</td></tr>",
                escape(&pair.from),
                escape(&pair.to)
            );
        }
        h.push_str("</table>");
    }
    let _ = write!(
        h,
        "<code class=\"cmd\">dfx canister call {canister} add_pair '(record {{ from = \"BTC\"; \
         to = \"GRC\" }}, \"why\")'</code>\
         <code class=\"cmd\">dfx canister call {canister} remove_pair '(record {{ from = \"BTC\"; \
         to = \"GRC\" }}, \"why\")'</code>"
    );

    // ---- the thresholds ----
    h.push_str("<h2>Confirmation thresholds</h2><p>How many blocks a deposit needs before it \
                is credited. An asset with no entry has no threshold here, which a reader must \
                treat as unset rather than zero.</p>");
    if posture.confirmations.is_empty() {
        h.push_str("<p><span class=\"badge off\">NONE SET</span></p>");
    } else {
        h.push_str("<table><tr><th>Asset</th><th>Blocks</th></tr>");
        for (asset, blocks) in &posture.confirmations {
            let _ = write!(
                h,
                "<tr><td>{}</td><td class=\"n\">{}</td></tr>",
                escape(asset),
                blocks
            );
        }
        h.push_str("</table>");
    }
    let _ = write!(
        h,
        "<code class=\"cmd\">dfx canister call {canister} set_confirmations '(\"BTC\", 2 : nat32, \
         \"why\")'</code>"
    );

    // ---- arming ----
    h.push_str("<h2>Armed payout chains</h2><p>Absent means NOT armed. This list says what is \
                turned on and never what is turned off, so a forgotten entry fails closed.</p>");
    if posture.armed.is_empty() {
        h.push_str("<p><span class=\"badge off\">NONE ARMED</span></p>");
    } else {
        h.push_str("<p>");
        for asset in &posture.armed {
            let _ = write!(h, "<span class=\"badge ok\">{}</span> &nbsp;", escape(asset));
        }
        h.push_str("</p>");
    }
    let _ = write!(
        h,
        "<code class=\"cmd\">dfx canister call {canister} set_armed '(\"GRC\", true, \"why\")'</code>"
    );

    // ---- the audit log ----
    let _ = write!(
        h,
        "<h2>Audit log &nbsp;<span class=\"badge ok\">{}</span></h2>\
         <p>Append-only and never pruned. Newest first. The principal is what signed the call; \
         this canister is never told a name, because a name is something it could be told \
         wrongly.</p>",
        audit.len()
    );
    if audit.is_empty() {
        h.push_str("<p><span class=\"badge off\">NO CHANGES RECORDED</span> &nbsp;Every value \
                    above is as installed.</p>");
    } else {
        h.push_str("<table><tr><th>When (UTC)</th><th>Field</th><th>Was</th><th>Now</th>\
                    <th>Reason</th><th>By</th></tr>");
        for entry in audit.iter().rev() {
            let _ = write!(
                h,
                "<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>",
                escape(&format_time(entry.at)),
                escape(&entry.field),
                escape(&entry.was),
                escape(&entry.now),
                escape(&entry.note),
                escape(&entry.by)
            );
        }
        h.push_str("</table>");
    }

    let _ = write!(
        h,
        "<footer>{} &nbsp;|&nbsp; writes are controller-only &nbsp;|&nbsp; \
         <code>dfx canister call {canister} posture '()'</code> for the same state as candid\
         </footer></main></body></html>",
        escape(&posture_summary(posture))
    );
    h
}

// =============================================================================
// THE INTERFACE
// =============================================================================

#[derive(CandidType, Deserialize)]
pub struct HttpRequest {
    pub method: String,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

#[derive(CandidType)]
pub struct HttpResponse {
    pub status_code: u16,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
    /// Ask the HTTP gateway to re-send this request as an UPDATE call.
    ///
    /// THIS FIELD IS WHY THE CONSOLE WORKS AT ALL, and its absence is the defect
    /// it was added to fix. Measured on the operator's replica 2026-10-08, the
    /// first time this canister was ever deployed: the console answered
    ///
    /// ```text
    /// http gateway: 500    60 bytes
    /// ```
    ///
    /// and the browser said, verbatim, "Response verification failed:
    /// Certification values not found".
    ///
    /// WHAT THE GATEWAY ACTUALLY REQUIRES. A QUERY response is produced by one
    /// replica with nothing standing behind it -- a single malicious node could
    /// return any page it liked -- so the HTTP Gateway Protocol will not serve a
    /// query response to a browser unless the canister certifies it, with an
    /// `IC-Certificate` header carrying a certificate and a witness tree rooted
    /// in the 32 bytes of certified data the canister has published. No header,
    /// no page: the gateway returns 500 and the canister never learns why.
    ///
    /// WHY NOT CERTIFY IT. Certifying means maintaining a certification tree and
    /// re-publishing a root hash on every state change, because
    /// `set_certified_data` holds 32 bytes and nothing more. This page is derived
    /// from the posture AND the full audit log, so every write would have to
    /// re-hash a page nobody is reading at that moment. That is real machinery to
    /// maintain, and the thing it buys -- a browser cryptographically verifying
    /// an operator console served from the operator's own laptop over
    /// 127.0.0.1 -- is not a threat this deployment has.
    ///
    /// WHAT upgrade = Some(true) DOES INSTEAD. The gateway re-sends the identical
    /// request as an UPDATE call to `http_request_update`. An update goes through
    /// consensus, so its response is already backed by the subnet and the
    /// protocol skips response verification entirely. It costs one extra round
    /// trip and a couple of seconds, which for a page an operator opens a few
    /// times a day is the correct trade and for an asset served per page-load
    /// would not be.
    ///
    /// None on a response that is already the answer; the update handler sets it
    /// None precisely so a response cannot ask to be upgraded twice.
    pub upgrade: Option<bool>,
}

/// Only a CONTROLLER may write.
///
/// `is_controller` AND NOT AN ALLOWLIST THIS CANISTER KEEPS. A principal list in
/// state is a list that can be emptied by a bad call, locking the canister's own
/// authority away from everybody; controllers are held by the management canister,
/// which is the one place that cannot be bricked from inside here. It is also
/// already the operator's identity -- whoever deployed this -- so there is nothing
/// extra to configure and nothing to get out of step.
fn require_controller() -> Result<Principal, String> {
    let caller = ic_cdk::caller();
    if ic_cdk::api::is_controller(&caller) {
        return Ok(caller);
    }
    Err(format!(
        "{caller} is not a controller of this canister, so nothing was changed. Writes are \
         controller-only on purpose: these are the values that decide what trades. Check with \
         `dfx canister info <this canister>` and call again under an identity that is listed."
    ))
}

fn record(by: Principal, field: &str, was: String, now: String, note: &str) {
    STATE.with(|s| {
        s.borrow_mut().audit.push(AuditEntry {
            at: ic_cdk::api::time(),
            by: by.to_text(),
            field: field.to_string(),
            was,
            now,
            note: note.trim().to_string(),
        });
    });
}

#[ic_cdk::init]
fn init(seed: Posture) {
    // SEEDED AT INSTALL FROM THE TERMINAL'S CURRENT CONFIGURATION, so the console
    // shows real values on its first load rather than an empty page the operator
    // has to populate by hand before it says anything. Validation is NOT applied to
    // the seed on purpose: refusing at install would leave a canister that exists
    // and holds nothing, and the console renders whatever is there -- so a bad seed
    // is visible rather than fatal.
    STATE.with(|s| s.borrow_mut().posture = seed);
}

#[ic_cdk::pre_upgrade]
fn pre_upgrade() {
    // THE AUDIT LOG IS THE REASON THIS EXISTS. threshold_custody deliberately keeps
    // nothing across an upgrade because its installer re-supplies everything it
    // holds; here the log is evidence that cannot be re-supplied by anyone, and rule
    // 7 is explicit that evidence is never dropped to save effort.
    STATE.with(|s| {
        let state = s.borrow();
        let saved = (state.posture.clone(), state.audit.clone());
        if let Err(error) = ic_cdk::storage::stable_save((saved,)) {
            // A TRAP RATHER THAN A SWALLOW: if the log cannot be written, the upgrade
            // must not proceed, because proceeding is what loses it.
            ic_cdk::trap(&format!("could not save state before upgrade, so the upgrade was \
                                   refused rather than losing the audit log: {error:?}"));
        }
    });
}

#[ic_cdk::post_upgrade]
fn post_upgrade() {
    match ic_cdk::storage::stable_restore::<((Posture, Vec<AuditEntry>),)>() {
        Ok(((posture, audit),)) => STATE.with(|s| {
            let mut state = s.borrow_mut();
            state.posture = posture;
            state.audit = audit;
        }),
        Err(error) => ic_cdk::trap(&format!(
            "could not restore state after upgrade: {error:?}. Refusing to come up with an \
             empty audit log, which would read as 'nothing was ever changed'"
        )),
    }
}

#[ic_cdk::query]
fn posture() -> Posture {
    STATE.with(|s| s.borrow().posture.clone())
}

#[ic_cdk::query]
fn audit() -> Vec<AuditEntry> {
    STATE.with(|s| s.borrow().audit.clone())
}

#[ic_cdk::query]
fn confirmations(asset: String) -> Option<u32> {
    STATE.with(|s| confirmations_for(&s.borrow().posture, &asset))
}

#[ic_cdk::update]
fn set_fee_bps(bps: u16, note: String) -> Result<u16, String> {
    let by = require_controller()?;
    validate_note(&note)?;
    validate_fee_bps(bps)?;
    let was = STATE.with(|s| {
        let mut state = s.borrow_mut();
        let was = state.posture.fee_bps;
        state.posture.fee_bps = bps;
        was
    });
    record(by, "fee_bps", was.to_string(), bps.to_string(), &note);
    Ok(bps)
}

#[ic_cdk::update]
fn add_pair(pair: Pair, note: String) -> Result<Vec<Pair>, String> {
    let by = require_controller()?;
    validate_note(&note)?;
    validate_pair(&pair)?;
    let added = STATE.with(|s| {
        let mut state = s.borrow_mut();
        if state.posture.pairs.contains(&pair) {
            return false;
        }
        state.posture.pairs.push(pair.clone());
        true
    });
    if !added {
        // ALREADY PRESENT IS NOT AN ERROR AND IS NOT A CHANGE. It writes no audit
        // entry, because an entry saying a value became what it already was is
        // noise in the one record that has to stay readable.
        return Err(format!(
            "{} is already enabled, so nothing was changed and no audit entry was written",
            pair_text(&pair)
        ));
    }
    record(by, "pairs", "absent".to_string(), pair_text(&pair), &note);
    Ok(posture().pairs)
}

#[ic_cdk::update]
fn remove_pair(pair: Pair, note: String) -> Result<Vec<Pair>, String> {
    let by = require_controller()?;
    validate_note(&note)?;
    let removed = STATE.with(|s| {
        let mut state = s.borrow_mut();
        let before = state.posture.pairs.len();
        state.posture.pairs.retain(|p| p != &pair);
        before != state.posture.pairs.len()
    });
    if !removed {
        return Err(format!(
            "{} is not enabled, so nothing was changed",
            pair_text(&pair)
        ));
    }
    record(by, "pairs", pair_text(&pair), "absent".to_string(), &note);
    Ok(posture().pairs)
}

#[ic_cdk::update]
fn set_confirmations(asset: String, blocks: u32, note: String) -> Result<u32, String> {
    let by = require_controller()?;
    validate_note(&note)?;
    validate_confirmations(&asset, blocks)?;
    let was = STATE.with(|s| {
        let mut state = s.borrow_mut();
        let was = confirmations_for(&state.posture, &asset);
        match state.posture.confirmations.iter_mut().find(|(a, _)| a == &asset) {
            Some(entry) => entry.1 = blocks,
            None => state.posture.confirmations.push((asset.clone(), blocks)),
        }
        was
    });
    record(
        by,
        &format!("confirmations[{asset}]"),
        was.map_or_else(|| "unset".to_string(), |n| n.to_string()),
        blocks.to_string(),
        &note,
    );
    Ok(blocks)
}

#[ic_cdk::update]
fn set_armed(asset: String, armed: bool, note: String) -> Result<Vec<String>, String> {
    let by = require_controller()?;
    validate_note(&note)?;
    validate_asset(&asset)?;
    let was = STATE.with(|s| {
        let mut state = s.borrow_mut();
        let was = state.posture.armed.contains(&asset);
        if armed && !was {
            state.posture.armed.push(asset.clone());
        } else if !armed {
            state.posture.armed.retain(|a| a != &asset);
        }
        was
    });
    if was == armed {
        return Err(format!(
            "{asset} is already {}, so nothing was changed",
            if armed { "armed" } else { "not armed" }
        ));
    }
    record(
        by,
        &format!("armed[{asset}]"),
        was.to_string(),
        armed.to_string(),
        &note,
    );
    Ok(posture().armed)
}

/// The console, as the gateway asks for it: tell it to come back as an update.
///
/// NO WORK IS DONE HERE AND NO BODY IS RETURNED, deliberately. Whatever this
/// query returned would be discarded -- the gateway re-issues the request the
/// moment it sees `upgrade = Some(true)` -- so rendering the page here would
/// render it twice per page load and throw one away. The empty body is the
/// honest representation of that.
///
/// THE 405 MOVED WITH IT, which is the part worth saying out loud: a non-GET
/// used to be refused right here, in the query. That refusal was just as
/// uncertified as the page was, so it hit the same gateway 500 and the operator
/// would have seen a server error where this canister had written a careful
/// explanation. Both paths now run in `http_request_update`, where a response
/// is backed by consensus and actually reaches the caller.
#[ic_cdk::query]
fn http_request(_request: HttpRequest) -> HttpResponse {
    HttpResponse {
        status_code: 200,
        headers: vec![],
        body: Vec::new(),
        upgrade: Some(true),
    }
}

/// The console, for real. Reached only because `http_request` asked for it.
#[ic_cdk::update]
fn http_request_update(request: HttpRequest) -> HttpResponse {
    // GET ONLY, AND THE REFUSAL IS THE POINT RATHER THAN A FORMALITY. A POST here
    // would arrive with no principal -- the HTTP gateway does not carry one -- so
    // the only honest answer to one is 405 naming where writes actually go.
    if request.method.to_uppercase() != "GET" {
        return HttpResponse {
            status_code: 405,
            upgrade: None,
            headers: vec![("Content-Type".to_string(), "text/plain; charset=utf-8".to_string())],
            body: b"405 -- this console is read-only. Requests through the HTTP gateway arrive \
                    ANONYMOUS, so a write accepted here would be an unauthenticated change to the \
                    values that decide what trades. Writes go through the Candid interface, where \
                    your own identity signs the call; the console prints the exact command for \
                    every field."
                .to_vec(),
        };
    }
    let (posture, audit) = STATE.with(|s| {
        let state = s.borrow();
        (state.posture.clone(), state.audit.clone())
    });
    console_response(&posture, &audit, &ic_cdk::id().to_text())
}

/// The 200 that carries the console. PURE, and that is the point of it existing.
///
/// EXTRACTED 2026-10-08, while fixing the gateway 500, because the GET path could
/// not be reached from a test at all: `http_request_update` calls `ic_cdk::id()`,
/// which panics outside a canister, so every assertion about the response a
/// browser actually receives -- its status, its `upgrade` field, its headers --
/// was unreachable. The 405 branch was testable only by accident, because it
/// happens to return before that call.
///
/// Rule 10, and it is the same move the rest of this file already made for
/// `render_console` and the validators: the thing that decides is the smallest
/// piece at the bottom, and the handler above it is left holding only the parts
/// that need a canister (reading STATE, asking its own id).
fn console_response(posture: &Posture, audit: &[AuditEntry], canister: &str) -> HttpResponse {
    HttpResponse {
        status_code: 200,
        // None, NOT Some(false): this response IS the answer. A response that
        // asked to be upgraded again would loop the gateway.
        upgrade: None,
        headers: vec![
            ("Content-Type".to_string(), "text/html; charset=utf-8".to_string()),
            // NO CACHING. The whole value of this page is that it shows what the
            // canister holds RIGHT NOW; a cached posture page is the stale-truth
            // failure this repository keeps paying for.
            ("Cache-Control".to_string(), "no-store".to_string()),
            // The page loads no script and no remote resource, so the strictest
            // policy is also the accurate description of it.
            (
                "Content-Security-Policy".to_string(),
                "default-src 'none'; style-src 'unsafe-inline'".to_string(),
            ),
        ],
        body: render_console(posture, audit, canister).into_bytes(),
    }
}

ic_cdk::export_candid!();

// =============================================================================
// THE TESTS
//
// THEY RUN WITH `cargo test` AND NO REPLICA, which is why every decision above is
// a free function rather than a method on the canister: a check that needed `dfx
// deploy` would run on the operator's host and nowhere else, and a gate that only
// runs where the mistake is already made is not a gate (the same argument
// tests/test_dfx_canisters_are_buildable.py makes for using tomllib over `cargo
// metadata`).
//
// WHAT THEY CANNOT COVER, said plainly rather than implied: `ic_cdk::caller()`,
// `is_controller`, `time()` and stable storage all need a canister environment, so
// require_controller(), the update methods' writes and the upgrade hooks are NOT
// exercised here. What is exercised is every refusal those methods delegate to and
// every byte of the page they render -- the parts where a wrong answer is silent.
// =============================================================================

#[cfg(test)]
mod tests {
    use super::*;

    fn seeded() -> Posture {
        Posture {
            pairs: vec![
                Pair { from: "BTC".into(), to: "GRC".into() },
                Pair { from: "ICP".into(), to: "GRC".into() },
            ],
            fee_bps: 150,
            confirmations: vec![("BTC".into(), 2), ("ICP".into(), 1)],
            armed: vec!["GRC".into()],
        }
    }

    // ---- asset codes ----

    #[test]
    fn an_uppercase_code_is_accepted() {
        for code in ["BTC", "GRC", "ICP", "USDC", "WBTC01"] {
            assert!(validate_asset(code).is_ok(), "{code} was refused");
        }
    }

    #[test]
    fn a_lowercase_code_is_refused_because_nothing_would_match_it() {
        // The terminal keys adapters, reserves and thresholds on the uppercase
        // form, so a lowercase entry here is not a different asset -- it is an
        // entry that silently matches nothing.
        let why = validate_asset("btc").unwrap_err();
        assert!(why.contains("uppercase"), "{why}");
        assert!(why.contains("Nothing was changed"), "{why}");
    }

    #[test]
    fn a_code_that_could_not_be_one_at_all_is_refused() {
        for code in ["", "B", "BTC-GRC", "BTC ", "VERYLONGASSET"] {
            assert!(validate_asset(code).is_err(), "{code:?} was accepted");
        }
    }

    #[test]
    fn the_six_chains_the_terminal_serves_are_all_acceptable() {
        // NOT AN ALLOWLIST -- see validate_asset's docstring for why this canister
        // deliberately does not hold one. This asserts the validator does not
        // ACCIDENTALLY exclude the chains that exist, which is the only thing a
        // non-allowlist can promise about them.
        for code in ["BTC", "LTC", "GRC", "XRP", "SOL", "ICP"] {
            assert!(validate_asset(code).is_ok(), "{code} would be refused");
        }
    }

    // ---- pairs ----

    #[test]
    fn a_direction_needs_two_different_assets() {
        let why = validate_pair(&Pair { from: "GRC".into(), to: "GRC".into() }).unwrap_err();
        assert!(why.contains("same asset on both sides"), "{why}");
    }

    #[test]
    fn both_sides_of_a_pair_are_validated() {
        assert!(validate_pair(&Pair { from: "btc".into(), to: "GRC".into() }).is_err());
        assert!(validate_pair(&Pair { from: "BTC".into(), to: "grc".into() }).is_err());
        assert!(validate_pair(&Pair { from: "BTC".into(), to: "GRC".into() }).is_ok());
    }

    #[test]
    fn direction_is_not_a_set() {
        // BTC -> GRC spends the desk's GRC and GRC -> BTC spends its BTC, so the
        // two are different postures and both are valid on their own.
        let forward = Pair { from: "BTC".into(), to: "GRC".into() };
        let back = Pair { from: "GRC".into(), to: "BTC".into() };
        assert!(validate_pair(&forward).is_ok());
        assert!(validate_pair(&back).is_ok());
        assert_ne!(forward, back);
    }

    // ---- the fee ----

    #[test]
    fn the_terminals_current_fee_is_accepted_and_a_misplaced_digit_is_not() {
        assert!(validate_fee_bps(150).is_ok(), "the figure the desk actually charges");
        assert!(validate_fee_bps(FEE_BPS_CEILING).is_ok(), "the ceiling itself is allowed");
        // 1500 IS THE TYPO THIS GUARD EXISTS FOR: one extra digit on 150. The
        // ceiling was 2000 when this test was written and let it through, which is
        // what sent me back to the constant.
        let why = validate_fee_bps(1500).unwrap_err();
        assert!(why.contains("15.0%"), "the refusal must say what the number MEANS: {why}");
        assert!(why.contains("1000 bps"), "and name the ceiling it broke: {why}");
        assert!(why.contains("ceiling is what to change"), "{why}");
    }

    #[test]
    fn a_fee_that_would_zero_a_payout_is_refused() {
        assert!(validate_fee_bps(10_000).is_err(), "100% is a payout of nothing");
    }

    // ---- confirmations ----

    #[test]
    fn zero_confirmations_is_refused_and_the_refusal_says_why_it_costs_money() {
        let why = validate_confirmations("BTC", 0).unwrap_err();
        assert!(why.contains("in no block yet"), "{why}");
        assert!(why.contains("later replaced"), "a replaced deposit is the hazard: {why}");
        assert!(why.contains("smallest honest value is 1"), "{why}");
    }

    #[test]
    fn one_confirmation_is_allowed_because_icp_uses_it_and_means_final() {
        assert!(validate_confirmations("ICP", 1).is_ok());
    }

    #[test]
    fn a_threshold_nothing_could_ever_reach_is_refused_as_a_halt() {
        let why = validate_confirmations("BTC", 100_000).unwrap_err();
        assert!(why.contains("it is a halt"), "{why}");
    }

    // ---- the reason ----

    #[test]
    fn every_change_needs_a_reason_and_whitespace_is_not_one() {
        for note in ["", "   ", "ok", "\t\n "] {
            assert!(validate_note(note).is_err(), "{note:?} was accepted as a reason");
        }
        assert!(validate_note("refill after the GRC top-up").is_ok());
    }

    #[test]
    fn a_reason_longer_than_a_line_is_refused() {
        assert!(validate_note(&"x".repeat(301)).is_err());
        assert!(validate_note(&"x".repeat(300)).is_ok());
    }

    // ---- lookups ----

    #[test]
    fn an_asset_with_no_threshold_answers_none_rather_than_zero() {
        // None is the whole point: a defaulted threshold is a number nobody chose,
        // attached to a decision about somebody's money.
        let posture = seeded();
        assert_eq!(confirmations_for(&posture, "BTC"), Some(2));
        assert_eq!(confirmations_for(&posture, "LTC"), None);
    }

    // ---- the clock ----

    #[test]
    fn the_timestamp_formatter_matches_known_dates() {
        // Hand-checked instants, including a leap day and the epoch itself, because
        // the civil-from-days arithmetic is the kind that is wrong by one day for
        // years at a time without anybody noticing.
        assert_eq!(format_time(0), "1970-01-01T00:00:00Z");
        assert_eq!(format_time(1_000_000_000 * 1_000_000_000), "2001-09-09T01:46:40Z");
        // A LEAP DAY, which is the case the civil-from-days arithmetic exists for.
        assert_eq!(format_time(1_709_206_496 * 1_000_000_000), "2024-02-29T11:34:56Z");
        // The evening this canister was written.
        assert_eq!(format_time(1_791_500_000 * 1_000_000_000), "2026-10-08T22:53:20Z");
        // EVERY EXPECTATION ABOVE IS CROSS-CHECKED AGAINST AN INDEPENDENT
        // IMPLEMENTATION rather than read off this one, because a formatter tested
        // against its own output is tested against nothing. Three of these were
        // wrong on the first run -- I had typed the hours by hand -- and the values
        // here are `datetime.fromtimestamp(e, UTC)` from CPython for the same
        // instants. The leap day is the one that matters: an off-by-one in the
        // era arithmetic is wrong for years at a stretch and only shows up in
        // February.
    }

    // ---- escaping ----

    #[test]
    fn a_hostile_note_cannot_reach_the_page_as_markup() {
        // THE AUDIT LOG IS CALLER-SUPPLIED and the console renders it. The replica
        // binds 127.0.0.1 only, which limits who can reach the page -- it does not
        // limit what a note already in the log does once the page is opened, from
        // an origin that can also call this canister's update methods.
        let hostile = "</td><script>alert('x')</script>";
        let escaped = escape(hostile);
        assert!(!escaped.contains('<'), "{escaped}");
        assert!(!escaped.contains('>'), "{escaped}");
        assert_eq!(escape("a&b"), "a&amp;b");
        assert_eq!(escape("\"q\""), "&quot;q&quot;");
        assert_eq!(escape("it's"), "it&#39;s");
    }

    #[test]
    fn the_rendered_page_contains_no_unescaped_caller_text() {
        let posture = seeded();
        let audit = vec![AuditEntry {
            at: 1_791_500_000 * 1_000_000_000,
            by: "aaaaa-aa".into(),
            field: "fee_bps".into(),
            was: "150".into(),
            now: "175".into(),
            note: "<img src=x onerror=alert(1)>".into(),
        }];
        let html = render_console(&posture, &audit, "be2us-64aaa-aaaaa-qaabq-cai");
        assert!(!html.contains("<img"), "a caller's markup reached the page");
        assert!(html.contains("&lt;img src=x onerror=alert(1)&gt;"), "and was not escaped");
    }

    // ---- the console ----

    #[test]
    fn every_page_says_it_is_not_wired() {
        // A page that looks authoritative and is not is worse than no page. This is
        // the assertion that keeps that sentence from being quietly dropped.
        let html = render_console(&seeded(), &[], "aaaaa-aa");
        assert!(html.contains("NOT WIRED"), "{html}");
        assert!(html.contains("takes \\ALLOWED_PAIRS") || html.contains("ALLOWED_PAIRS"));
        assert!(html.contains("what the desk holds when the replica is down"));
    }

    #[test]
    fn the_console_says_it_is_read_only_and_why() {
        let html = render_console(&seeded(), &[], "aaaaa-aa");
        assert!(html.contains("READ-ONLY"), "{html}");
        assert!(html.contains("anonymous"), "the REASON must be there, not just the fact");
    }

    #[test]
    fn every_field_carries_the_command_that_changes_it() {
        let canister = "be2us-64aaa-aaaaa-qaabq-cai";
        let html = render_console(&seeded(), &[], canister);
        for method in ["set_fee_bps", "add_pair", "remove_pair", "set_confirmations", "set_armed"] {
            let expected = format!("dfx canister call {canister} {method}");
            assert!(html.contains(&expected), "no pasteable command for {method}");
        }
    }

    #[test]
    fn an_empty_posture_renders_results_rather_than_blank_gaps() {
        // Rule 14: `(none)` is a result and a blank gap is not. An empty admin page
        // must say that nothing is enabled, which is a fact, instead of showing an
        // empty table that cannot be told from a broken query.
        let html = render_console(&Posture::default(), &[], "aaaaa-aa");
        assert!(html.contains("No direction is enabled"), "{html}");
        assert!(html.contains("NONE SET"), "no thresholds must say so");
        assert!(html.contains("NONE ARMED"), "nothing armed must say so");
        assert!(html.contains("NO CHANGES RECORDED"), "an empty audit log must say so");
        assert!(!html.contains("<table><tr><th>From</th><th>To</th></tr></table>"),
                "an empty table was rendered where a sentence belongs");
    }

    #[test]
    fn the_page_renders_the_values_it_was_given() {
        let html = render_console(&seeded(), &[], "aaaaa-aa");
        assert!(html.contains("150"), "the fee");
        assert!(html.contains("1.50%"), "and what it means as a percentage");
        assert!(html.contains("BTC") && html.contains("GRC"), "the pairs");
        assert!(html.contains("<td class=\"n\">2</td>"), "the BTC threshold");
    }

    #[test]
    fn the_audit_log_renders_newest_first() {
        let entry = |at: u64, note: &str| AuditEntry {
            at,
            by: "aaaaa-aa".into(),
            field: "fee_bps".into(),
            was: "150".into(),
            now: "160".into(),
            note: note.into(),
        };
        let audit = vec![
            entry(1_700_000_000 * 1_000_000_000, "the older change"),
            entry(1_791_500_000 * 1_000_000_000, "the newer change"),
        ];
        let html = render_console(&seeded(), &audit, "aaaaa-aa");
        let newer = html.find("the newer change").expect("newer entry missing");
        let older = html.find("the older change").expect("older entry missing");
        assert!(newer < older, "the log must read newest first");
    }

    #[test]
    fn the_summary_states_every_count_with_its_unit() {
        // Rule 3: a count with no denominator -- or no unit -- has caused real
        // errors here. "2 pair(s)" is readable; "2" beside three other numbers is
        // not.
        let summary = posture_summary(&seeded());
        assert_eq!(summary, "2 pair(s), 150 bps, 2 threshold(s), 1 armed");
    }

    #[test]
    fn the_page_loads_no_script_and_no_remote_resource() {
        // The Content-Security-Policy this canister sends is `default-src 'none'`,
        // and that header is only honest if the page really does load nothing. This
        // asserts the page and the header agree.
        let html = render_console(&seeded(), &[], "aaaaa-aa");
        for forbidden in ["<script", "src=\"http", "href=\"http", "@import", "fetch(", "onload="] {
            assert!(!html.contains(forbidden), "the page loads {forbidden}");
        }
    }

    // =========================================================================
    // THE GATEWAY 500. Measured on the operator's replica 2026-10-08, the first
    // time this canister was ever deployed:
    //
    //     http gateway: 500    60 bytes
    //
    // and in the browser, verbatim: "Response verification failed: Certification
    // values not found". The canister was fine; `posture '()'` answered through
    // Candid on the same replica in the same minute. The HTTP Gateway Protocol
    // will not serve a QUERY response it cannot verify against certified data,
    // this canister published none, and nothing in it could have told you that --
    // the 500 is generated by the gateway, before the body is ever looked at.
    //
    // These pin the fix: the query asks to be upgraded, and the responses that
    // carry real answers come from the update, where consensus has already
    // backed them and no certificate is required.
    // =========================================================================

    fn request(method: &str) -> HttpRequest {
        HttpRequest {
            method: method.to_string(),
            url: "/".to_string(),
            headers: vec![],
            body: vec![],
        }
    }

    #[test]
    fn the_query_ALWAYS_asks_to_be_upgraded_and_renders_nothing() {
        // Every method and every url, because the query cannot usefully decide
        // anything: it has no certificate to offer, so the only response it can
        // give that a browser will ever see is "come back as an update". A query
        // that tried to answer here would be a 500 with a careful body nobody
        // receives.
        for method in ["GET", "POST", "HEAD", "PUT", "DELETE", "OPTIONS", "get", "Get"] {
            let response = http_request(request(method));
            assert_eq!(
                response.upgrade,
                Some(true),
                "{method}: the query must ask the gateway to re-send as an update; \
                 without it the gateway answers 500 'Certification values not found'"
            );
            assert!(
                response.body.is_empty(),
                "{method}: the body is discarded when the gateway upgrades, so rendering \
                 one here renders the page twice per load and throws one away"
            );
        }
    }

    #[test]
    fn a_non_GET_is_refused_by_the_UPDATE_where_the_answer_can_be_delivered() {
        // THE 405 MOVED, and that is a fix rather than a relocation. It used to be
        // returned by the query, which made it exactly as uncertified as the page
        // was -- so a POST would have produced a gateway 500 where this canister
        // had written a careful explanation of where writes go. Reachable from a
        // test because the branch returns before `ic_cdk::id()`.
        for method in ["POST", "PUT", "DELETE", "PATCH"] {
            let response = http_request_update(request(method));
            assert_eq!(response.status_code, 405, "{method} must be refused");
            assert_eq!(
                response.upgrade, None,
                "{method}: a response that is already the answer must not ask to be \
                 upgraded again -- that loops the gateway"
            );
            let body = String::from_utf8(response.body.clone()).expect("the refusal is utf-8");
            // Rule 14 and the refusal rule together: a refusal has to say what it
            // refused and where the caller should actually go.
            assert!(body.contains("read-only"), "{method}: {body}");
            assert!(body.contains("ANONYMOUS"), "{method}: must say WHY, not just no: {body}");
            assert!(body.contains("Candid"), "{method}: must name where writes go: {body}");
        }
    }

    #[test]
    fn the_console_response_is_the_answer_and_never_asks_for_another_upgrade() {
        // The response a browser actually renders. `upgrade: Some(false)` would be
        // wrong in a quieter way than Some(true) -- the gateway treats any present
        // value as a decision -- so None is asserted exactly.
        let response = console_response(&seeded(), &[], "br5f7-7uaaa-aaaaa-qaaca-cai");
        assert_eq!(response.status_code, 200);
        assert_eq!(
            response.upgrade, None,
            "the page IS the answer; asking to be upgraded again loops the gateway"
        );
        assert!(!response.body.is_empty(), "a 200 with an empty body is the 500 all over again");
        let body = String::from_utf8(response.body.clone()).expect("the page is utf-8");
        assert!(body.contains("br5f7-7uaaa-aaaaa-qaaca-cai"), "the page names its own canister");
    }

    #[test]
    fn the_hand_written_did_declares_exactly_what_the_rust_exports() {
        // RULE 8, AND THIS FILE IS ONE OF ITS TWO COPIES. operator_admin.did is a
        // hand-written statement of this canister's interface, and lib.rs is the
        // interface. Two representations of one thing agree on the day they are
        // written and drift from then on -- and the drift here is not cosmetic:
        // dfx.json names the .did as the canister's `candid`, so it is what gets
        // INSTALLED as the declared interface. A method the Rust exports and the
        // .did omits is a method the Candid UI will not show and a caller cannot
        // discover; a method the .did declares and the Rust does not export is a
        // button in that UI that fails when pressed.
        //
        // WHY IT IS A TEST AND NOT A GENERATED FILE. Generating the .did at build
        // time is the cleaner answer and it needs a build step that runs the wasm
        // or a packtool, which dfx.json currently sets to "". This closes the gap
        // that matters -- the method sets -- with no new machinery, and
        // __export_service() means the comparison is against the REAL interface
        // rather than a paraphrase of it.
        //
        // ONLY THE METHOD NAMES AND THEIR QUERY-NESS are compared. Type names
        // legitimately differ: candid::export_service! calls the result of
        // add_pair "Result" while the .did calls it "PairsResult", and a name is
        // not an interface -- the shapes are what callers encode against, and
        // renaming a variant alias breaks nobody. Asserting on names would make
        // this test fail for reasons that are not defects, which is the kind of
        // test somebody eventually deletes.
        fn methods(did: &str) -> Vec<String> {
            let service = did
                .split_once("service :")
                .expect("no `service :` block")
                .1;
            let mut found: Vec<String> = service
                .lines()
                .map(str::trim)
                // Comments carry prose with colons and parentheses in it, and this
                // file's comments are long. Dropping them first is what keeps the
                // parse honest.
                .filter(|line| !line.starts_with("//"))
                .filter_map(|line| {
                    let (name, rest) = line.split_once(" : ")?;
                    if !rest.contains("->") {
                        return None;
                    }
                    let kind = if rest.contains("query") { "query" } else { "update" };
                    Some(format!("{name} ({kind})"))
                })
                .collect();
            found.sort();
            found
        }

        let generated = methods(&__export_service());
        let declared = methods(&include_str!("../operator_admin.did").to_string());

        assert!(!generated.is_empty(), "parsed no methods out of the generated candid");
        assert_eq!(
            declared, generated,
            "\noperator_admin.did and lib.rs disagree about this canister's interface.\n  \
             .did declares: {declared:?}\n  rust exports:  {generated:?}\n\
             dfx.json installs the .did as the declared interface, so whichever is wrong, \
             the one callers see is the .did."
        );
    }

    #[test]
    fn the_did_declares_the_upgrade_field_the_gateway_reads() {
        // Narrower than the test above and aimed at the actual 2026-10-08 defect,
        // which was not a missing METHOD but a missing FIELD. HttpResponse without
        // `upgrade` is a canister that cannot ask to be re-called as an update, and
        // the symptom is a gateway 500 the canister never sees. The method-set
        // comparison above would not have caught it.
        let did = include_str!("../operator_admin.did");
        // BOUNDED BY THE NEXT `type` DECLARATION, NOT BY THE NEXT "};", and the
        // first version of this test did the latter and failed on a correct file.
        // `headers : vec record { text; text };` is a FIELD of this very record and
        // it ends in "};", so splitting on that stopped three lines early and the
        // test reported a missing `upgrade` field to a file that had one. A test
        // that fails for a reason that is not a defect is the kind somebody
        // eventually deletes, which would have cost the check entirely.
        let after = did
            .split_once("type HttpResponse = record {")
            .expect("no HttpResponse type in the .did")
            .1;
        let response_type = after.split_once("\ntype ").map_or(after, |(head, _)| head);
        assert!(
            response_type.contains("upgrade : opt bool"),
            "operator_admin.did's HttpResponse is missing `upgrade : opt bool`. Without it the \
             HTTP gateway cannot be told to re-send as an update, every query response needs a \
             certificate this canister does not publish, and the console answers 500 \
             'Certification values not found' -- which is exactly what it did the first time \
             it was deployed. The type reads:{response_type}"
        );
    }

    #[test]
    fn the_console_response_sends_html_uncached_and_locked_down() {
        // The three headers, read out of the response rather than restated, because
        // a Content-Type the browser does not get is a page it downloads instead of
        // displaying -- which is how a 200 can still look broken.
        let response = console_response(&seeded(), &[], "aaaaa-aa");
        let header = |name: &str| {
            response
                .headers
                .iter()
                .find(|(key, _)| key.eq_ignore_ascii_case(name))
                .map(|(_, value)| value.clone())
                .unwrap_or_else(|| panic!("no {name} header; the response has {:?}", response.headers))
        };
        assert!(header("Content-Type").starts_with("text/html"), "{}", header("Content-Type"));
        assert_eq!(
            header("Cache-Control"),
            "no-store",
            "a cached posture page is the stale-truth failure this repository keeps paying for"
        );
        assert!(
            header("Content-Security-Policy").contains("default-src 'none'"),
            "{}",
            header("Content-Security-Policy")
        );
    }
}
