"""A QR code as inline SVG, rendered on the server so nothing can retarget it.

Role: function level (one decision -- the markup for a string -- callable with
      seeded inputs per CLAUDE.md rule 10)
Reads: nothing. The string is an argument. No environment, no file, no socket.
Writes: nothing
Can move funds: no. It draws a picture of a payment REQUEST; a wallet shows it
      to its owner, who approves or does not.
Mainnet-safe: yes. It knows nothing about any chain.

WHY THE SERVER DRAWS IT.

Asked for by the operator 2026-10-01 alongside the wallet menu. The easy way is a
JavaScript QR library on the page, and that is the same objection
chains/solana_pay.py already refuses for the URI itself: a QR encodes the deposit
ADDRESS, so a compromised or buggy encoder draws a different address and the
customer's phone obediently pays it. Every server-side check passes, because the
server was never asked. The encoding happens here, over the exact bytes
payment_uri() produced, and the browser receives finished markup it cannot alter
meaningfully.

WHY segno, AND WHAT IT COSTS.

swap_terminal/requirements.txt deliberately holds almost nothing -- its own
header says a deployment must not install tooling "onto a host that holds wallet
RPC credentials" -- so a new runtime dependency is a real decision and not a
convenience. segno was chosen and measured 2026-10-01:

    pip show segno  ->  Requires:        (empty: no transitive dependencies)
    pure Python, no C extension, no build step
    a Solana Pay URI encodes to a version 6 symbol, 49x49, 2805 bytes of SVG

The alternative was hand-rolling Reed-Solomon error correction, roughly three
hundred lines of finite-field arithmetic whose failure mode is a symbol that
scans to the wrong bytes. That is not code to write blind under a page that
tells customers where to send money.

IT DEGRADES INSTEAD OF FAILING. A serving host that has not installed segno gets
`None` from render(), the page says the QR is unavailable and why, and the copy
fields and wallet menu work exactly as before. A 500 on the deposit page because
a drawing library is missing would take out the one page a customer needs in
order to be paid -- rule 14's "never let an empty result print nothing", applied
to an absent optional dependency.
"""

from __future__ import annotations

import io

#: Error correction level. "m" recovers about 15% of the symbol and is the usual
#: choice for a screen; "l" would make the symbol smaller and a thumbprint or a
#: reflection likelier to defeat it, and a QR for a payment is read once, in a
#: hurry, by a phone camera at an angle.
ERROR_CORRECTION = "m"

#: Pixels per module. 4 puts a version 6 symbol (49 modules) at 196px plus the
#: border, which is legible on a phone held at arm's length without dominating
#: the panel.
SCALE = 4

#: Quiet zone in modules. The QR specification requires 4 and scanners are
#: measurably worse below it; 2 is a common web shortcut and is NOT taken here,
#: because a symbol that needs a second attempt is a customer who gives up.
BORDER = 4


def is_available() -> bool:
    """Whether this host can draw a QR at all. Cheap, and asked before claiming.

    A separate function so a page can say "unavailable, install segno" rather
    than rendering an empty box, and so a diagnostic can report the capability
    without rendering anything.
    """
    try:
        import segno  # noqa: F401, PLC0415 -- checked: imported for its presence, not its API, and deferred because the whole point is that it may be absent
    except ImportError:
        return False
    return True


def unavailable_reason() -> str:
    """Why there is no QR, in a sentence a page can print (rule 14)."""
    return (
        "no QR is drawn because the `segno` package is not installed in the process serving this "
        "page. It is a pure-Python dependency with no transitive requirements; `pip install -r "
        "swap_terminal/requirements.txt` adds it. The deposit account and memo above are complete "
        "without it -- a QR is a convenience for a phone camera, never the instruction itself."
    )


def render(payload: str) -> str | None:
    """Inline SVG for `payload`, or None when this host cannot draw one.

    INLINE, not a data: URI and not a separate endpoint, and each alternative was
    rejected for a reason:

      a data: URI in <img>   would need base64 in the markup and a
          Content-Security-Policy that permits data: images, which is a
          loosening of the one page where tightening matters.
      /swap/<id>/qr.svg      a second request for a value that is already in the
          response. It would also mean a route that renders a payment request,
          which is one more surface that has to get the swap id right.

    The XML declaration is stripped because the SVG is embedded in an HTML
    document rather than served as a standalone file, and a declaration inside
    HTML is markup the parser has to recover from.

    svgclass/lineclass are None so the markup carries no class attributes of
    segno's choosing: styling belongs to static/styles.css, and a library's
    class names appearing in our stylesheet would couple the two.
    """
    if not payload:
        # Not an exception: a caller with nothing to encode has nothing to show,
        # and the page's own "unavailable" path already says what to do. Raising
        # here would turn a missing value into a 500 on the deposit page.
        return None
    try:
        import segno  # noqa: PLC0415 -- deferred on purpose: this module is importable, and useful for is_available(), on a host that does not have it
    except ImportError:
        return None
    symbol = segno.make(payload, error=ERROR_CORRECTION)
    buffer = io.BytesIO()
    # BytesIO and not StringIO. segno's SVG writer writes bytes; handed a text
    # buffer it raises `TypeError: string argument expected, got 'bytes'`, which
    # is how the first version of this function failed. Measured, not recalled.
    symbol.save(buffer, kind="svg", scale=SCALE, border=BORDER,
                svgclass=None, lineclass=None, omitsize=True)
    svg = buffer.getvalue().decode("utf-8")
    declaration_end = svg.find("?>")
    return svg[declaration_end + 2:].strip() if declaration_end != -1 else svg.strip()
