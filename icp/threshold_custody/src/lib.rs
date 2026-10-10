//! Hold threshold keys on three curves and report their PUBLIC halves. Nothing else.
//!
//! Role: canister (increment 1b of the ICP track -- multi-curve, still read-only)
//! Reads: the management canister's `ecdsa_public_key` and `schnorr_public_key`
//! Writes: its own stable config (the key name), set once at install
//! Can move funds: NO. It never produces a signature, holds no chain client, and
//!       knows no addresses. There is no code path here that signs anything,
//!       which is why this increment is safe to deploy anywhere.
//! Mainnet-safe: yes, in the only sense that applies -- it cannot spend. Which
//!       NETWORK's key it asks for is the `key_name` given at install, and that
//!       choice is the operator's; see `KeyConfig` below.
//!
//! =========================================================================
//! WHAT CHANGED IN 1b, AND WHY ONE CURVE WAS NOT ENOUGH
//! =========================================================================
//!
//! Increment 1 asked `ecdsa_public_key` for a secp256k1 key and nothing else.
//! That covers BTC, LTC and GRC -- and strands the two chains whose keys are
//! the ones currently sitting on the operator's disk. From HANDOFF.md's custody
//! table: SOL is a keypair JSON under ~/.config/solana/ and XRP is a seed in the
//! environment. Both are ed25519. A keyring that cannot express ed25519 cannot
//! take custody of either, so it could never replace the two local keys that
//! most want replacing.
//!
//! So this increment adds the second management-canister call and three named
//! curves. The set is three rather than two because `schnorr_public_key` serves
//! BOTH ed25519 and BIP-340 secp256k1, selected by its `algorithm` field, and
//! BIP-340 secp256k1 is NOT the same key as ECDSA secp256k1 -- same curve,
//! different derivation, different public key, different address. Collapsing
//! them because the curve name matches would be the quietest possible way to
//! derive an address nobody holds the key for.
//!
//! =========================================================================
//! WHY THIS CANISTER DERIVES NEITHER ADDRESSES NOR PATHS
//! =========================================================================
//!
//! Both would be a few lines, and both are deliberately absent. Increment 1
//! already argued the address half: `ecdsa_public_key` returns a 33-byte
//! compressed secp256k1 point and no address; turning that into a BTC, LTC or
//! GRC address is `base58check(version || hash160(pubkey))`, and
//! swap_terminal/modules/pubkey_address.py already does exactly that --
//! measured against a live Gridcoin daemon's `validateaddress` AND against the
//! OP_HASH160 the chain recorded in four real payouts (2026-10-05).
//!
//! The path half is new in 1b and is the same argument. The per-asset derivation
//! path scheme lives in swap_terminal/modules/keyring_paths.py, and this
//! canister takes the path as an ARGUMENT and never constructs one.
//!
//! Two implementations of one derivation is CLAUDE.md rule 8's bug with a delay
//! on it: they agree the day they are written and drift from then on, and the
//! drift is invisible because each looks correct in its own file. Worse here
//! than usual -- a canister deriving a path one way and a terminal deriving it
//! another means the terminal computes an address from the key at path A while
//! this canister would eventually sign with the key at path B, and the only
//! symptom is a signature that does not validate against the address the funds
//! are at. Keeping both tables in one language leaves nothing to drift.
//!
//! So the split is: the canister answers the one question only it can answer
//! (what is the public key for this curve and this path), and the Python side
//! answers the two it can verify offline (which path, and what address does
//! that key control). That also makes the Python side a CHECK on this one
//! rather than a consumer of it -- the same shape as the existing
//! `derive_and_check()` guards for XRP and SOL, which refuse a key paired with
//! an account it does not control.
//!
//! =========================================================================
//! WHAT 1b STILL DELIBERATELY LEAVES OUT
//! =========================================================================
//!
//! No signing, no chain reads, no timers, no funds. The method surface is still
//! exactly one update and one query, and tests/test_icp_canister_cannot_sign.py
//! pins both that count and the absence of every signing call.
//!
//! Each omission is a separate increment with its own failure modes, and the
//! research that scoped this work named them: a canister timer is destroyed by
//! upgrade, by cycle exhaustion and by firing, so an HTLC refund path must rest
//! on the on-chain timelock rather than on a timer. None of that matters yet,
//! because this cannot send anything.
//!
//! AND ONE THING THAT BLOCKS CUSTODY REGARDLESS OF THIS FILE, recorded here
//! because it is the fact that decides whether any of this is usable:
//! HANDOFF.md section 2 measured that `dfx_test_key` on a LOCAL replica is not
//! stable across container recreation -- same canister id, same key name, two
//! different keys, 2026-10-07. Funds at an address derived from a local
//! replica's key are lost when the container is replaced. That is a property of
//! `dfx_test_key`, not of canister custody: mainnet's `key_1` and a test
//! subnet's `test_key_1` are persistent threshold keys. Arming custody against
//! a local replica would lose funds, and arming it anywhere is the operator's
//! call (rule 16).

use candid::CandidType;
use ic_cdk::api::management_canister::ecdsa::{
    ecdsa_public_key, EcdsaCurve, EcdsaKeyId, EcdsaPublicKeyArgument,
};
use ic_cdk::api::management_canister::schnorr::{
    schnorr_public_key, SchnorrAlgorithm, SchnorrKeyId, SchnorrPublicKeyArgument,
};
use serde::Deserialize;
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
///
/// ONE NAME ACROSS ALL THREE CURVES, which is a claim about the IC rather than a
/// simplification: a key name is scoped per algorithm on the platform side, so
/// `key_1` names the ECDSA master key AND the Schnorr master key, and they are
/// different keys. This type therefore stays a single field, and the curve is
/// chosen per call instead. An environment that provisions one algorithm and not
/// the other surfaces as a rejected call naming the missing key, which is the
/// loud version of the same fact.
#[derive(CandidType, Deserialize, Clone, Debug)]
pub struct KeyConfig {
    pub key_name: String,
}

/// Which curve and signature scheme a key lives on.
///
/// THE RENAMES ARE THE CROSS-LANGUAGE CONTRACT. These three strings are the
/// vocabulary swap_terminal/modules/keyring_paths.py names, and
/// tests/test_keyring_curve_vocabulary.py reads both files and asserts the sets
/// are equal. Renaming a variant here without renaming it there is a caller
/// asking for a curve this canister will refuse -- which is the safe direction,
/// and the test is what keeps it from being discovered at the call site.
#[derive(CandidType, Deserialize, Clone, Copy, Debug, PartialEq, Eq)]
pub enum Curve {
    /// secp256k1 with ECDSA, via `ecdsa_public_key`. BTC, LTC, GRC.
    #[serde(rename = "secp256k1_ecdsa")]
    Secp256k1Ecdsa,
    /// ed25519 with Schnorr, via `schnorr_public_key`. SOL, XRP.
    #[serde(rename = "ed25519")]
    Ed25519,
    /// secp256k1 with BIP-340 Schnorr, via `schnorr_public_key`. Bitcoin taproot.
    /// Supported and used by no asset yet; present so that adding taproot is a
    /// row in the Python asset table rather than a change to this canister.
    #[serde(rename = "bip340secp256k1")]
    Bip340Secp256k1,
}

/// What to ask for. A record rather than two positional arguments, so that
/// adding a field later does not reorder anything a caller already sends.
#[derive(CandidType, Deserialize, Clone, Debug)]
pub struct PublicKeyRequest {
    pub curve: Curve,
    /// Built by swap_terminal/modules/keyring_paths.derivation_path(). This
    /// canister forwards it unexamined and deliberately: validating a path here
    /// would mean knowing the scheme, which would mean a second copy of it.
    pub derivation_path: Vec<Vec<u8>>,
}

/// The reply of [`public_key`], with everything a verifier needs and nothing more.
#[derive(CandidType, Deserialize, Clone, Debug)]
pub struct PublicKeyReply {
    /// Lowercase hex. 33 bytes for `Secp256k1Ecdsa` (compressed SEC1, leading
    /// 0x02 or 0x03); 32 bytes for `Ed25519` and `Bip340Secp256k1`, which carry
    /// no format prefix at all. The shapes are in [`expected_shape`].
    pub public_key_hex: String,
    /// Echoed so a verifier never has to assume which curve produced the key.
    /// Three curves under one key name give three unrelated addresses.
    pub curve: Curve,
    /// Echoed so a verifier never has to assume which path produced the key.
    /// Two paths under one curve give two unrelated addresses.
    pub derivation_path: Vec<Vec<u8>>,
    /// Echoed for the same reason, and because it is the field most likely to be
    /// wrong in a way nothing else would reveal (see [`KeyConfig`]).
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

/// The (length, allowed leading bytes) a public key on `curve` must have.
///
/// AN EMPTY PREFIX SLICE MEANS ANY LEADING BYTE IS VALID, which is true of both
/// 32-byte forms: an ed25519 public key and a BIP-340 x-only key are each a bare
/// coordinate with no parity byte and no format tag. Writing that as an empty
/// slice rather than as a special case keeps [`refuse_wrong_shape`] one branch.
///
/// Mirrored in swap_terminal/modules/keyring_paths.PUBLIC_KEY_SHAPE, which
/// re-checks the same thing after the value has crossed a dfx transport and a
/// candid text reply. That is two trust boundaries rather than rule 8's two
/// copies of one rule, and both sites say so.
pub fn expected_shape(curve: Curve) -> (usize, &'static [u8]) {
    match curve {
        Curve::Secp256k1Ecdsa => (33, &[0x02, 0x03]),
        Curve::Ed25519 => (32, &[]),
        Curve::Bip340Secp256k1 => (32, &[]),
    }
}

/// Refuse a public key that is not the shape `curve` requires.
///
/// A PURE FUNCTION ON PURPOSE (CLAUDE.md rule 10): this is the decision, and a
/// decision belongs in the smallest thing that can be called with seeded inputs.
/// The crate is built as an rlib alongside the cdylib so that `cargo test` can
/// link against it and assert on this directly, without a replica.
///
/// CHECKED AT ALL because the whole point of this canister is to hand a key to
/// something that will derive an address from it. A key of the wrong length
/// still hashes, and what it hashes to is a well-formed address nobody holds the
/// key for -- the 82.65 tGRC failure mode this repository has already paid for
/// once, reached by a different road.
pub fn refuse_wrong_shape(curve: Curve, key: &[u8]) -> Result<(), String> {
    let (length, prefixes) = expected_shape(curve);
    if key.len() != length {
        return Err(format!(
            "{curve:?} public key is {} bytes; expected {length}. No address should be \
             derived from this.",
            key.len()
        ));
    }
    if !prefixes.is_empty() && !prefixes.contains(&key[0]) {
        let allowed: Vec<String> = prefixes.iter().map(|p| format!("0x{p:02x}")).collect();
        return Err(format!(
            "{curve:?} public key starts 0x{:02x}; expected {}. No address should be \
             derived from this.",
            key[0],
            allowed.join(" or ")
        ));
    }
    Ok(())
}

/// Ask the management canister for the public key at `request`.
///
/// AN UPDATE CALL, NOT A QUERY, and not by choice: both `ecdsa_public_key` and
/// `schnorr_public_key` are inter-canister calls and a query cannot make one.
/// The practical consequence is that reading a key goes through consensus and
/// costs cycles, so a caller that needs one repeatedly should cache it -- the
/// key for a given (key_name, curve, derivation_path) does not change.
///
/// THE ERROR IS RETURNED, NOT TRAPPED. A trap gives the caller a reject with no
/// structure; a `Result` lets the terminal print the reason next to the request
/// that produced it, which is what CLAUDE.md rule 14 asks of anything an
/// operator reads.
///
/// THE CHAIN CODE IS DISCARDED, and that is worth a line because both management
/// calls return one. A chain code is for BIP32-style child derivation off the
/// returned key. This scheme does not do that: it derives by asking for a new
/// `derivation_path`, which is the platform's own mechanism, so a chain code
/// here would be a value no caller may act on. Returning it would invite exactly
/// the second derivation scheme the header argues against.
#[ic_cdk::update]
async fn public_key(request: PublicKeyRequest) -> Result<PublicKeyReply, String> {
    let config = configured()?;
    let path = request.derivation_path.clone();

    let key = match request.curve {
        Curve::Secp256k1Ecdsa => {
            let argument = EcdsaPublicKeyArgument {
                canister_id: None,
                derivation_path: path.clone(),
                key_id: EcdsaKeyId {
                    curve: EcdsaCurve::Secp256k1,
                    name: config.key_name.clone(),
                },
            };
            let (response,) = ecdsa_public_key(argument)
                .await
                .map_err(|(code, message)| {
                    format!("ecdsa_public_key failed: {code:?} {message}")
                })?;
            response.public_key
        }
        Curve::Ed25519 | Curve::Bip340Secp256k1 => {
            // ONE ARM FOR TWO CURVES because one management call serves both, and
            // the algorithm field is the only difference. Splitting it into two
            // arms would duplicate the argument construction and the error
            // mapping for the sake of one enum value.
            let algorithm = match request.curve {
                Curve::Ed25519 => SchnorrAlgorithm::Ed25519,
                // Unreachable for Secp256k1Ecdsa, which the outer match already
                // routed elsewhere. Spelled as Bip340secp256k1 rather than `_`
                // so that adding a fourth curve fails to compile here instead of
                // silently being treated as BIP-340.
                Curve::Bip340Secp256k1 | Curve::Secp256k1Ecdsa => {
                    SchnorrAlgorithm::Bip340secp256k1
                }
            };
            let argument = SchnorrPublicKeyArgument {
                canister_id: None,
                derivation_path: path.clone(),
                key_id: SchnorrKeyId {
                    algorithm,
                    name: config.key_name.clone(),
                },
            };
            let (response,) = schnorr_public_key(argument)
                .await
                .map_err(|(code, message)| {
                    format!("schnorr_public_key failed: {code:?} {message}")
                })?;
            response.public_key
        }
    };

    refuse_wrong_shape(request.curve, &key)?;

    Ok(PublicKeyReply {
        public_key_hex: key.iter().map(|b| format!("{b:02x}")).collect(),
        curve: request.curve,
        derivation_path: path,
        key_name: config.key_name,
    })
}

ic_cdk::export_candid!();

#[cfg(test)]
mod tests {
    //! Shape decisions, asserted directly. No replica, no network, no wasm.
    //!
    //! These run under `cargo test` because the crate is an rlib as well as a
    //! cdylib. Everything an `#[ic_cdk::*]` attribute touches needs a canister
    //! environment and is NOT tested here -- the Python suite pins that surface
    //! by reading the source instead (tests/test_icp_canister_cannot_sign.py).

    use super::*;

    /// A valid compressed secp256k1 point: 33 bytes, leading 0x02.
    fn secp_key() -> Vec<u8> {
        let mut key = vec![0x02];
        key.extend(std::iter::repeat_n(0xAB, 32));
        key
    }

    #[test]
    fn secp256k1_ecdsa_accepts_33_bytes_with_an_even_or_odd_prefix() {
        for prefix in [0x02u8, 0x03u8] {
            let mut key = secp_key();
            key[0] = prefix;
            assert!(refuse_wrong_shape(Curve::Secp256k1Ecdsa, &key).is_ok());
        }
    }

    #[test]
    fn secp256k1_ecdsa_refuses_an_uncompressed_key() {
        // 65 bytes, leading 0x04. Hashes to a DIFFERENT hash160 than its own
        // compressed form, so an address derived from it is one nobody holds.
        let mut key = vec![0x04];
        key.extend(std::iter::repeat_n(0xAB, 64));
        let error = refuse_wrong_shape(Curve::Secp256k1Ecdsa, &key).unwrap_err();
        assert!(error.contains("65 bytes"), "{error}");
        assert!(error.contains("No address should be derived"), "{error}");
    }

    #[test]
    fn secp256k1_ecdsa_refuses_a_32_byte_key_which_is_the_ed25519_length() {
        // THE CONFUSION THIS CATCHES IS THE LIKELY ONE: an ed25519 key handed to
        // the secp256k1 arm is exactly one byte short, and 32 bytes of key
        // material look entirely reasonable.
        let key = vec![0xAB; 32];
        let error = refuse_wrong_shape(Curve::Secp256k1Ecdsa, &key).unwrap_err();
        assert!(error.contains("32 bytes"), "{error}");
        assert!(error.contains("expected 33"), "{error}");
    }

    #[test]
    fn ed25519_accepts_any_leading_byte_because_it_has_no_format_prefix() {
        for prefix in [0x00u8, 0x02, 0x04, 0xED, 0xFF] {
            let mut key = vec![prefix];
            key.extend(std::iter::repeat_n(0xAB, 31));
            assert_eq!(key.len(), 32);
            assert!(
                refuse_wrong_shape(Curve::Ed25519, &key).is_ok(),
                "0x{prefix:02x} was refused; ed25519 keys carry no prefix"
            );
        }
    }

    #[test]
    fn ed25519_refuses_33_bytes_which_is_xrps_published_form() {
        // XRP publishes an ed25519 key as 0xED followed by the 32 raw bytes. The
        // canister returns the BARE 32, so a 33-byte value arriving here means
        // somebody added the prefix on the wrong side of the boundary.
        let mut key = vec![0xED];
        key.extend(std::iter::repeat_n(0xAB, 32));
        let error = refuse_wrong_shape(Curve::Ed25519, &key).unwrap_err();
        assert!(error.contains("33 bytes"), "{error}");
        assert!(error.contains("expected 32"), "{error}");
    }

    #[test]
    fn bip340_has_the_same_shape_as_ed25519_and_is_still_a_different_key() {
        // Same 32-byte x-only shape, so the shape check cannot tell them apart.
        // That is exactly why `curve` is echoed in the reply: the shape does not
        // identify the curve, and the caller must not infer it.
        assert_eq!(
            expected_shape(Curve::Bip340Secp256k1),
            expected_shape(Curve::Ed25519)
        );
        assert_ne!(Curve::Bip340Secp256k1, Curve::Ed25519);
    }

    #[test]
    fn every_curve_has_a_shape() {
        // A curve added to the enum without a shape fails to compile in
        // `expected_shape`, which is the real guard. This asserts the lengths are
        // sane rather than that the match is exhaustive.
        for curve in [
            Curve::Secp256k1Ecdsa,
            Curve::Ed25519,
            Curve::Bip340Secp256k1,
        ] {
            let (length, _) = expected_shape(curve);
            assert!((32..=33).contains(&length), "{curve:?} has length {length}");
        }
    }

    #[test]
    fn an_empty_key_is_refused_rather_than_indexed() {
        // The prefix check reads key[0]. An empty slice must be caught by the
        // length branch first, or this panics instead of refusing.
        for curve in [
            Curve::Secp256k1Ecdsa,
            Curve::Ed25519,
            Curve::Bip340Secp256k1,
        ] {
            assert!(refuse_wrong_shape(curve, &[]).is_err(), "{curve:?}");
        }
    }
}
