#!/usr/bin/env python3
"""Both directions move the same money between the same two parties.

Role: tests (read-only)
Reads: atomic_swap_xrp.py's source as an AST. No chain, no network, no daemon.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

WHAT SHIPPED, AND WHAT IT WOULD HAVE COST

run_chain_first() built its XRP escrow as

    escrow_create_tx(ctx.b_xrp, ctx.a_xrp, ...)   signed with ctx.b_xrp_secret

-- B sending XRP to A -- and finished it as A. Its own docstring says the
opposite, in these words:

    "B funds GRC first, A funds XRP, B claims XRP, A reads the Fulfillment, A
     claims GRC ... The escrow is CREATED by the XRP holder and FINISHED by the
     GRC holder."

So as written, B funded the script leg AND sent the XRP, A received both legs,
and B claimed back its own coins. One side paid twice. Not a swap.

IT WAS FOUND BY READING, NOT BY RUNNING, and nothing would have found it
otherwise: chain-first has never been run on this code path, the dry run returns
before any runner is called, and no test drove a runner at all. "It shares every
function with the direction that works" was the reasoning available, and it was
worthless -- the functions are shared and the ARGUMENTS are not.

THE INVARIANT, which is simpler than either runner:

Both directions move the same money between the same two parties. A gives XRP
and receives script-chain coins; B gives script-chain coins and receives XRP.
What differs is the ORDER the legs are funded in and WHERE the secret surfaces --
a scriptSig on the script chain in one, an EscrowFinish Fulfillment on XRPL in
the other. Neither difference touches who pays whom.

So: in EVERY runner, the escrow is created by a_xrp paying b_xrp, and the script
leg's claim pays a_grc. Three argument positions, checked by reading them out of
the AST rather than by running a swap.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SOURCE = pathlib.Path(__file__).resolve().parent.parent / "atomic_swap_xrp.py"
TREE = ast.parse(SOURCE.read_text())

RUNNERS = ("run_xrp_first", "run_chain_first")


def _runner(name: str) -> ast.FunctionDef:
    for node in ast.walk(TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() is gone from atomic_swap_xrp.py; this test no longer measures it")


def _calls(function: ast.FunctionDef, callee: str) -> list[ast.Call]:
    return [node for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == callee]


def _attribute_name(node) -> str:
    """"ctx.a_xrp" for an Attribute on a Name, else a description of what it is."""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return f"<{type(node).__name__}>"


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_XRP_escrow_is_always_created_by_A_paying_B(runner):
    """escrow_create_tx(sender, receiver, ...). A pays; B receives. Both directions.

    MUTATION: swap the two in run_chain_first -- which is what shipped -- and this
    fails naming the runner. Verified 2026-09-29; it is the mutation that was the
    bug.
    """
    creates = _calls(_runner(runner), "escrow_create_tx")
    assert len(creates) == 1, f"{runner}() creates {len(creates)} escrows; it should create exactly one"
    sender, receiver = (_attribute_name(argument) for argument in creates[0].args[:2])
    assert (sender, receiver) == ("ctx.a_xrp", "ctx.b_xrp"), (
        f"{runner}() escrows XRP from {sender} to {receiver}. A gives XRP and B receives it in BOTH "
        f"directions -- what changes between them is the ORDER the legs are funded and where the "
        f"secret surfaces, never who pays whom. Reversed, one party funds both legs and the other "
        f"receives both"
    )


@pytest.mark.parametrize("runner", RUNNERS)
def test_the_script_leg_is_always_claimed_to_As_address(runner):
    """claim_the_script_leg(..., destination). A receives the script-chain coins.

    The other half of the same conservation. B funds that leg in both directions,
    so a claim paying B would hand B back its own coins -- which is what the
    shipped chain-first run would have printed as a success.
    """
    claims = _calls(_runner(runner), "claim_the_script_leg")
    assert len(claims) == 1, f"{runner}() claims the script leg {len(claims)} times; it should be once"
    destination = _attribute_name(claims[0].args[-1])
    assert destination == "ctx.a_grc", (
        f"{runner}() pays the script leg's claim to {destination}. B funds that leg in both "
        f"directions, so anything but A's address returns B its own coins and A pays for nothing"
    )


def test_the_two_directions_differ_in_ORDER_and_REVEAL_and_nothing_else_about_the_parties():
    """The claim this file rests on, asserted rather than assumed.

    If a future direction genuinely needed a different party assignment, the two
    tests above would be wrong rather than the code -- so the reason they hold is
    written down here: the only asymmetry between the runners is which leg is
    funded first and which chain the secret surfaces on.
    """
    xrp_first, chain_first = (_runner(name) for name in RUNNERS)
    # One escrow and one script-leg claim each: the shapes are the same.
    for runner in (xrp_first, chain_first):
        assert len(_calls(runner, "escrow_create_tx")) == 1
        assert len(_calls(runner, "claim_the_script_leg")) == 1
    # And the reveal differs: one reads a scriptSig, the other a Fulfillment.
    def _names_called(function):
        return {node.func.id for node in ast.walk(function)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}

    assert "preimage_from_escrow_finish" in _names_called(chain_first), (
        "chain-first must read the secret out of an EscrowFinish Fulfillment; that reader is the "
        "reason the direction can exist at all"
    )
    assert "preimage_from_escrow_finish" not in _names_called(xrp_first), (
        "xrp-first reads the secret off the script chain's scriptSig, not off XRPL"
    )
