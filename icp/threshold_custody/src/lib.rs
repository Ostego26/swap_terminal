//! Hold one threshold key and report its PUBLIC half. Nothing else, on purpose.
//!
//! Role: canister (increment 1 of the ICP track)
//! Reads: the management canister's `ecdsa_public_key`
//! Writes: its own stable config (the key name), set once at install
//! Can move funds: NO. It never calls `sign_with_ecdsa`, holds no chain client,
//!       and knows no addresses. There is no code path here that produces a
//!       signature, which is why this increment is safe to deploy anywhere.
//! Mainnet-safe: yes, in the only sense that applies -- it cannot spend. Which
//!       NETWORK's key it asks for is the `key_name` given at install, and that
//!       choice is the operator's; see `KeyConfig` below.
//!
//! =========================================================================
//! WHY THIS CANISTER DOES NOT DERIVE ADDRESSES
//! =========================================================================
//!
//! It would be four lines, and it is deliberately absent. `ecdsa_public_key`
//! returns a 33-byte compressed secp256k1 point and no address; turning that
//! into a BTC, LTC or GRC address is `base58check(version || hash160(pubkey))`,
//! and swap_terminal/modules/pubkey_address.py already does exactly that --
//! measured against a live Gridcoin daemon's `validateaddress` AND against the
//! OP_HASH160 the chain recorded in four real payouts (2026-10-05).
//!
//! Two implementations of one derivation is CLAUDE.md rule 8's bug with a delay
//! on it: they agree the day they are written and drift from then on, and the
//! drift is invisible because each looks correct in its own file. Worse here
//! than usual -- a canister deriving its own address and a terminal deriving the
//! canister's address disagreeing by one version byte means funds sent to an
//! address nobody holds the key for.
//!
//! So the split is: the canister answers the one question only it can answer
//! (what is the public key), and the Python side answers the one it can verify
//! (what address does that key control). That also makes the Python side a
//! CHECK on this one rather than a consumer of it -- the same shape as the
//! existing `derive_and_check()` guards for XRP and SOL, which refuse a key
//! paired with an account it does not control.
//!
//! =========================================================================
//! WHAT INCREMENT 1 DELIBERATELY LEAVES OUT
//! =========================================================================
//!
//! No signing, no chain reads, no timers, no funds. Each of those is a separate
//! increment with its own failure modes, and the research that scoped this work
//! named them: a canister timer is destroyed by upgrade, by cycle exhaustion and
//! by firing, so an HTLC refund path must rest on the on-chain timelock rather
//! than on a timer. None of that matters yet, because this cannot send anything.

use candid::CandidType;
use serde::Deserialize;
use ic_cdk::api::management_canister::ecdsa::{
    ecdsa_public_key, EcdsaCurve, EcdsaKeyId, EcdsaPublicKeyArgument,
};
use std::cell::RefCell;

/// The key name this canister asks the management canister for.
///
/// THERE IS NO DEFAULT, AND THAT IS THE WHOLE DESIGN OF THIS TYPE. The names
/// differ per environment -- `dfx_test_key` on a local replica, `test_key_1` and
/// `key_1` on mainnet -- and they are not interchangeable: each is a DIFFERENT
/// master key, so the same derivation path under two names gives two different
/// public keys and therefore two different addresses. A default would mean a
/// canister that silently reports an address for a key the operator did not
/// choose, which is the "valid-looking address nobody holds" failure the Python
/// side already refuses for uncompressed keys.
#[derive(CandidType, Deserialize, Clone, Debug)]
pub struct KeyConfig {
    pub key_name: String,
}

thread_local! {
    static CONFIG: RefCell<Option<KeyConfig>> = const { RefCell::new(None) };
}

#[ic_cdk::init]
fn init(config: KeyConfig) {
    CONFIG.with(|c| *c.borrow_mut() = Some(config));
}

/// Re-read after an upgrade. Stable memory is not used because there is nothing
/// here worth keeping that the installer does not re-supply: the config arrives
/// as an upgrade argument, and a canister that silently kept a STALE key name
/// across an upgrade would report addresses for a key the new install did not
/// name.
#[ic_cdk::post_upgrade]
fn post_upgrade(config: KeyConfig) {
    CONFIG.with(|c| *c.borrow_mut() = Some(config));
}

fn configured() -> Result<KeyConfig, String> {
    CONFIG
        .with(|c| c.borrow().clone())
        .ok_or_else(|| "no key_name configured: this canister was installed without one".to_string())
}

/// What this canister is set to ask for. A query, so it costs nothing to check.
#[ic_cdk::query]
fn config() -> Result<KeyConfig, String> {
    configured()
}

/// The reply of [`public_key`], with everything a verifier needs and nothing more.
#[derive(CandidType, Deserialize, Clone, Debug)]
pub struct PublicKeyReply {
    /// 33 bytes, compressed secp256k1, lowercase hex. The form
    /// `swap_terminal/modules/pubkey_address.address_from_public_key` requires,
    /// and the form it REFUSES to accept any other version of.
    pub public_key_hex: String,
    /// Echoed so a verifier never has to assume which path produced the key.
    /// Two paths under one key name give two unrelated addresses.
    pub derivation_path: Vec<Vec<u8>>,
    /// Echoed for the same reason, and because it is the field most likely to be
    /// wrong in a way nothing else would reveal (see [`KeyConfig`]).
    pub key_name: String,
}

/// Ask the management canister for the public key at `derivation_path`.
///
/// AN UPDATE CALL, NOT A QUERY, and not by choice: `ecdsa_public_key` is an
/// inter-canister call and a query cannot make one. The practical consequence is
/// that reading this key goes through consensus and costs cycles, so a caller
/// that needs it repeatedly should cache it -- the key for a given (key_name,
/// derivation_path) does not change.
///
/// THE ERROR IS RETURNED, NOT TRAPPED. A trap gives the caller a reject with no
/// structure; a `Result` lets the terminal print the reason next to the request
/// that produced it, which is what CLAUDE.md rule 14 asks of anything an
/// operator reads.
#[ic_cdk::update]
async fn public_key(derivation_path: Vec<Vec<u8>>) -> Result<PublicKeyReply, String> {
    let config = configured()?;
    let argument = EcdsaPublicKeyArgument {
        canister_id: None,
        derivation_path: derivation_path.clone(),
        key_id: EcdsaKeyId {
            curve: EcdsaCurve::Secp256k1,
            name: config.key_name.clone(),
        },
    };
    let (response,) = ecdsa_public_key(argument)
        .await
        .map_err(|(code, message)| format!("ecdsa_public_key failed: {code:?} {message}"))?;

    // CHECKED HERE RATHER THAN ASSUMED, because the whole point of this canister
    // is to hand a key to something that will derive an address from it. The
    // Python side refuses anything that is not 33 bytes starting 0x02/0x03; this
    // refuses it one hop earlier, where the reason is still attributable to the
    // management canister rather than to the caller.
    let key = &response.public_key;
    if key.len() != 33 || !(key[0] == 0x02 || key[0] == 0x03) {
        return Err(format!(
            "ecdsa_public_key returned {} bytes starting 0x{:02x}; expected 33 bytes starting \
             0x02 or 0x03 (compressed secp256k1). No address should be derived from this.",
            key.len(),
            key.first().copied().unwrap_or(0)
        ));
    }

    Ok(PublicKeyReply {
        public_key_hex: key.iter().map(|b| format!("{b:02x}")).collect(),
        derivation_path,
        key_name: config.key_name,
    })
}

ic_cdk::export_candid!();
