"""An adapter that CAN send must derive can_spend, never hardcode it.

Role: tests (the structural gate behind chains/icp_payout_identity.py)
Reads: the chains/*.py sources, parsed. No adapter is constructed, no socket is
       opened, no environment variable is read.
Writes: nothing
Can move funds: no
Mainnet-safe: yes

=============================================================================
THREE CHAINS, THREE TIMES AN OPERATOR HAD TO NOTICE
=============================================================================

Every payout-capable chain in this tree arrived with `can_spend = False`
hardcoded, and each one was only corrected after a person said so:

    XRP   derived from the seed since 2026-10-02, and from the seed actually
          DECODING since 2026-10-03 -- after a nine-character placeholder on the
          operator's host made can_spend True and offered an XRP payout that
          could never have been signed.
    SOL   hardcoded until 2026-10-03. chains/solana_payout_keypair.py's header
          records the instruction that moved it: "whoa we have to be able to swap
          TO SOL too".
    ICP   hardcoded from 2026-10-06 to 2026-10-11. "what the fuck why is this
          hardcoded to not pay out. it shouldn't."

EACH TIME THE CAPABILITY WAS ALREADY BUILT. ICP is the clearest: send_to_address()
was complete from the day the chain landed, dfx_transport() grew an `identity`
parameter the NEXT day for a different caller, and chains/registry.py never passed
it. The transport could sign, the adapter could pay, and the declaration said no.

A HARDCODED FALSE CANNOT GO STALE, which is the whole reason this gate exists
rather than a fourth fix. A derived value tracks the thing it describes; a literal
describes the day it was written. So the rule is not "ICP should pay out", it is
"a chain that can send must say so from the fact, not from a literal" -- rule 19's
test for a fix: does it stop the symptom being reported, or stop the cause
existing.

=============================================================================
WHAT COUNTS AS DERIVED, AND WHY THE CLASS-LEVEL FALSE IS STILL CORRECT
=============================================================================

chains/solana.py and chains/xrp.py both carry `can_spend = False` at CLASS level
and override it per instance in __init__. That is not the defect -- it is the
fail-closed fallback those files document at length, and it matters because
registry.why_cannot_pay_out() reads the attribute with
`getattr(adapter, "can_spend", False)`: anything that reads the class without
constructing an adapter must get "cannot pay out".

So the gate accepts either shape:

  can_spend = True at class level      the Bitcoin-derived chains. The daemon holds
                                       the keys and signs its own sends, so the
                                       capability comes with being reachable at all
                                       and there is no separate arming token.
  self.can_spend assigned in __init__  SOL, XRP and now ICP. The capability is a
                                       credential this process either has or does
                                       not, read at construction.

And it refuses exactly one shape: a class that defines send_to_address, sets
can_spend to a FALSE literal at class level, and never assigns self.can_spend.
That is the state ICP was in for five days.
"""

from __future__ import annotations

import ast
import pathlib

CHAINS = pathlib.Path(__file__).resolve().parent.parent / "swap_terminal" / "chains"

#: The method whose presence means "this adapter can move money". Named rather
#: than inferred because chains/registry.py's payout path calls exactly this --
#: `adapter.send_to_address(...)` at services/payout_service.py:529 and three
#: siblings -- so a class defining it is a class the payout worker can reach.
PAYOUT_METHOD = "send_to_address"


def adapter_classes():
    """Every class in chains/*.py that defines the payout method, with its tree.

    PARSED RATHER THAN IMPORTED, which is deliberate: importing chains/solana.py
    and chains/xrp.py pulls in base58 and bech32, and importing an adapter module
    to ask a structural question about its source would make this gate depend on
    the dependency set rather than on the code. It also means a module that fails
    to import cannot silently drop out of the sweep.
    """
    found = []
    for path in sorted(CHAINS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            methods = {child.name for child in node.body if isinstance(child, ast.FunctionDef)}
            if PAYOUT_METHOD in methods:
                found.append((path.name, node))
    return found


def class_level_can_spend(node: ast.ClassDef):
    """The literal assigned to can_spend in the class body, or None if absent."""
    for statement in node.body:
        if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "can_spend"
                for target in statement.targets):
            value = statement.value
            return value.value if isinstance(value, ast.Constant) else "not-a-literal"
    return None


def assigns_self_can_spend(node: ast.ClassDef) -> bool:
    """Whether __init__ assigns self.can_spend, i.e. derives it per instance.

    ANY ATTRIBUTE NAMED can_spend IN A *STORE* CONTEXT, ANYWHERE IN __init__, plus
    setattr() with that literal name. Written that way after the first version read
    only `Assign.targets` directly and reported chains/xrp.py:XRPAdapter as
    hardcoded -- which is false. XRPAdapter derives it at line 810:

        self.can_spend, self.payout_refusal = payout_capability()

    TUPLE UNPACKING, so the single target is an ast.Tuple and the Attribute nodes are
    its elements. `isinstance(target, ast.Attribute)` is False for a Tuple, so the
    assignment was invisible and the gate accused the one adapter with the longest
    paper trail of deriving it. That is the same defect the gate is named for, in the
    gate: a check that enumerates the shapes it knows measures the list, and this one
    knew one of two.

    ast.Store IS WHAT MAKES IT AN ASSIGNMENT rather than a read. `if self.can_spend:`
    at line 913 is a Load and must not count -- a class that only ever READS the
    attribute has not derived anything.
    """
    for statement in node.body:
        if not (isinstance(statement, ast.FunctionDef) and statement.name == "__init__"):
            continue
        for inner in ast.walk(statement):
            if (isinstance(inner, ast.Attribute) and inner.attr == "can_spend"
                    and isinstance(inner.ctx, ast.Store)):
                return True
            # setattr(self, "can_spend", ...) -- a third shape nothing uses today, and
            # accepted so that a future adapter using it is not accused either.
            if (isinstance(inner, ast.Call) and getattr(inner.func, "id", "") == "setattr"
                    and len(inner.args) >= 2 and isinstance(inner.args[1], ast.Constant)
                    and inner.args[1].value == "can_spend"):
                return True
    return False


def test_the_sweep_finds_the_adapters_it_is_meant_to_measure():
    """A gate that measures nothing passes forever. MUTATION: rename PAYOUT_METHOD.

    THE DENOMINATOR IS STATED (rule 3) because "every adapter derives it" means
    nothing without how many were looked at. Four classes define send_to_address
    today -- base.RPCAdapter and the SOL, XRP and ICP adapters -- and the three
    Bitcoin-derived chains inherit from the first rather than defining their own.
    """
    found = adapter_classes()

    assert len(found) >= 4, (
        f"only {len(found)} class(es) define {PAYOUT_METHOD}; this gate has stopped measuring. "
        f"Found: {[f'{file}:{node.name}' for file, node in found]}"
    )
    files = {file for file, _node in found}
    for expected in ("base.py", "solana.py", "xrp.py", "icp.py"):
        assert expected in files, f"chains/{expected} defines no {PAYOUT_METHOD}; has it moved?"


def test_no_adapter_that_can_send_hardcodes_itself_unable_to():
    """THE GATE. MUTATION: delete the two self.can_spend lines from ICPAdapter.__init__.

    This is the state ICP was in from 2026-10-06 to 2026-10-11: a class with a
    complete send_to_address(), can_spend set to a False literal in the class body,
    and nothing per instance to ever change it. Five days, three surfaces rendering
    "cannot pay out", and a transport one argument away from signing.
    """
    offenders = []
    for file, node in adapter_classes():
        literal = class_level_can_spend(node)
        if literal is True:
            continue              # the daemon signs; reachable IS payable
        if assigns_self_can_spend(node):
            continue              # derived per instance from a credential
        offenders.append(f"chains/{file}:{node.name} (class-level can_spend={literal!r})")

    assert not offenders, (
        "these adapters define " + PAYOUT_METHOD + " and declare themselves unable to pay with a "
        "literal that nothing can ever update: " + ", ".join(offenders) + ". Either the chain's "
        "daemon signs its own sends (can_spend = True at class level, as chains/base.py) or the "
        "capability is a credential this process has or lacks (assign self.can_spend in __init__ "
        "from a module like chains/icp_payout_identity.py). A hardcoded False cannot go stale, "
        "which is how ICP shipped for five days with a working payout method it refused to use"
    )


def test_the_fail_closed_class_level_default_is_still_there():
    """The other half, and dropping it is the EXPENSIVE direction.

    registry.why_cannot_pay_out() reads `getattr(adapter, "can_spend", False)`, so a
    class that omits the attribute entirely already fails closed. What this asserts is
    that the three credential-armed adapters keep the refusing literal in the class
    body as well -- a stub, a reflective check, or a reader who never constructed an
    adapter must get "cannot pay out" rather than an AttributeError or a True.

    MUTATION: change ICPAdapter's class-level can_spend to True and this fails while
    the derivation gate above still passes, because the instance override would mask
    it everywhere except on the class itself.
    """
    for file, node in adapter_classes():
        if not assigns_self_can_spend(node):
            continue
        assert class_level_can_spend(node) is False, (
            f"chains/{file}:{node.name} derives can_spend per instance but its CLASS-level value "
            f"is {class_level_can_spend(node)!r}. Anything reading the class without constructing "
            f"an adapter would be told this chain can pay out"
        )
