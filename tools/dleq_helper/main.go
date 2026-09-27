// A cross-curve DLEQ prove/verify helper, spoken to over stdin and stdout.
//
// Role: file (operator/caller entry point -- a standalone binary, not a library)
// Reads: newline-delimited JSON requests on stdin. Nothing else. No file, no
//
//	socket, no environment variable.
//
// Writes: newline-delimited JSON responses on stdout, and diagnostics on stderr.
// Can move funds: no. It holds no key material belonging to any wallet, opens
//
//	no RPC connection, and signs no transaction. It does handle a WITNESS,
//	which for a GRC<->XMR swap is one share of a Monero spend key -- see
//	"WHAT MUST NEVER BE LOGGED".
//
// Mainnet-safe: yes to run, in the sense that it cannot reach any chain. The
//
//	cryptography it performs is UNAUDITED; see the Python wrapper's module
//	docstring, which is where that warning belongs because that is the file
//	a reader of this repo will find first.
//
// # WHY THIS EXISTS RATHER THAN A PYTHON IMPLEMENTATION
//
// docs/dleq_cross_curve_design.md section 7 argues at length against writing a
// fourth Python implementation of MRL-0010 and ranks the alternatives. Its
// top-ranked option is this one, quoted:
//
//	"Call out to a maintained implementation as a separate process. This is
//	the strongest anti-build option and it is not the thing the repo has
//	already refused. solana_address.py and adaptor_ecdsa.py refuse a compiled
//	Python extension in the wallet-holding process; a small go-dleq helper
//	binary, invoked over stdin/stdout with no wallet credentials in its
//	environment, is a different trade entirely -- no in-process ABI, no C in
//	the interpreter that holds GRC_RPC_PASS, and the crypto lives in code
//	other people are running."
//
// So this is the design document's own conclusion carried out, not a detour
// around it. What it buys is shared exposure and other people's bug reports; it
// buys no proof, and go-dleq is itself unaudited and says so.
//
// The two costs the document names are real and are both here. A Go toolchain in
// deployment: that is the README next to this file. A serialization boundary:
// that is what tests/vectors/dleq_cross_curve_go.json and
// swap_terminal/modules/dleq_proof_format.py exist to pin, and it is why the
// Python side frames every proof itself before handing the bytes over.
//
// # WHY A LINE PROTOCOL AND NOT ONE PROCESS PER CALL
//
// A proof is ~65 KiB and a swap needs two of them, each verified by the other
// party. Process startup is small next to the ~150ms of curve arithmetic per
// proof, so one-shot would also have worked -- the reason for the line protocol
// is that a request carries a WITNESS on the "prove" path, and passing it as a
// command-line argument would put a Monero spend-key share in the process table
// where any user on the host can read it with ps. stdin is the only channel here
// that is not world-readable, and that is the whole argument.
//
// # WHAT MUST NEVER BE LOGGED
//
// The witness, and any error string that could contain it. Every error returned
// below is either a constant or wraps a library error over PUBLIC data (a proof,
// a point). No code path formats the witness into a message, and no code path
// writes to stderr on the prove branch. If a future change adds logging, that is
// the invariant to preserve: a side channel in a log file is still a side
// channel, and unlike the timing one (section 4.4 of the design doc) it is
// trivially avoidable.
//
// # PROTOCOL
//
// One JSON object per line in, one JSON object per line out, in order. Every
// response carries "ok"; a false "ok" carries "error". Unknown fields in a
// request are ignored, and an unparseable line gets an error response rather
// than killing the process -- a helper that exits on one bad line turns a
// caller's encoding bug into a lost swap leg.
//
//	{"op":"verify","proof":"<hex>"}
//	  -> {"ok":true,"secp256k1_point":"<hex33>","ed25519_point":"<hex32>"}
//	  -> {"ok":false,"error":"..."}
//	     The two points are echoed from the DESERIALIZED proof so a caller can
//	     check them against the keys it already holds. Verifying a proof whose
//	     commitments are not the expected keys proves nothing about those keys,
//	     and returning them here is what lets the caller notice.
//
//	{"op":"prove","witness":"<hex32-little-endian>"}
//	  -> {"ok":true,"proof":"<hex>","secp256k1_point":"...","ed25519_point":"..."}
//	     The witness must be < 2^252. go-dleq enforces it in checkWitnessSize and
//	     the error is passed through; the constraint is not ours to relax, since
//	     252 = min(ed25519 252, secp256k1 255) and a larger value is not a scalar
//	     on both curves.
//
//	{"op":"version"}
//	  -> {"ok":true,"go_dleq":"<module version>","bit_count":252}
//	     So a caller can record which implementation produced a proof it stored.
package main

import (
	"bufio"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"runtime/debug"

	dleq "github.com/athanorlabs/go-dleq"
	"github.com/athanorlabs/go-dleq/ed25519"
	"github.com/athanorlabs/go-dleq/secp256k1"
	"github.com/athanorlabs/go-dleq/types"
)

// witnessBits is min(ed25519.BitSize(), secp256k1.BitSize()) -- the same 252
// that swap_terminal/modules/dleq_proof_format.py refuses to read anything else
// for. Named here so the "version" response can report it rather than a caller
// assuming it.
const witnessBits = 252

const witnessHexLen = 64

type request struct {
	Op      string `json:"op"`
	Proof   string `json:"proof"`
	Witness string `json:"witness"`
}

type response struct {
	OK      bool   `json:"ok"`
	Error   string `json:"error,omitempty"`
	Proof   string `json:"proof,omitempty"`
	SecpKey string `json:"secp256k1_point,omitempty"`
	EdKey   string `json:"ed25519_point,omitempty"`
	GoDleq  string `json:"go_dleq,omitempty"`
	Bits    int    `json:"bit_count,omitempty"`
}

func fail(format string, args ...any) response {
	return response{OK: false, Error: fmt.Sprintf(format, args...)}
}

// goDleqVersion reads the module version out of the binary's own build info, so
// a stored proof can be attributed to an implementation without anybody having
// to remember which commit was current. It is empty for a build whose module
// info was stripped, and the caller is told "unknown" rather than being given a
// plausible-looking wrong answer.
func goDleqVersion() string {
	info, ok := debug.ReadBuildInfo()
	if !ok {
		return "unknown"
	}
	for _, dep := range info.Deps {
		if dep.Path == "github.com/athanorlabs/go-dleq" {
			if dep.Replace != nil {
				return dep.Replace.Path + "@" + dep.Replace.Version
			}
			return dep.Version
		}
	}
	return "unknown"
}

func handleVerify(curveA, curveB types.Curve, hexProof string) response {
	raw, err := hex.DecodeString(hexProof)
	if err != nil {
		return fail("proof is not hex: %v", err)
	}
	if len(raw) == 0 {
		return fail("proof is empty")
	}
	proof := new(dleq.Proof)
	if err := proof.Deserialize(curveA, curveB, raw); err != nil {
		return fail("deserialize: %v", err)
	}
	if err := proof.Verify(curveA, curveB); err != nil {
		// A verification failure is a NORMAL response, not a protocol error: a
		// counterparty sending a bad proof is exactly what this is for. It goes
		// back as ok=false with the library's reason and the process stays up.
		return fail("verify: %v", err)
	}
	return response{
		OK:      true,
		SecpKey: hex.EncodeToString(proof.CommitmentA.Encode()),
		EdKey:   hex.EncodeToString(proof.CommitmentB.Encode()),
	}
}

// handleProve takes the witness as hex rather than bytes because the transport is
// JSON, and returns no diagnostic that could echo it. errWitnessLength is a
// constant for that reason: the obvious "witness must be 32 bytes, got %q"
// would put the witness in the error.
var errWitnessLength = errors.New("witness must be 64 hex characters (32 bytes, little-endian)")

func handleProve(curveA, curveB types.Curve, hexWitness string) response {
	if len(hexWitness) != witnessHexLen {
		return fail("%v", errWitnessLength)
	}
	raw, err := hex.DecodeString(hexWitness)
	if err != nil {
		// Deliberately not wrapping err: hex.InvalidByteError includes the
		// offending byte, which is a byte of the witness.
		return fail("witness is not hex")
	}
	var witness [32]byte
	copy(witness[:], raw)

	proof, err := dleq.NewProof(curveA, curveB, witness)
	if err != nil {
		// go-dleq's NewProof errors are about the witness SIZE, not its value
		// (checkWitnessSize compares against the bit bound), so passing the
		// message through does not leak it. That is a property of the current
		// library and it is asserted by a test on the Python side rather than
		// assumed here.
		return fail("prove: %v", err)
	}
	return response{
		OK:      true,
		Proof:   hex.EncodeToString(proof.Serialize()),
		SecpKey: hex.EncodeToString(proof.CommitmentA.Encode()),
		EdKey:   hex.EncodeToString(proof.CommitmentB.Encode()),
	}
}

func main() {
	curveA := secp256k1.NewCurve()
	curveB := ed25519.NewCurve()

	reader := bufio.NewReaderSize(os.Stdin, 1<<20)
	scanner := bufio.NewScanner(reader)
	// A proof is ~65 KiB of hex, so ~130 KiB per line, and bufio.Scanner's
	// default 64 KiB limit would truncate every verify request. 4 MiB is room
	// for a proof an order of magnitude larger than any this construction
	// produces; a longer line is a caller bug and gets an error response.
	scanner.Buffer(make([]byte, 0, 1<<16), 4<<20)

	writer := bufio.NewWriter(os.Stdout)
	defer writer.Flush()
	encoder := json.NewEncoder(writer)

	emit := func(resp response) {
		_ = encoder.Encode(resp)
		// Flushed per response because the caller is blocking on this line. A
		// buffered response with no flush is a deadlock, not slow output.
		_ = writer.Flush()
	}

	for scanner.Scan() {
		line := scanner.Bytes()
		if len(line) == 0 {
			continue
		}
		var req request
		if err := json.Unmarshal(line, &req); err != nil {
			emit(fail("request is not JSON: %v", err))
			continue
		}
		switch req.Op {
		case "verify":
			emit(handleVerify(curveA, curveB, req.Proof))
		case "prove":
			emit(handleProve(curveA, curveB, req.Witness))
		case "version":
			emit(response{OK: true, GoDleq: goDleqVersion(), Bits: witnessBits})
		case "":
			emit(fail("request has no op"))
		default:
			emit(fail("unknown op %q; expected verify, prove or version", req.Op))
		}
	}
	if err := scanner.Err(); err != nil {
		fmt.Fprintf(os.Stderr, "dleq_helper: reading stdin: %v\n", err)
		os.Exit(1)
	}
}
