#!/usr/bin/env bash
# Set the environment one devnet SOL -> testnet GRC rehearsal needs. SOURCE this,
# do not run it.
#
# Role: file (operator entry point, at the repository root per CLAUDE.md rule 10)
# Reads: the two secret values, from an interactive prompt, ONLY when they are not
#       already set in this shell. It reads no file: not .env, not a conf, not a
#       wallet. It does not read ~/.GridcoinResearch.
# Writes: NOTHING to disk. No file is created, appended to or edited. The values
#       live in this shell's environment and die with it.
# Can move funds: no. It exports variables and runs nothing.
# Mainnet-safe: it pins GRC_RPC_PORT to the TEST chain and SOL_RPC_URL to devnet,
#       and refuses to overwrite a GRC_RPC_PORT you already set -- so it cannot
#       silently point you at 15715, which is the live staking wallet.
#
# WHY THIS EXISTS, COUNTED 2026-10-02.
#
# A rehearsal is: preflight, start workers, open the launcher, create a swap, send
# a deposit, watch two logs, show the swap, show the fees. Every one of those reads
# the process environment, because nothing in the serving path loads a .env on
# purpose -- config.py is a class body evaluated at import, and adding
# load_dotenv() to a module read at import is the import-time side effect rule 12
# names as a measured past defect.
#
# So the environment has to be in the shell, and on 2026-10-02 the operator lost it
# FOUR TIMES in one session -- three times to `source .venv/bin/activate` in a new
# terminal and once to a shell that was never given it at all. Each loss cost a
# round trip and produced a different confusing symptom:
#
#   no SOL_RPC_URL + no GRC    pay_test_deposit.py refused with three reasons, one
#                              of which could not even check an address
#   no GRC_RPC_PASS            readiness said NOTHING CAN BE PAID OUT, and the
#                              supervisor spawned three workers anyway while
#                              claiming a payout could broadcast
#   a WRONG SWAP_DB_PATH       three workers polled an empty database for an hour
#                              while every cycle printed IDLE
#
# None of those is a bug in this project's code, and all three are the same
# operator-facing defect: a ten-step procedure whose first step is "retype six
# exports from memory" will be got wrong, and rule 14's whole argument is that the
# screen has to carry what the operator needs rather than their recall.
#
# WHAT IT DELIBERATELY DOES NOT DO.
#
# It does not write the secrets anywhere, and it does not read them from a file.
# `.env` is off limits (the live-safety rules forbid editing it, and a file is a
# thing that gets committed, backed up and pasted), so the two secrets are prompted
# for, with no echo, and only when this shell does not already have them -- so
# sourcing it twice does not ask twice.
#
# It does not set SWAP_DB_PATH. The built-in default is correct, and the one time a
# value was exported for it the workers spent an hour on the wrong file. A variable
# that only has a wrong answer available is better left unset.

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
    echo "REFUSED: run this with \`source devnet_rehearsal_env.sh\`, not as a command." >&2
    echo "  Exports from a subshell die with it, so running it would appear to work and change nothing." >&2
    exit 2
fi

# NON-SECRET, AND PINNED TO TEST NETWORKS. Devnet and 25715 are both named here
# rather than defaulted, because the alternative value of each is real money: the
# Solana mainnet-beta cluster and Gridcoin's 15715 staking wallet.
export SOL_RPC_URL=https://api.devnet.solana.com
export SOL_DEPOSIT_ACCOUNT=CUBnQ5QBfYkL71TCqSdecAQ9xjfGmAdu6Hs3fjQeLorp
export GRC_RPC_HOST=127.0.0.1
export GRC_RPC_USER=gridcoinrpc

# REFUSES TO OVERWRITE A PORT YOU ALREADY CHOSE, and reports it, because silently
# replacing one chain selection with another is how 15715 gets polled by something
# that believed it was on testnet.
if [ -n "${GRC_RPC_PORT}" ] && [ "${GRC_RPC_PORT}" != "25715" ]; then
    echo "  GRC_RPC_PORT   ${GRC_RPC_PORT} was ALREADY SET and is left alone <- 25715 is the test chain and"
    echo "                 15715 is MAINNET. If you did not mean this one, unset it and source again."
else
    export GRC_RPC_PORT=25715
fi

# PROMPTED, NEVER STORED, AND ONLY WHEN MISSING. -s so nothing is echoed; no
# length, no fingerprint and no value is ever printed by this file.
if [ -z "${GRC_RPC_PASS}" ]; then
    read -rsp "  GRC rpcpassword from the TESTNET conf (not echoed): " GRC_RPC_PASS
    export GRC_RPC_PASS
    echo
fi
if [ -z "${GRIDCOIN_WALLET_PASSPHRASE}" ]; then
    read -rsp "  Gridcoin wallet passphrase (not echoed): " GRIDCOIN_WALLET_PASSPHRASE
    export GRIDCOIN_WALLET_PASSPHRASE
    echo
fi

# Rule 14: say what was set, so a sourced file is not a silent one. Presence for
# the secrets, values for everything else -- those are the ones worth checking by
# eye, and a secret's presence is the only fact about it that may be printed.
echo "  SOL_RPC_URL          ${SOL_RPC_URL}  <- DEVNET; readiness confirms the cluster by genesis hash"
echo "  SOL_DEPOSIT_ACCOUNT  ${SOL_DEPOSIT_ACCOUNT}"
echo "  GRC rpc              ${GRC_RPC_HOST}:${GRC_RPC_PORT} user=${GRC_RPC_USER}  <- 25715 is the test chain"
if [ -n "${GRC_RPC_PASS}" ]; then rpcpass=SET; else rpcpass=EMPTY; fi
if [ -n "${GRIDCOIN_WALLET_PASSPHRASE}" ]; then phrase=SET; else phrase=EMPTY; fi
echo "  GRC_RPC_PASS         ${rpcpass}  <- presence only. NOT a claim it is correct; a wrong one 401s"
echo "  WALLET_PASSPHRASE    ${phrase}  <- presence only. A wrong one fails at the send, not here"
unset rpcpass phrase
echo "  SWAP_DB_PATH         ${SWAP_DB_PATH:-(unset) <- correct; the built-in default is the right file}"
echo "  next                 python3 swap_readiness.py --pair SOL:GRC && python3 swap_terminal/supervisor.py start"
