#!/bin/bash
# chain_tx.sh
# This script chains together several Gridcoin RPC calls.
# It does the following:
#   1. Fetches available UTXOs (using listunspent) and selects the first one.
#   2. Retrieves the current network fee rate (paytxfee) in GRC per byte.
#   3. Creates a temporary raw transaction (using the full UTXO amount) to measure its size.
#   4. Computes the fee as:
#          fee = max(TX_SIZE_BYTES * fee_rate, MIN_FEE)
#   5. Creates a final raw transaction with two outputs:
#         - An OP_RETURN output containing your supplied hex data.
#         - A change output sending (UTXO amount – fee) to the change address.
#   6. Signs the raw transaction using signrawtransaction.
#   7. Sends the signed transaction with sendrawtransaction.

# --- Configuration ---
#
# CREDENTIALS COME FROM THE ENVIRONMENT, AND THEY DID NOT USED TO.
#
# RPC_PASS was a hardcoded literal on this line until 2026-09-25. The value is
# deliberately not repeated here: a comment naming it would keep the string in
# the tree, trip every secret scanner, and survive the history rewrite meant to
# remove it -- documenting a leak by reproducing it.
# It was a TESTNET credential -- port 25779 is Gridcoin's test chain, 15715 is
# mainnet -- so nothing of value was ever behind it. It still had to go, for
# one reason: a hardcoded secret is a pattern, and the next person to copy this
# file will be pointing it at something that is not testnet.
#
# It was also committed and pushed (b3aa36a), which is why removing it from
# this line does not finish the job. A pushed credential is published; the only
# step that changes anything is rotating it at the daemon.
RPC_USER="${GRIDCOIN_RPC_USER:-gridcoinrpc}"
RPC_PASS="${GRIDCOIN_RPC_PASS:-}"
RPC_URL="${GRIDCOIN_RPC_URL:-http://127.0.0.1:25779}"

if [ -z "$RPC_PASS" ]; then
  echo "REFUSED: GRIDCOIN_RPC_PASS is not set, so nothing was sent." >&2
  echo "  This script signs and broadcasts a transaction, and an unauthenticated" >&2
  echo "  RPC call fails in a way that looks like an empty wallet rather than a" >&2
  echo "  missing password -- which is the more expensive failure." >&2
  echo "  Set it from your gridcoinresearch.conf rpcpassword:" >&2
  echo "      export GRIDCOIN_RPC_PASS=...    # never paste it into a terminal you share" >&2
  exit 2
fi

# ONE RPC CALL, IN ONE PLACE, AND THE CREDENTIAL NEVER TOUCHES argv.
#
# This replaces six near-identical `curl --silent --user "$RPC_USER:$RPC_PASS"`
# invocations (rule 8: two copies of one rule is a bug with a delay on it; six
# is five delays). Both defects were in every copy:
#
#   1. --user puts "user:password" in the process command line, which ANY other
#      user on the machine can read out of /proc while it runs. Piping a config
#      file in on stdin keeps it off argv entirely -- curl reads `user = ...`
#      from the config and never publishes it.
#   2. Changing the call shape meant changing it six times, and the copies
#      would have drifted the moment one of them needed a flag the others did
#      not.
#
# params defaults to [] because most of these calls take none.
grc_rpc() {
  local method="$1"
  local params="${2:-[]}"
  printf 'user = "%s:%s"\n' "$RPC_USER" "$RPC_PASS" | curl --silent --config - \
    --data "{\"jsonrpc\": \"1.0\", \"id\": \"chain\", \"method\": \"$method\", \"params\": $params}" \
    -H "Content-Type: application/json" "$RPC_URL"
}

# Use the network’s fee rate (GRC per byte) as returned by getnetworkinfo.
DEFAULT_FEE_RATE=0.001
# Set a minimum fee for nonstandard transactions.
MIN_FEE=135

# --- Check argument ---
if [ -z "$1" ]; then
  echo "Usage: $0 <OP_RETURN_HEX>"
  echo "Example: $0 95377d3310ae7eef71557753dad3d6f575e032f99998579f51924a29db0f463b"
  read -r -p "Press Enter to exit..."
  exit 1
fi

OP_RETURN_DATA="$1"
if [ ${#OP_RETURN_DATA} -ne 64 ]; then
  echo "Warning: Expected a 64-character hex string for OP_RETURN data, got ${#OP_RETURN_DATA}."
fi

ERROR=0

echo "=== Step 1: Fetching UTXOs ==="
UTXO_JSON=$(grc_rpc listunspent)

TXID=$(echo "$UTXO_JSON" | jq -r '.result[0].txid')
VOUT=$(echo "$UTXO_JSON" | jq -r '.result[0].vout')
ADDRESS=$(echo "$UTXO_JSON" | jq -r '.result[0].address')
AMOUNT=$(echo "$UTXO_JSON" | jq -r '.result[0].amount')

if [[ -z "$TXID" || "$TXID" == "null" ]]; then
  echo "Error: Could not retrieve a UTXO. Ensure your wallet is funded and unlocked."
  ERROR=1
fi

echo "Selected UTXO:"
echo "  TXID:    $TXID"
echo "  VOUT:    $VOUT"
echo "  Address: $ADDRESS"
echo "  Amount:  $AMOUNT"

# Use the main address and change address:
MAIN_ADDRESS="mre8bKn5zM72oVCk3W6noNwajtoFEqpHhT"
CHANGE_ADDRESS="mg3G6MkQSxMp1iCUYFH5JztynohhGYHPDA"  # Change address

echo "=== Step 2: Retrieving Network Fee Rate ==="
NETWORK_INFO=$(grc_rpc getnetworkinfo)
FEE_RATE=$(echo "$NETWORK_INFO" | jq -r '.result.paytxfee')
if [[ -z "$FEE_RATE" || "$FEE_RATE" == "null" ]]; then
  echo "Warning: Could not retrieve paytxfee from getnetworkinfo. Defaulting to $DEFAULT_FEE_RATE GRC/byte."
  FEE_RATE=$DEFAULT_FEE_RATE
fi
echo "Current paytxfee: $FEE_RATE GRC/byte"

echo "=== Step 3: Estimating Fee Based on Transaction Size ==="
# Create a temporary raw transaction using the full UTXO amount to measure its size.
RAW_TX_JSON_FULL=$(grc_rpc createrawtransaction \
  "[[{\"txid\":\"$TXID\", \"vout\": $VOUT}], {\"data\": \"$OP_RETURN_DATA\", \"$MAIN_ADDRESS\": $AMOUNT}]")
RAW_TX_FULL=$(echo "$RAW_TX_JSON_FULL" | jq -r '.result')
if [[ -z "$RAW_TX_FULL" || "$RAW_TX_FULL" == "null" ]]; then
  echo "Error creating full raw transaction: $(echo "$RAW_TX_JSON_FULL" | jq -r '.error.message')"
  ERROR=1
fi

# Calculate transaction size in bytes (2 hex digits = 1 byte).
TX_SIZE_BYTES=$(echo -n "$RAW_TX_FULL" | wc -c | awk '{printf "%.0f", $1/2}')
echo "Raw transaction (full amount) size: $TX_SIZE_BYTES bytes"

# Compute fee based on fee rate (GRC per byte).
COMPUTED_FEE=$(echo "$TX_SIZE_BYTES * $FEE_RATE" | bc -l)
# Force fee to be at least MIN_FEE.
if (( $(echo "$COMPUTED_FEE < $MIN_FEE" | bc -l) )); then
  FEE=$MIN_FEE
else
  FEE=$COMPUTED_FEE
fi
echo "Computed fee (@ $FEE_RATE GRC/byte, min fee $MIN_FEE GRC): $FEE GRC"

CHANGE=$(echo "$AMOUNT - $FEE" | bc -l)
echo "Calculated change amount (Amount - Fee): $CHANGE"
if (( $(echo "$CHANGE <= 0" | bc -l) )); then
  echo "Error: Fee is greater than or equal to UTXO amount."
  read -p "Press Enter to exit..."
  exit 1
fi

echo "=== Step 4: Creating Raw Transaction ==="
RAW_TX_JSON=$(grc_rpc createrawtransaction \
  "[[{\"txid\":\"$TXID\", \"vout\": $VOUT}], {\"data\": \"$OP_RETURN_DATA\", \"$MAIN_ADDRESS\": $CHANGE}]")
RAW_TX=$(echo "$RAW_TX_JSON" | jq -r '.result')
if [[ -z "$RAW_TX" || "$RAW_TX" == "null" ]]; then
  echo "Error creating raw transaction: $(echo "$RAW_TX_JSON" | jq -r '.error.message')"
  ERROR=1
fi
echo "Raw transaction: $RAW_TX"

echo "=== Step 5: Signing Raw Transaction ==="
SIGNED_TX_JSON=$(grc_rpc signrawtransaction "[\"$RAW_TX\"]")
SIGNED_COMPLETE=$(echo "$SIGNED_TX_JSON" | jq -r '.result.complete')
if [ "$SIGNED_COMPLETE" != "true" ]; then
  echo "Error: Transaction signing incomplete: $(echo "$SIGNED_TX_JSON" | jq -r '.error.message')"
  ERROR=1
fi
SIGNED_TX=$(echo "$SIGNED_TX_JSON" | jq -r '.result.hex')
echo "Signed transaction: $SIGNED_TX"

echo "=== Step 6: Sending Signed Transaction ==="
SEND_TX_JSON=$(grc_rpc sendrawtransaction "[\"$SIGNED_TX\"]")
SEND_TX=$(echo "$SEND_TX_JSON" | jq -r '.result')
if [ "$SEND_TX" = "null" ]; then
  echo "Error sending transaction: $(echo "$SEND_TX_JSON" | jq -r '.error.message')"
  ERROR=1
fi

if [ $ERROR -eq 0 ]; then
  echo "Transaction successfully sent!"
  echo "TXID: $SEND_TX"
else
  echo "One or more errors occurred. Please review the output above."
fi

echo "=== End of Script Output ==="
read -r -p "Press Enter to exit..."
