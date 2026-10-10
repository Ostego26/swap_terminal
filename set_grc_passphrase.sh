#!/usr/bin/env bash
#
# Prompt for the Gridcoin wallet passphrase and store it in .env, persistently.
#
# Role: file (entry point -- what the operator runs)
# Reads: .env (one line of it, by name, never its contents to a terminal); on
#        --verify, the value the running web container actually received
# Writes: the GRIDCOIN_WALLET_PASSPHRASE line of .env, mode 600. Every other line
#        is carried through byte for byte.
# Can move funds: NO. It stores the passphrase the payout worker uses to unlock the
#        GRC wallet; it never unlocks anything, never calls an RPC, and never signs.
# Mainnet-safe: the SCRIPT is. What it arms is not: the passphrase it stores is what
#        lets workers/payout_worker.py broadcast GRC. This repository is testnet/dev
#        only -- see CLAUDE.md's chain-safety rules.
#
# =============================================================================
# WHY THIS FILE EXISTS
# =============================================================================
#
# Operator, 2026-10-10, after a host restart left the stack unable to interpolate:
# "i already fucking told you to make it presistent and you're asking me if i want
# ephemeral" and then "no create a bash prompt for me to enter the fucking
# passphrase for the wallet".
#
# They are right, and the earlier answer was wrong. `read -rs GRIDCOIN_WALLET_PASSPHRASE
# && export GRIDCOIN_WALLET_PASSPHRASE` dies with the shell. A new terminal, a reboot,
# or a second tmux pane and `docker compose up` refuses to interpolate again -- which
# is the exact failure already recorded against COMPOSE_FILE: arming meant remembering
# something on every invocation, the operator came up unarmed three times in a row, and
# a GRC payout failed with "GRIDCOIN_WALLET_PASSPHRASE is not set in this process's
# environment" after a customer's deposit had already confirmed.
#
# =============================================================================
# THE FOUR RULES THIS SCRIPT IS BUILT AROUND, AND NONE OF THEM IS OPTIONAL
# =============================================================================
#
#   never in argv       /proc/<pid>/cmdline is world-readable. The passphrase is
#                       never a command-line argument -- not to this script, not to
#                       anything it calls. That is why it is read from the terminal
#                       rather than taken as "$1", and why there is no --passphrase.
#   never echoed        `read -rs`, and the verification below compares SHA-256
#                       digests rather than values, so the passphrase is never
#                       printed, never logged, and never in scrollback. Not even its
#                       length (that is a real, if small, leak and it buys nothing).
#   never in history    `read` does not enter shell history. Typing an assignment
#                       would.
#   never a .bak        CLAUDE.md rule 2: the backup habit is what put a live
#                       GRIDCOIN_RPC_PASSWORD on GitHub via .env.bak.2026-03-28_185451.
#                       This rewrites .env through a temp file in the same directory
#                       and renames over it -- atomic, and it leaves no copy behind.
#
# =============================================================================
# WHY SINGLE QUOTES, AND WHY A PASSPHRASE CONTAINING ONE IS REFUSED
# =============================================================================
#
# compose's .env parser treats a single-quoted value as LITERAL: no escapes, no
# ${...} interpolation. That is the only encoding in which an arbitrary passphrase
# survives unchanged -- an unquoted value loses a trailing `#` to a comment, and a
# double-quoted one has `$`, `\` and `"` taken as syntax.
#
# What single quotes cannot carry is a single quote. There is no escape for one
# inside them in any dotenv dialect. So a passphrase containing `'` is REFUSED with
# the reason, rather than silently mangled: a passphrase stored wrong does not fail
# here, it fails later as "Error: The wallet passphrase entered was incorrect" in a
# payout worker, hours away from this script, with a deposit already confirmed.
#
# =============================================================================
# VERIFICATION IS A SEPARATE STEP BECAUSE IT NEEDS A RUNNING CONTAINER
# =============================================================================
#
# Writing the file proves nothing about what compose does with it (rule 17: a reason
# to believe is not a measurement). `--verify` is the measurement: it asks the
# RUNNING web container to hash the value it actually received, hashes what you type
# again here, and compares. Equal digests mean the encoding survived .env, compose's
# parser and the container's environment. Nothing but the verdict is printed.
#
set -euo pipefail

# No xtrace, ever, whatever the caller's environment says: `set -x` in a parent shell
# would print the passphrase.
set +x

#: Which file to write. Overridable so tests can point it at a temp directory -- the
#: alternative is a test that edits the real .env, which is not a test anyone should
#: run twice.
ENV_FILE="${ST_ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.env}"

#: The one variable this script owns. Named once (rule 11).
VAR_NAME="GRIDCOIN_WALLET_PASSPHRASE"

say() { printf '%s\n' "$*"; }

#: SHA-256 of stdin, hex. python3 rather than sha256sum because the web image is a
#: python image and coreutils is not guaranteed in it -- and the host and the
#: container must compute the digest the same way or the comparison is meaningless.
digest() {
    python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
}

prompt_twice() {
    # /dev/tty AND NOT stdin: this keeps working when the script is run from a pipe,
    # and it means a redirected stdin cannot feed the passphrase in without the
    # operator seeing the prompt. Tests set ST_PASSPHRASE_FD to override.
    local first second
    if [[ -n "${ST_PASSPHRASE_STDIN:-}" ]]; then
        # TEST PATH ONLY, and it is here rather than in the test so that the quoting
        # and the file rewrite under test are the REAL ones (CLAUDE.md: run the real
        # script, not a paraphrase of its logic). It reads one line from stdin and
        # skips the confirmation, which is the only difference.
        IFS= read -r first
        printf '%s' "$first"
        return 0
    fi
    printf '%s' "  ${VAR_NAME} (not echoed): " > /dev/tty
    IFS= read -rs first < /dev/tty
    printf '\n' > /dev/tty
    printf '%s' "  again, to confirm: " > /dev/tty
    IFS= read -rs second < /dev/tty
    printf '\n' > /dev/tty
    if [[ "$first" != "$second" ]]; then
        say "  REFUSED: the two entries differ. Nothing was written."
        return 1
    fi
    printf '%s' "$first"
}

cmd_set() {
    say "set_grc_passphrase: STORE"
    say "  env file          ${ENV_FILE}"
    say "  variable          ${VAR_NAME}  <- read by docker-compose.web.armed-grc.yml"
    say "  network           whatever GRIDCOIN_RPC_* points at. This repository is"
    say "                    TESTNET/DEV only; a mainnet wallet here is the operator's"
    say "                    own decision and this script cannot tell which it is."
    say "  what it arms      the container's payout worker can unlock the GRC wallet"
    say "                    and BROADCAST. That is the point of storing it."
    say ""

    local value
    value="$(prompt_twice)" || return 1

    if [[ -z "$value" ]]; then
        say "  REFUSED: empty. compose rejects an empty value exactly as it rejects an unset"
        say "           one, so writing it would leave you where you started. Nothing written."
        return 1
    fi
    if [[ "$value" == *"'"* ]]; then
        say "  REFUSED: the passphrase contains a single quote, and a single-quoted .env value"
        say "           cannot carry one in any dotenv dialect -- there is no escape for it."
        say "           Nothing was written, because storing it mangled would not fail here: it"
        say "           would fail as 'the wallet passphrase entered was incorrect' in a payout"
        say "           worker, after a deposit had already confirmed. Say the word and the"
        say "           mechanism changes (a compose secret, or a file the app reads)."
        return 1
    fi

    # umask BEFORE the file exists, not chmod after: a chmod leaves a window in which
    # the file is world-readable, and on a system holding keys that window is the whole
    # exposure.
    local previous_umask
    previous_umask="$(umask)"
    umask 077

    local temp
    temp="$(mktemp "${ENV_FILE}.new.XXXXXX")"
    # NOT a .bak and NOT a copy that outlives this script: the temp file is renamed
    # over .env below, so there is exactly one file at the end. CLAUDE.md rule 2.
    if [[ -f "$ENV_FILE" ]]; then
        grep -v "^${VAR_NAME}=" "$ENV_FILE" > "$temp" || true
    fi
    printf "%s='%s'\n" "$VAR_NAME" "$value" >> "$temp"
    mv "$temp" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    umask "$previous_umask"

    say "  WROTE             ${VAR_NAME} into ${ENV_FILE}, mode 600, single-quoted"
    say "                    every other line carried through unchanged"
    say "  persists          across new shells and reboots, which is the whole point"
    say ""
    say "  NOT YET PROVEN    writing the file says nothing about what compose does with"
    say "                    it. Bring the stack up, then run:"
    say "                        ./set_grc_passphrase.sh --verify"
    say "                    which compares digests -- it prints no value and no length."
}

cmd_verify() {
    say "set_grc_passphrase: VERIFY"
    say "  env file          ${ENV_FILE}"
    say "  how               the RUNNING web container hashes the value it actually"
    say "                    received; this hashes what you type; the two are compared."
    say "                    Neither value is printed, and neither is its length."
    say ""

    local in_container
    if ! in_container="$(docker compose exec -T web python3 -c \
        'import hashlib,os; print(hashlib.sha256(os.environb.get(b"GRIDCOIN_WALLET_PASSPHRASE", b"")).hexdigest())' \
        2>/dev/null)"; then
        say "  CANNOT VERIFY     the web container is not running, or docker compose could not"
        say "                    reach it. Start it first: docker compose up -d"
        return 2
    fi

    local empty_digest
    empty_digest="$(printf '' | digest)"
    if [[ "$in_container" == "$empty_digest" ]]; then
        say "  FAILED            the container's ${VAR_NAME} is EMPTY. compose did not pass it."
        say "                    Check that docker-compose.web.armed-grc.yml is in COMPOSE_FILE"
        say "                    in ${ENV_FILE}, and that the container was recreated after the"
        say "                    value was written -- an already-running container keeps the"
        say "                    environment it started with. docker compose up -d --force-recreate web"
        return 1
    fi

    local typed
    typed="$(prompt_twice)" || return 1
    if [[ "$in_container" == "$(printf '%s' "$typed" | digest)" ]]; then
        say "  MATCH             the container holds exactly what you just typed. The encoding"
        say "                    survived .env, compose's parser and the container environment."
        say "                    This does NOT prove the Gridcoin wallet accepts it -- the payout"
        say "                    worker's own startup banner is what proves that."
        return 0
    fi
    say "  MISMATCH          the container's value is not what you just typed."
    say "                    Either .env was written before the container started (recreate it:"
    say "                    docker compose up -d --force-recreate web), or the value was"
    say "                    altered in transit. Nothing was changed by this check."
    return 1
}

main() {
    case "${1:-}" in
        ""|--set) cmd_set ;;
        --verify) cmd_verify ;;
        -h|--help)
            say "usage: ./set_grc_passphrase.sh [--set | --verify]"
            say "  --set      (default) prompt for the passphrase and store it in .env"
            say "  --verify   prove the running container holds what you type. Prints no value."
            ;;
        *)
            say "unknown argument: $1  (there is deliberately no way to pass the passphrase as"
            say "an argument -- /proc/<pid>/cmdline is world-readable)"
            return 64
            ;;
    esac
}

main "$@"
