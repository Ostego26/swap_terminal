# abstergo_exchange -- the Node bridge

    Role: file (entry point: `node server.js`)
    Reads: the process environment (Gridcoin RPC credentials, DEVNET_RPC_URL,
           SOLANA_PAYER_KEYPAIR_PATH), and `swap_intents.json`
    Writes: `swap_intents.json`
    Can move funds: YES -- `sendSolPayout()` signs and broadcasts real SOL
    Mainnet-safe: NO. Leaving `SOLANA_PAYER_KEYPAIR_PATH` unset leaves it
           unarmed, which `auth.js` documents as the supported read-only posture.

An Express server that verifies a Gridcoin deposit and pays out on Solana. It is
one of the nine process kinds in `docs/containerization_design_2026_10_03.md`
(service `S3. abstergo`), and `swap_terminal/chains/solana_transaction.py` names
it as the thing whose payout shapes the Python Solana adapter was written
against.

## THIS README USED TO DESCRIBE A REACT FRONTEND THAT NO LONGER EXISTS

Verbatim, it was Vite's scaffold text: "# React + Vite / This template provides
a minimal setup to get React working in Vite with HMR and some ESLint rules."
That frontend was deleted on 2026-10-04 -- eighteen files of the thirty-four
tracked here, 1,228 lines -- because the operator asked for one UI and this
repository had two. The survivor is the Flask/Jinja app under
`swap_terminal/templates/`, which is the one the operator actually runs.

Why the React half lost, measured rather than assumed:

  - **It could not build.** `npm install && npm run build` exited 1 with
    `Could not resolve "./IntentForm" from "src/App.jsx"`. `src/App.jsx`
    imported `./IntentForm`, `./IntentStatusCard` and `./OperatorQueue`, and
    none of the three existed anywhere in the tree under any extension Vite
    resolves.
  - **Nothing referenced it.** Every frontend filename was grepped across the
    whole tree, all file types, not just the import graph (`.sh`, `.desktop`,
    `package.json` scripts, the containerization doc, `swap_terminal_desktop.py`,
    `install_desktop_icon.py`). Zero external references. The one `SwapForm.jsx`
    hit in the root `CLAUDE.md` is its note about a leaked
    `SwapForm.jsx.bak.2026-03-26_112534`, not a caller.
  - **Four of its six components were orphans inside their own tree**, and they
    called four endpoints this server does not have: `/get-exchange-wallet`,
    `/deposit`, `/swap/grc-to-sol` and `/swap/sol-to-grc`, against the eight
    routes `server.js` actually serves.
  - **The one near-reachable component posted field names this server does not
    read.** `SwapIntentForm.jsx` POSTed `sourceAmount` / `destinationAddress` /
    `notes`; `POST /swap-intents` reads `grcAmount` and
    `destinationSolanaAddress` (or `userAddress`) and builds the intent
    field-by-field with no spread, so `notes` was discarded and the amount
    never arrived. `notes` appears nowhere else in this directory.

Nothing was ported, because the Flask UI is a strict superset of every feature
the React tree actually had. The one thing with no Flask equivalent was
`WalletConnection.jsx` (Phantom / MetaMask / Coinbase / Brave), and it was
deleted rather than ported: nothing imported it, it made no call to this server,
it invoked an `onConnect` prop no parent supplied, and a browser wallet connector
has no function in a custodial deposit-address desk, where nothing is signed in
the browser. Porting it would have been writing a feature, not merging one.

## What is here now

    server.js              the HTTP surface: 8 routes. The only thing that signs.
    auth.js                shared-secret check, constant-time.
    intent_store.js        swap_intents.json, with a cross-process advisory lock.
    services/coinGecko.js  price lookup. Imported by nothing today.
    services/gridcoin.js   a SECOND Express app, imported by nothing today.
    scan_solana_wallets.js operator diagnostic: walks a tree printing keypair pubkeys.
    rotate_solana_key.mjs  the key-rotation helper `docs/key_exposure_runbook.md` names.
    testAddress.js         a one-line PublicKey parse check.
    tests/                 32 tests over auth.js and intent_store.js.

`server.js` imports only `auth.js` and `intent_store.js`. It serves no static
files and no HTML -- there is no `express.static`, no `sendFile`, no `dist` and
no `build` anywhere in it -- which is why removing the frontend could not affect
it, and why this directory never served a page to begin with. The React app was
a separate Vite dev server that talked to it over CORS at `localhost:5000`.

## The caveat that has not moved

`swap_intents.json` is still this server's authority for a fund-releasing
decision, and this server still has no connection to `swap_terminal.db`. So
there remain two systems of record for a swap and nothing reconciles them --
rule 15, and it is named work rather than a baseline.
`swap_terminal/migrate_swap_intents.py` reads the JSON file but does not repoint
the server at the database.
