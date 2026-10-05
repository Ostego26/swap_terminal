# The Express swap-intent server, containerized.
#
# Role: container image for S3 `abstergo` in docs/containerization_design_2026_10_03.md
# Reads: the process environment; swap_intents.json through a VOLUME, never from
#        the image. Optionally a Solana keypair, read-only, if SOL payouts are armed.
# Writes: swap_intents.json, on the volume.
# Can move funds: YES, indirectly and this is the reason the volume matters.
#        swap_intents.json is a FUND-RELEASING AUTHORITY with no link to
#        swap_terminal.db -- see §12 of the design doc. An intent recorded there
#        is what a later verify step reads to decide a release. Baking it into
#        the image, or letting it live on the container's writable layer, means
#        a rebuild or a `docker compose down` silently discards records that
#        decide whether money moves.
# Live-safe: the image itself holds no secret and no key. Every credential
#        arrives through the environment at RUN time (compose `environment:`),
#        which is the only reason this file can be committed.
#
# WHY INCREMENT 1 (design doc §9): this server touches no SQLite, shares no PID
# namespace with anything, and TODAY HAS NO REAPER AT ALL -- no pid file, no stop
# path, nothing in the tree that can prove it stopped. So the only thing a
# container can regress here is a process nothing can currently stop. That makes
# it the smallest blast radius in the system and the clearest win.
#
# NODE 22 BECAUSE THAT IS WHAT IT RUNS ON. Measured on the operator's host
# 2026-10-05: `node --version` -> v22.22.2, and package.json pins no `engines`
# field, so nothing in the repository recorded this. Pinning the major here is
# the first time that version is written down anywhere.
FROM node:22-alpine AS deps

WORKDIR /app

# PACKAGE MANIFEST FIRST, SOURCE SECOND, so a source edit does not invalidate the
# dependency layer. yarn.lock and not package-lock.json: the repository carries a
# yarn.lock (1,613 lines after 97c820a pruned the dead React tree) and no
# package-lock.json. Installing with npm here would resolve a DIFFERENT dependency
# graph from the one the lockfile pins, which is the whole point of having one.
COPY package.json yarn.lock ./

# corepack ships with Node 22 and is how yarn is obtained without a global install.
# --frozen-lockfile makes a lockfile that disagrees with package.json a BUILD
# FAILURE rather than a silent resolution -- the build is the right place to find
# that out. --production drops the three eslint devDependencies, which have no
# business in a running image.
RUN corepack enable && yarn install --frozen-lockfile --production

FROM node:22-alpine AS runtime

# dumb-init IS NOT DECORATION, and rule 13 is why. node as PID 1 does not get the
# default SIGTERM handler, so `docker compose stop` would wait out its full
# timeout and then SIGKILL -- which is exactly the "a stop that cannot prove it
# worked is not a stop" failure this increment exists to fix. dumb-init runs as
# PID 1, forwards the signal, and lets node exit on its own.
RUN apk add --no-cache dumb-init

WORKDIR /app
ENV NODE_ENV=production

COPY --from=deps /app/node_modules ./node_modules
# NAMED, NOT `COPY . .`. Everything this server needs and nothing it does not:
# no tests, no eslint config, no scan/test scripts, and no chance of a stray
# swap_intents.json from the build context ending up baked into the image where
# it would shadow the volume.
COPY package.json server.js auth.js intent_store.js ./

# services/ IS DELIBERATELY NOT COPIED, and it is the one omission worth a
# paragraph. Measured 2026-10-05: server.js's only local imports are './auth.js'
# and './intent_store.js' (server.js:48-63). NOTHING imports services/ -- every
# other reference to it in the tree is a comment or a test.
#
# And it is not merely unused. auth.js:37 documents what is in there:
#
#     services/gridcoin.js    POST /deposit    no   <-- sendtoaddress
#
# an UNAUTHENTICATED route that sends Gridcoin, in a second Express app that
# auth.js:46 itself calls dead. Copying it in would put a money-moving endpoint
# inside the image where anyone who can `docker compose exec` into the container
# could run it, for code the running server never loads. Rule 9 says leave less
# behind; this is the version of that rule where the dead code can spend money.
#
# NOT DELETED FROM THE REPOSITORY by this change -- that is a separate decision
# and the subagent that surveyed it could establish "no importer in this tree",
# which is not "no caller" (rule 2). Not shipping it and not deleting it are
# different claims, and this file only makes the first.

# NON-ROOT. The `node` user ships with the image at uid 1000. The server opens a
# socket above 1024 and writes only to the volume, so it needs nothing root has.
# The volume's host directory must be writable by uid 1000 -- see the compose file.
USER node

# The container's own port. The HOST side is chosen in compose and is deliberately
# NOT 5000: server.js:80 defaults PORT to 5000 and so does Flask (app.py:78), and
# the design doc counted 23 occurrences of that literal across 10 non-test files.
# Two servers defaulting to one port is a collision waiting for whoever starts
# second, and inside the container there is nothing to collide with.
EXPOSE 5000

# Its own /health route (server.js:396), so `docker compose ps` reports on the
# APPLICATION rather than on whether a process exists. A node process that is
# alive and not serving is precisely the state rule 13 calls a defect in the
# output.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||5000)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

ENTRYPOINT ["dumb-init", "--"]
CMD ["node", "server.js"]
