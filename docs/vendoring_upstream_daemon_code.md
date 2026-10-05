# Vendoring upstream daemon code

Role: documentation (reference)
Reads: nothing at runtime
Writes: nothing
Can send orders: no
Live-safe: yes

Operator instruction, 2026-10-05: *"if we must 'steal' or 'co-opt' the open
source code from any of the daemons or agents...just incorporate it as such"*.

That is standing authorization. Copying a function, a patch, or a whole file out
of Bitcoin Core, Litecoin Core or Gridcoin Research into this tree does not need
to be asked about. What follows is the part that still has to be done every
time, and it is not legal paperwork -- it is the thing that keeps a vendored
copy from becoming CLAUDE.md rule 8's duplicate-with-a-delay.

## The license question is settled, and it was measured

Fetched 2026-10-05 from each project's `COPYING` on `master`:

    bitcoin/bitcoin                        "The MIT License (MIT)"
    litecoin-project/litecoin              "The MIT License (MIT)"
    gridcoin-community/Gridcoin-Research   the MIT body, under no title

All three permit copying, modification and redistribution. The only obligation
is the one MIT states: *"The above copyright notice and this permission notice
shall be included in all copies or substantial portions of the Software."*

**Gridcoin's file is the one to be careful with, and the care is about
attribution rather than permission.** It never says "MIT" anywhere -- a
keyword grep for `mit license` returns nothing, and the only match is
`Permission is hereby granted` on line 7. It opens with FIVE copyright lines,
not one:

    Copyright (c) 2014 Black-Coin Developers
    Copyright (c) 2013-2014 NovaCoin Developers
    Copyright (c) 2011-2012 PPCoin Developers
    Copyright (c) 2009-2022 Bitcoin Developers
    Copyright (c) 2014-2022 Gridcoin Developers

So "it's MIT" is true of the TERMS and false of the HEADER. Copying the
Bitcoin Core notice onto a file taken from Gridcoin would drop four copyright
holders the text explicitly requires be carried. Copy the notice from the
project the code came from, not from memory of what MIT looks like.

This note exists because the claim was first made from recollection and then
checked. It is recorded here rather than left in a session log for the reason
rule 7 gives: a measurement that only exists in a log is not learning.

## What goes at every vendor site

A vendored copy looks correct in its own file forever. Upstream fixes a bug,
our copy does not move, and nothing fails -- which is the exact shape of the
`KXTEMP([A-Z]+?)[A-Z]*-` regex that had to be diagnosed three separate times in
Project Mammon because three copies existed and none pointed at the others.

So each vendored file or function carries, in a comment:

1. **The upstream project, path, and COMMIT SHA.** Not a version string and not
   a branch name. A SHA is what makes the drift checkable later; `master` is a
   moving target that will not answer "has this been fixed upstream since?"
2. **The upstream copyright notice and permission notice**, copied from that
   project's own `COPYING`. In the file, not in a separate NOTICE nobody opens.
3. **Every modification we made, and why.** The diff against upstream is the
   thing a future reader needs and the thing git history will not show, because
   the first commit introduces the already-modified copy.
4. **Why it was vendored rather than called.** If the answer is "the daemon has
   no RPC for this", say so -- that is the sentence that tells someone when the
   copy can be deleted.

## The candidates this unblocks

Named as candidates, not as done work. All three are small, self-contained, and
in Gridcoin Research:

  `ThreadCleanWalletPassphrase`   `src/wallet/rpcwallet.cpp:2209-2211` spawns
                                  two threads per `walletpassphrase`, and
                                  `NewThread` (`src/util.cpp:406-416`) detaches
                                  them into no thread_group. Measured live:
                                  36 -> 38 threads on one send. Bounded and
                                  cosmetic, but it is the leak the operator
                                  asked about.
  `RenameThread`                  a NO-OP on the 5.5.1.0 build, because
                                  `src/util.cpp` never includes
                                  `<sys/prctl.h>`. A one-line include.
  the quit veto                   upstream issue #2995: Qt6 `quit()` ->
                                  `closeAllWindows()` -> `BitcoinGUI::closeEvent`
                                  honors minimize-on-close by ignoring the
                                  event, vetoing the quit. Fixed upstream by
                                  `a951294df` (2026-06-13), five days after the
                                  5.5.1.0 tag.

Holding those as patches instead of waiting on `5.5.1.7-testnet` is what the
authorization above makes possible. Note what it does NOT make possible: none
of them has been tested here, and a change to a daemon that holds a wallet is
live posture. They are proposals until the operator builds and runs them
(rule 16).

**AND MEASURED 2026-10-05, PATCHING IS NOW THE WORSE OPTION FOR ALL THREE.**
`getpeerinfo` on the operator's testnet node, 9 peers, subver as reported:

    /Halford:5.5.1.8/   x1
    /Halford:5.5.1.7/   x3
    /Halford:5.5.1.4/   x1
    /Halford:5.5.1.3/   x1
    /Halford:5.5.1/     x3

The node itself runs 5.5.1.0 with `-disableupdatecheck`, so the network is four
patch levels ahead of it and `5.5.1.7` -- the build that carries `a951294df` --
is the single most common version among its own peers. Upgrading gets all three
fixes AND version parity with the chain; vendoring them into 5.5.1.0 gets the
fixes and keeps the divergence. So the authorization above stands and these
three stop being the reason to use it.

That is not an argument against vendoring in general. It is the measurement
that settles these three, and it only exists because somebody ran
`getpeerinfo` for an unrelated reason -- which is the case for recording a
number rather than reasoning about it.
