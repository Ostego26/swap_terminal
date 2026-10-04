/*
 * ESLint for the Node half of this directory, and ONLY the Node half, because
 * as of 2026-10-04 that is all there is here.
 *
 * Role: submodule (lint configuration; no runtime behavior)
 * Reads: the .js/.mjs files in this directory
 * Writes: nothing
 * Can move funds: no
 * Mainnet-safe: yes -- it is never imported by server.js
 *
 * WHAT THIS FILE USED TO BE, and why it could not stay. It configured a React
 * frontend: it imported eslint-plugin-react, eslint-plugin-react-hooks and
 * eslint-plugin-react-refresh, set `settings: { react: { version: '18.3' } }`,
 * matched `**\/*.{js,jsx}` and declared `globals.browser`. That frontend was
 * deleted in the same commit as this rewrite -- eighteen files, including
 * src/App.jsx, which could not build (`vite build` exited 1 on
 * `Could not resolve "./IntentForm"`, measured 2026-10-04). Leaving the old
 * config would have left `npm run lint` importing three plugins that are no
 * longer in package.json, so the lint script would have died on a missing
 * module rather than reporting on the code -- a check that cannot run is worse
 * than no check, because it reports nothing while looking configured.
 *
 * `globals.node`, NOT `globals.browser`, AND THAT IS THE CORRECTION THAT
 * MATTERS. Every surviving file here runs under node: server.js, auth.js,
 * intent_store.js, services/, the two test files, and the three operator
 * scripts. The old config declared browser globals over all of them, so
 * `process`, `Buffer`, `__dirname` and `console` were undeclared while
 * `window` and `document` were -- exactly backwards for a file that reads
 * process.env to find RPC credentials. It never failed loudly because
 * `no-undef` was the only rule that would have caught it and the react
 * recommended sets do not turn it up.
 *
 * The `dist` ignore is gone with the build that produced it. Flat config
 * already ignores node_modules by default, so nothing replaces it.
 */
import js from '@eslint/js'
import globals from 'globals'

export default [
  {
    files: ['**/*.{js,mjs}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: globals.node,
    },
    rules: {
      ...js.configs.recommended.rules,
    },
  },
]
