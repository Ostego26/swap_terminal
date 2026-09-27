// Emit cross-check vectors for a Python port of cross-curve DLEQ.
// Deterministic witnesses, so the output is reproducible and diffable.
package main

import (
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"

	dleq "github.com/athanorlabs/go-dleq"
	"github.com/athanorlabs/go-dleq/ed25519"
	"github.com/athanorlabs/go-dleq/secp256k1"
)

type vector struct {
	Label    string `json:"label"`
	Witness  string `json:"witness_hex_le32"`
	SecpKey  string `json:"secp256k1_point"`
	EdKey    string `json:"ed25519_point"`
	ProofLen int    `json:"proof_len_bytes"`
	Proof    string `json:"proof_hex"`
	Verifies bool   `json:"verifies"`
}

func main() {
	curveA := secp256k1.NewCurve()
	curveB := ed25519.NewCurve()

	// Deterministic witnesses. Each must be < 2^252 so it is a valid scalar on
	// BOTH curves; the library enforces that in checkWitnessSize.
	witnesses := map[string][32]byte{}
	var one [32]byte
	one[0] = 1
	witnesses["one"] = one
	var counting [32]byte
	for i := 0; i < 31; i++ {
		counting[i] = byte(i + 1)
	}
	witnesses["counting_1_to_31"] = counting
	var high [32]byte
	for i := 0; i < 31; i++ {
		high[i] = 0xff
	}
	high[31] = 0x0f // keep it under 2^252
	witnesses["just_under_2_252"] = high

	out := []vector{}
	for label, x := range witnesses {
		proof, err := dleq.NewProof(curveA, curveB, x)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s: NewProof: %v\n", label, err)
			os.Exit(1)
		}
		verr := proof.Verify(curveA, curveB)
		ser := proof.Serialize()
		out = append(out, vector{
			Label:    label,
			Witness:  hex.EncodeToString(x[:]),
			SecpKey:  hex.EncodeToString(proof.CommitmentA.Encode()),
			EdKey:    hex.EncodeToString(proof.CommitmentB.Encode()),
			ProofLen: len(ser),
			Proof:    hex.EncodeToString(ser),
			Verifies: verr == nil,
		})
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	_ = enc.Encode(out)
}
