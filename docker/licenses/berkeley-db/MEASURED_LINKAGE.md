# Berkeley DB 4.8.30 is INSIDE the daemons this image installs

Role: measured finding (data, shipped into the image beside the MIT notices)
Reads: nothing at runtime
Writes: nothing
Can move funds: no
Mainnet-safe: yes

## Why this file exists rather than only the two MIT notices

`docker/licenses/bitcoin-core/COPYING` and `docker/licenses/litecoin-core/COPYING`
are the upstream MIT texts, verbatim, and MIT's one obligation is to carry them.
Shipping those two files alone would be accurate about Bitcoin Core and Litecoin
Core and **wrong about what is in the binaries**, because both daemons contain a
third party's code under a different license, and that license is the only one in
this image with a source-disclosure condition.

Measured 2026-11-21 against the upstream release tarballs this image installs,
`bitcoin-28.1-x86_64-linux-gnu.tar.gz` (49,598,812 bytes, digest confirmed OK
against upstream's own `SHA256SUMS`) and
`litecoin-0.21.4-x86_64-linux-gnu.tar.gz` (36,737,533 bytes, digest confirmed OK
against upstream's CLEARSIGNED `SHA256SUMS.asc`):

    strings -a <binary> | grep -c '^Berkeley DB 4\.8\.30: (April  9, 2010)$'

      bitcoind        1
      bitcoin-cli     0
      litecoind       1
      litecoin-cli    0

That string is Berkeley DB's own `DB_VERSION_STRING`. It is the library reporting
its own build date, not a message *about* Berkeley DB, and it is compiled into the
daemon. Supporting counts from the same scan: `bitcoind` carries 161 and
`litecoind` 162 distinct `DbEnv`/`db_cxx` symbol strings, plus BDB's internal error
messages ("Thread died in Berkeley DB library", "Berkeley DB library does not
support DB_REGISTER on this system"). The two `-cli` binaries carry zero of any of
it.

## The trap: `ldd` reports these binaries as clean, and they are not

    ldd bitcoin-28.1/bin/bitcoind
      linux-vdso.so.1  libpthread.so.0  libm.so.6  libgcc_s.so.1  libc.so.6  ld-linux

    ldd litecoin-0.21.4/bin/litecoind
      linux-vdso.so.1  libpthread.so.0  librt.so.1  libm.so.6  libgcc_s.so.1
      libc.so.6  ld-linux

No `libdb_cxx` in either. The upstream release daemons are statically linked
against everything except the C runtime, so **a linkage audit done with `ldd`
concludes there is no Berkeley DB here.** That is how this finding gets missed: a
from-source build links BDB dynamically and `ldd` names it, while the artifact
actually shipped hides the same code inside the executable. The scan above is the
one that answers the question for a prebuilt binary.

## What this corrects

A license audit of these two projects' **source trees** on 2026-11-21 concluded
"BITCOIN CORE 28.1 -- CLEAN" and found `--without-bdb --with-sqlite` produces a
wallet-capable `bitcoind` with no Berkeley DB linkage. That conclusion is correct
and was proven by a build: `src/config/bitcoin-config.h` showed
`/* #undef USE_BDB */`, zero `bdb.o` in the build tree, zero `libdb` in `ldd`.

**It is a finding about a build this image does not perform.** `harness.Dockerfile`
downloads upstream's prebuilt release tarball; it never runs `./configure`. So
`--without-bdb` is not available to it, and the artifact it installs is the one
measured above. The audit's Bitcoin-versus-Litecoin asymmetry -- Litecoin 0.21.4
genuinely has no `--without-bdb` option, Bitcoin 28.1 does -- is real, and it does
not reach this image, because neither daemon here is built from source. Both carry
Berkeley DB 4.8.30.

## The license, and the honest limit on what was read

Berkeley DB 4.8.30 predates Oracle's 6.0 relicensing to AGPL-3.0, so the applicable
terms are **Sleepycat**, which has no network-use trigger. Clause 3, quoted
verbatim from this container's `/usr/share/doc/libdb5.3-dev/copyright`:

> 3. Redistributions in any form must be accompanied by information on how to
> obtain complete source code for the DB software and any accompanying software
> that uses the DB software. The source code must either be included in the
> distribution or be available for no more than the cost of distribution plus a
> nominal fee, and must be freely redistributable under reasonable conditions. For
> an executable file, complete source code means the source code for all modules
> it contains. It does not include source code for modules or files that typically
> accompany the major components of the operating system on which the executable
> file runs.

**NOT READ: Berkeley DB 4.8.30's own LICENSE file.** The text above is BDB *5.3*'s,
from Debian's packaging on this container. `download.oracle.com` is not on this
session's allow-list and the version is asserted from the binaries' own
`DB_VERSION_STRING`, not from a license file shipped with 4.8.30. The 4.8.30
licensing claim is **version-range reasoning, not a file read**, and the
distinction matters: Sleepycat has no network-use trigger, AGPL-3.0 does.

## What this repository does about it, and what it does not

Every obligation in clause 3 triggers on **distribution**. This repository
distributes nothing:

  - No upstream daemon binary or source tree is committed here. Measured
    2026-11-21: 567 tracked files, zero whose first bytes are ELF or gzip magic
    (561 before the six files this change itself adds -- the count is stated with
    its denominator per rule 3, and the gate asserts the zero, not the 567).
    `tests/test_upstream_daemons_are_pinned_and_verified.py` holds that as a clean
    gate (rule 19: zero asserted, no baseline, nothing tolerated), and
    `architecture.VENDORED_UPSTREAM_TREES` is the empty tuple it checks.
  - The image is built on the operator's own host from the projects' own tarballs.
    The bytes come from `bitcoincore.org` and `download.litecoin.org`, not from us.

**The exclusion that IS available was taken, and it is not BDB.** Berkeley DB
cannot be removed from a prebuilt daemon. What can be excluded is every binary in
those tarballs that this image does not need, and
`harness.Dockerfile` installs exactly four of the thirteen:

    bitcoin-28.1/bin/     bitcoind bitcoin-cli          INSTALLED
                          bitcoin-wallet bitcoin-tx     EXCLUDED -- bitcoin-wallet
                          bitcoin-util bitcoin-qt         carries the same BDB
                          test_bitcoin                     (DB_VERSION_STRING=1,
                                                            161 DbEnv strings)
    litecoin-0.21.4/bin/  litecoind litecoin-cli        INSTALLED
                          litecoin-wallet litecoin-tx   EXCLUDED -- litecoin-wallet
                          litecoin-qt test_litecoin       carries it too

`test_the_image_installs_only_the_binaries_it_declares` derives that install list
from the Dockerfile and fails if a fifth binary is added, which is the only part of
this a test can hold: a wallet tool nobody needs is a second copy of the same
obligation, and `bitcoin-qt` would additionally drag Qt's GPL/LGPL into an image
that has no use for a GUI.

## Before any redistribution, two things are the operator's

1. **Satisfy clause 3 or do not ship the binary.** Shipping it means publishing
   complete source for the daemon *and* for Berkeley DB 4.8.30, at no more than the
   cost of distribution. Both are already public and neither reveals anything
   proprietary -- the daemons are MIT. The cheaper option is not to ship them at
   all and have the operator install the upstream release themselves, which is
   exactly what this repository does today.
2. **The "accompanying software" question is legal and is not answered here.**
   Whether swap_terminal's closed-source Flask application -- a separate process
   that reaches the daemon over HTTP JSON-RPC (`chains/base.py` builds a
   `{"jsonrpc": "2.0", ...}` payload and `requests.post()`s it) and links no BDB
   symbol -- is "accompanying software that uses the DB software" under clause 3 is
   counsel's call. The separate-process boundary is a strong argument and it is
   also precisely the argument license lawyers disagree about. The mechanics are
   measured above; the conclusion is not drawn.
