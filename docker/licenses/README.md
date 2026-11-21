# Third-party notices that travel into the images

Role: documentation (reference), and the explanation for two files that deliberately
      carry no module header
Reads: nothing at runtime
Writes: nothing
Can move funds: no
Mainnet-safe: yes

## What is here

    bitcoin-core/COPYING              Bitcoin Core's own MIT notice, VERBATIM
    litecoin-core/COPYING             Litecoin Core's own MIT notice, VERBATIM
    berkeley-db/MEASURED_LINKAGE.md   the measured finding that both daemons
                                      contain Berkeley DB 4.8.30 under Sleepycat,
                                      which the two MIT notices do not cover

`docker/harness.Dockerfile` COPYs all three into the runtime stage under
`/usr/share/licenses/<project>/`, which is the convention
`docker/grc-desk.Dockerfile:90` already set for Gridcoin.

## Why the two COPYING files carry no `Role:` header

Every file this repository adds carries one (CLAUDE.md rule 1). These two do not,
and that is the point rather than an oversight: they are another project's license
text, and MIT's single obligation is that *"the above copyright notice and this
permission notice"* be included. A header prepended by us makes the file no longer
the notice it claims to be, and
`tests/test_upstream_daemons_are_pinned_and_verified.py` asserts each one still
contains that permission sentence and a `Copyright (c)` line. This file is their
header.

## Why they could not come from the tarballs the image downloads

Measured 2026-11-21 by extracting both release tarballs and listing every file:
`bitcoin-28.1/` contains `README.md`, `bitcoin.conf`, `bin/`, `share/man/` and
`share/rpcauth/` -- **no COPYING and no LICENSE**. `litecoin-0.21.4/` likewise. So
`COPY --from=build /src/COPYING` -- the move grc-desk.Dockerfile makes, because it
builds from a clone that has one -- is not available to an image that installs a
prebuilt binary. The notices have to be committed here or the image ships MIT
binaries with no notice at all, which is what it did until this change.

They also could not go under `docs/`: `.dockerignore:79-80` excludes `tests` and
`docs` from every build context rooted at the repository, so a notice filed there
could never reach an image.

## Where each text came from, and it was not from memory

`docs/vendoring_upstream_daemon_code.md`'s measured rule is *"Copy the notice from
the project the code came from, not from memory of what MIT looks like"* -- written
after Gridcoin's COPYING turned out to carry the MIT body under no title with FIVE
copyright holders. So each file here was copied out of that project's own source
release, and both releases were verified first:

    bitcoin-core/COPYING    from bitcoin-28.1.tar.gz, whose digest matched
                            upstream's bitcoincore.org SHA256SUMS
                              source tarball  c5ae2dd041c7f9d9b7c722490ba5a9d624f7e9a089c67090615e1ba4ad0883ba
                              THIS FILE       779d9beab4eef2340bb1e86e91f8f55dea9b0985a2d03fbcbb52bd713e091e1b

    litecoin-core/COPYING   from litecoin-0.21.4.tar.gz, whose digest matched the
                            CLEARSIGNED download.litecoin.org SHA256SUMS.asc,
                            "Good signature from David Burkett"
                              source tarball  ba0922213f69c1b1a1d6c3441c5ea3696b538437533555ab74268f075f0de5d3
                              THIS FILE       c71857544b2ea7371b4b70035a0a23ffe9691d0a07e7c9206b94be22983f5382

BOTH DIGESTS ARE GIVEN BECAUSE ONE OF THEM USED TO BE UNLABELED, and the unlabeled
one was the notice's. Until 2026-11-21 each entry read "<tarball>, digest matched
upstream's SHA256SUMS" with a single bare `sha256` line beneath it, which any reader
takes for the TARBALL's digest -- it is the only file named on the line. It is not;
it is this COPYING file's. Someone comparing it against bitcoincore.org's published
SHA256SUMS would have found no such line there and reasonably concluded the
provenance claim was fabricated. Rule 16: a wrong sentence an operator acts on is a
bug, and this one impugned a chain of custody that is actually sound.

WHAT THE SECOND LINE ACTUALLY PROVES, and it is the stronger claim of the two:
the committed file is byte-identical to the COPYING extracted from the
digest-verified source release. Re-measured 2026-11-21 against the extracted trees,
`sha256sum docker/licenses/bitcoin-core/COPYING` and
`sha256sum <extracted>/bitcoin-28.1/COPYING` are the same 779d9bea..., and the
litecoin pair are the same c7185754.... So the notice was not retyped, reflowed or
recalled -- which is the failure the Gridcoin COPYING above is the record of.

The bodies are byte-for-byte the same MIT grant. **Only the copyright blocks
differ**, which is the whole reason there are two files:

    Bitcoin     Copyright (c) 2009-2024 The Bitcoin Core developers
                Copyright (c) 2009-2024 Bitcoin Developers

    Litecoin    Copyright (c) 2009-2021 The Bitcoin Core developers
                Copyright (c) 2009-2021 Bitcoin Developers
                Copyright (c) 2011-2021 The Litecoin Core developers

Pointing both COPY lines at one shared file -- the obvious tidy-up, and it would
pass every other check -- drops "The Litecoin Core developers" and changes two date
ranges. `test_each_upstream_project_ships_its_OWN_license_notice_in_the_image`
asserts the two files are not byte-identical for exactly that mutation.
