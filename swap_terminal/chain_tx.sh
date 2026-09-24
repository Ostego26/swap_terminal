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
RPC_USER="gridcoinrpc"
RPC_PASS="REMOVED-SEE-GIT-HISTORY-PURGE"
RPC_URL="http://127.0.0.1:25779"

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
UTXO_JSON=$(curl --silent --user "$RPC_USER:$RPC_PASS" \
  --data '{"jsonrpc": "1.0", "id": "chain", "method": "listunspent", "params": []}' \
  -H "Content-Type: application/json" "$RPC_URL")

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
NETWORK_INFO=$(curl --silent --user "$RPC_USER:$RPC_PASS" \
  --data '{"jsonrpc": "1.0", "id": "chain", "method": "getnetworkinfo", "params": []}' \
  -H "Content-Type: application/json" "$RPC_URL")
FEE_RATE=$(echo "$NETWORK_INFO" | jq -r '.result.paytxfee')
if [[ -z "$FEE_RATE" || "$FEE_RATE" == "null" ]]; then
  echo "Warning: Could not retrieve paytxfee from getnetworkinfo. Defaulting to $DEFAULT_FEE_RATE GRC/byte."
  FEE_RATE=$DEFAULT_FEE_RATE
fi
echo "Current paytxfee: $FEE_RATE GRC/byte"

echo "=== Step 3: Estimating Fee Based on Transaction Size ==="
# Create a temporary raw transaction using the full UTXO amount to measure its size.
RAW_TX_JSON_FULL=$(curl --silent --user "$RPC_USER:$RPC_PASS" \
  --data "{\"jsonrpc\": \"1.0\", \"id\": \"chain\", \"method\": \"createrawtransaction\", \"params\": [[{\"txid\":\"$TXID\", \"vout\": $VOUT}], {\"data\": \"$OP_RETURN_DATA\", \"$MAIN_ADDRESS\": $AMOUNT}]}" \
  -H "Content-Type: application/json" "$RPC_URL")
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
RAW_TX_JSON=$(curl --silent --user "$RPC_USER:$RPC_PASS" \
  --data "{\"jsonrpc\": \"1.0\", \"id\": \"chain\", \"method\": \"createrawtransaction\", \"params\": [[{\"txid\":\"$TXID\", \"vout\": $VOUT}], {\"data\": \"$OP_RETURN_DATA\", \"$MAIN_ADDRESS\": $CHANGE}]}" \
  -H "Content-Type: application/json" "$RPC_URL")
RAW_TX=$(echo "$RAW_TX_JSON" | jq -r '.result')
if [[ -z "$RAW_TX" || "$RAW_TX" == "null" ]]; then
  echo "Error creating raw transaction: $(echo "$RAW_TX_JSON" | jq -r '.error.message')"
  ERROR=1
fi
echo "Raw transaction: $RAW_TX"

echo "=== Step 5: Signing Raw Transaction ==="
SIGNED_TX_JSON=$(curl --silent --user "$RPC_USER:$RPC_PASS" \
  --data "{\"jsonrpc\": \"1.0\", \"id\": \"chain\", \"method\": \"signrawtransaction\", \"params\": [\"$RAW_TX\"]}" \
  -H "Content-Type: application/json" "$RPC_URL")
SIGNED_COMPLETE=$(echo "$SIGNED_TX_JSON" | jq -r '.result.complete')
if [ "$SIGNED_COMPLETE" != "true" ]; then
  echo "Error: Transaction signing incomplete: $(echo "$SIGNED_TX_JSON" | jq -r '.error.message')"
  ERROR=1
fi
SIGNED_TX=$(echo "$SIGNED_TX_JSON" | jq -r '.result.hex')
echo "Signed transaction: $SIGNED_TX"

echo "=== Step 6: Sending Signed Transaction ==="
SEND_TX_JSON=$(curl --silent --user "$RPC_USER:$RPC_PASS" \
  --data "{\"jsonrpc\": \"1.0\", \"id\": \"chain\", \"method\": \"sendrawtransaction\", \"params\": [\"$SIGNED_TX\"]}" \
  -H "Content-Type: application/json" "$RPC_URL")
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
