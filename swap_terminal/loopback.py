"""What this process is bound to, and what counts as "this machine".

Role: shared leaf functions (address vocabulary plus one read of /proc)
Reads: /proc/self/fd and /proc/net/tcp*, to answer "what are MY listening
       sockets bound to". Nothing else; no environment, no database, no socket.
Writes: nothing
Can move funds: no. Nothing here signs, sends, spawns or signals. It answers a
       question that a caller which CAN move funds asks before deciding whether
       to let a request through -- see services/kill_switch.py, whose header
       says yes.
Mainnet-safe: yes. Every function is read-only and opens no socket.

=============================================================================
WHY THIS MODULE EXISTS: THERE WERE ALREADY THREE COPIES OF "IS IT LOOPBACK"
=============================================================================

Counted 2026-10-02, before this file:

    app.py:81                 LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
    gunicorn.conf.py:on_starting   {"127.0.0.1", "localhost", "::1", ""}  (inline)
    operator_panel.py:1204    LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

Three spellings of one vocabulary, in three files, and the swap_terminal kill
switch needed a fourth. CLAUDE.md rule 8: "two copies of one rule is not
redundancy, it is a bug with a delay on it", and rule 19 adds that a fourth copy
written today is the defect, not a backlog item. So the vocabulary is here, this
file and services/kill_switch.py are the only readers of it so far, and the
fourth copy was never written.

THE OTHER THREE STILL EXIST AND STILL DO NOT IMPORT IT, said plainly because
this paragraph claimed the opposite until 2026-10-02 and the claim was checked
rather than believed:

    $ grep -n 'LOOPBACK_HOSTS =' swap_terminal/app.py operator_panel.py
    swap_terminal/app.py:81    LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
    operator_panel.py:1204     LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

Both are untouched, and gunicorn.conf.py's inline set is untouched too. Pointing
them here is a THREE-LINE PROPOSAL and not a fix, for two separate reasons, and
rule 17 asks that the difference be stated rather than blurred: app.py and
operator_panel.py were off-limits to the change that wrote this file (concurrent
work), and gunicorn.conf.py's case is not a comment at all -- see the next
paragraph, where importing this name would change what a startup banner SAYS
about a live deployment. That is reported, measured, to the operator rather than
done here (rule 16).

AND THE THREE HAD ALREADY DRIFTED, which is the rule arriving on schedule.
gunicorn.conf.py's copy counts the EMPTY STRING as loopback; the other two do
not. That is not a harmless extra member:

    bind = f"{os.getenv('SWAP_TERMINAL_HOST', '127.0.0.1')}:{...}"

`os.getenv` returns "" for `SWAP_TERMINAL_HOST=` -- the default does not apply to
a variable that is set and empty -- so bind becomes ":5000", and gunicorn binds
an empty host on ALL INTERFACES. The banner whose entire job is to say whether
the deployment is exposed then printed `warnings        (none)`. app.py's
development server does not have the defect (`bind_host()` returns
`host or DEFAULT_HOST`, so "" becomes 127.0.0.1), so the two servers disagreed
about what an empty value means and only one of them said so.

Importing LOOPBACK_HOSTS into gunicorn.conf.py WOULD fix the REPORT: an empty
host would no longer be called loopback, so the exposure warning would print. It
would NOT change what gunicorn binds -- changing `bind` changes what the socket
is reachable from, which is the operator's call (rule 16). Neither half has been
done; both are in this change's report.

WHAT PROTECTS THE KILL SWITCH FROM THAT DEFECT IN THE MEANTIME is not the
banner. refuse_off_box() in services/kill_switch.py asks the KERNEL what this
process is bound to and refuses an empty SWAP_TERMINAL_HOST explicitly, so the
`SWAP_TERMINAL_HOST=` deployment that binds all interfaces is refused by the
controls whether or not any banner ever mentions it. The banner is a reporting
defect; the control surface does not depend on it.

=============================================================================
WHAT A REQUEST HANDLER MAY AND MAY NOT BELIEVE ABOUT THE BIND
=============================================================================

listening_addresses() is here because the alternatives are all weaker, and the
difference matters for anything that refuses to act when it is reachable
off-box:

  the Host header       CLIENT-SUPPLIED. `Host: localhost` is one line of curl,
                        and a DNS-rebinding attack sends a Host that resolves
                        to 127.0.0.1 on purpose. A WRONG Host is grounds to
                        refuse; a RIGHT one proves nothing at all.
  REMOTE_ADDR           the peer of THIS connection, which is a true fact about
                        this request and not about the listener. A server bound
                        to 0.0.0.0 reached from the same host reports
                        127.0.0.1, and a reverse proxy on the same host reports
                        127.0.0.1 for every off-box client it forwards.
  SWAP_TERMINAL_HOST    what both servers read to BUILD the bind, so it is the
                        best available statement of intent -- but `gunicorn -b`
                        on the command line overrides the config file's value
                        and leaves the variable saying something else, and a
                        unit file or a shell wrapper can do the same.
  /proc/net/tcp, filtered to THIS PROCESS'S OWN SOCKET INODES
                        what the kernel will actually accept a connection on,
                        asked of the kernel. No header, no variable and no
                        argument can lie about it. This is the one that is
                        evidence rather than a reason to believe (rule 17).

The inode filter is the load-bearing half and not a detail. Measured on this
machine 2026-10-02: /proc/net/tcp listed six LISTEN rows, two of which were
bound to 0.0.0.0 and belonged to processes that are nothing to do with this
application. Reading the file without matching inodes against /proc/self/fd
would have reported this process as bound to all interfaces because something
else on the host is.

WHAT IT CANNOT ESTABLISH, said plainly because a caller must fail closed rather
than guess: a platform with no /proc, and an unreadable /proc/self/fd, both
return None -- "could not look", which is a different answer from "nothing
found" and the caller is required to tell them apart.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

#: The host spellings a browser or an operator on this machine writes for "here".
#: NAMES as well as addresses, because a Host header and an Origin both carry
#: whatever the user typed, and `localhost` is what they type.
#:
#: Membership is the test the two startup banners want (app.exposure_warnings()
#: and gunicorn.conf.on_starting()) and the test operator_panel.py's
#: cross-origin refusal wants. It is deliberately NOT the test
#: is_loopback_host() performs -- see that function for why the two differ and
#: why neither one is the other's bug.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

#: Where the kernel publishes the socket table. A constant rather than the path
#: inline so a test can point the reader at seeded text, and so the
#: "this platform has no /proc" branch is reachable from a test rather than
#: being a branch nobody has ever executed (which is what CLAUDE.md rule 17
#: calls an unchecked claim).
PROC_SELF_FD = Path("/proc/self/fd")
PROC_NET_TCP_FILES = (Path("/proc/net/tcp"), Path("/proc/net/tcp6"))

#: /proc/net/tcp's socket-state column for LISTEN. The file writes the state as
#: two hex digits; 0A is TCP_LISTEN. Named because `== "0A"` in the middle of a
#: parser is the magic value ruff's PLR2004 is about, and the answer rule 19
#: asks for is to name it rather than to suppress the finding.
TCP_STATE_LISTEN = "0A"

#: The column index of the socket inode in /proc/net/tcp, and the smallest row
#: that can have one. Columns are: sl, local_address, rem_address, st,
#: tx_queue:rx_queue, tr:tm->when, retrnsmt, uid, timeout, inode, ...
_INODE_COLUMN = 9
_LOCAL_ADDRESS_COLUMN = 1
_STATE_COLUMN = 3
_MINIMUM_COLUMNS = _INODE_COLUMN + 1

#: An IPv4 local_address is 8 hex digits; IPv6 is 32. Named for the same reason
#: as TCP_STATE_LISTEN.
_IPV4_HEX_LENGTH = 8
_IPV6_HEX_LENGTH = 32

#: The prefix /proc/self/fd uses for a socket. `socket:[12345]`.
_SOCKET_LINK_PREFIX = "socket:["


def host_of(url_or_authority: str) -> str:
    """The bare host from an Origin or a Host header, with any scheme, port and brackets gone.

    COPIED HERE 2026-10-02 FROM operator_panel.py, which is where it was written
    and where its measurement comes from. The swap terminal's kill switch needs
    exactly this parse for exactly the same reason, and writing it a second time
    from scratch is rule 8's documented failure -- the weather-city regex whose
    identical defect was diagnosed and fixed three separate times because nothing
    pointed from one copy to the others.

    COPIED, NOT MOVED, AND THAT IS RULE 8's SHAPE ARRIVING RATHER THAN BEING
    AVOIDED. operator_panel.py:1207 still defines its own host_of() and still
    calls it from refuse_a_cross_origin_post(); that file was off-limits to this
    change. So there are TWO copies of this parse today, the other one is named
    here so a reader who finds one is told the other exists, and deleting
    operator_panel.py's in favor of this import is named work in this change's
    report -- not a baseline, and not something to discover later (rule 19).

    THE FOUR-LINE BODY IS CHARACTER-IDENTICAL IN BOTH AS OF 2026-10-02 -- diffed
    against operator_panel.py:1224-1228, not assumed --
    which is the dangerous state and not a reassuring one: identical copies are
    what drift looks like on the day they are written. Both carry the fix for the
    prefix defect described below, so there is nothing wrong in either one today.
    That is exactly the condition under which the next fix lands in one of them.

    WRITTEN BECAUSE MATCHING A PREFIX IS A VULNERABILITY, and this one was live for about four
    minutes before its own test caught it. The first version of the guard asked whether the
    Origin STARTED WITH "http://127.0.0.1" -- and

        http://127.0.0.1.attacker.com

    starts with exactly that. An attacker who controls any domain can register that subdomain,
    serve a page from it, and pass an Origin check that looks obviously correct. The same shape
    defeats endswith against a suffix (`evil-localhost`), which is why this parses instead of
    pattern-matching at either end.

    IPv6 brackets are stripped because a Host header writes the loopback address as `[::1]:8765`
    while an Origin writes `http://[::1]`, and a comparison that handled one and not the other
    would refuse the operator's own browser on a v6-preferring machine.
    """
    authority = url_or_authority.split("://", 1)[-1]
    authority = authority.split("/", 1)[0]
    if authority.startswith("["):
        return authority[1:].split("]", 1)[0]
    return authority.rsplit(":", 1)[0] if ":" in authority else authority


def is_loopback_host(host: str) -> bool:
    """True if this host STRING is an address that keeps traffic on this machine.

    TWO TESTS IN THIS FILE AND THEY ARE NOT THE SAME TEST, which is rule 8's
    "if they genuinely differ, the difference is the point":

      LOOPBACK_HOSTS membership    for a value a HUMAN wrote -- a Host header, an
          Origin, SWAP_TERMINAL_HOST. It accepts the three spellings anyone
          actually types and refuses everything else, including 127.0.0.2. That
          is the conservative side for a guard whose input is attacker-reachable.
      this function                for an address the KERNEL reported, where the
          whole 127.0.0.0/8 block and `::1` are all genuinely loopback and a
          name is impossible. A listener on 127.0.0.2 is not reachable off-box,
          and calling it exposed would refuse a control surface that is in fact
          private.

    Rejects anything that is not an address at all, including "localhost": a
    NAME has no answer here without resolving it, and resolution is a network
    question with a network's failure modes. Callers that must accept a name use
    LOOPBACK_HOSTS.

    An IPv4-mapped IPv6 address (`::ffff:127.0.0.1`) is unwrapped first, because
    a dual-stack socket reports v4 peers in that form and `.is_loopback` is
    False for the mapped form -- which would refuse the operator's own browser
    on a host where the listener is v6.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return address.is_loopback


def decode_proc_address(hex_address: str) -> str | None:
    """Turn /proc/net/tcp's `0100007F:1F90` local_address into `127.0.0.1`.

    Returns the host only; the port is dropped because every caller here asks
    "what interface" and not "what port". None means the field was not a shape
    this function understands, which is reported rather than guessed at -- a
    parser that returns "0.0.0.0" for input it did not recognize would refuse a
    loopback-bound control surface for the rest of time and look like a policy
    decision while doing it.

    THE BYTE ORDER IS THE WHOLE FUNCTION. Each 32-bit group is written in the
    host's byte order, which on every machine this runs on is little-endian, so
    `0100007F` is 7F.00.00.01 and reads 127.0.0.1 -- NOT 1.0.0.127. An IPv6
    address is four such groups, each individually byte-swapped, which is why
    the reversal is per 8 hex digits and not over the whole string.

    MEASURED, not assumed (rule 17): on this machine on 2026-10-02 a socket
    bound to 127.0.0.1 appeared as `0100007F` and one bound to 0.0.0.0 as
    `00000000`. The IPv6 half of this function is NOT measured here -- this
    sandbox has no AF_INET6 at all (`socket.socket(AF_INET6)` raises
    EAFNOSUPPORT) and no /proc/net/tcp6 -- so it is exercised by seeded text in
    tests/test_kill_switch.py and is honestly a parse checked against the
    documented format rather than against a live socket.
    """
    field = hex_address.split(":", 1)[0].strip()
    if len(field) not in (_IPV4_HEX_LENGTH, _IPV6_HEX_LENGTH):
        return None
    try:
        groups = [field[index:index + _IPV4_HEX_LENGTH] for index in range(0, len(field), _IPV4_HEX_LENGTH)]
        packed = b"".join(bytes.fromhex(group)[::-1] for group in groups)
        return str(ipaddress.ip_address(packed))
    except ValueError:
        return None


def listening_hosts_in(table_text: str, inodes: set[str]) -> set[str]:
    """The hosts the LISTEN rows belonging to `inodes` are bound to.

    A PURE FUNCTION OVER THE FILE'S TEXT, so the parser is testable without a
    socket and without a platform (rule 10: the decision is the smallest piece
    at the bottom). listening_addresses() is the part that does the I/O.

    Rows that are not LISTEN are skipped, and so are rows whose inode is not
    ours -- see this module's header for the measurement that makes the second
    filter load-bearing rather than tidy.
    """
    hosts: set[str] = set()
    for line in table_text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < _MINIMUM_COLUMNS:
            continue
        if fields[_STATE_COLUMN] != TCP_STATE_LISTEN:
            continue
        if fields[_INODE_COLUMN] not in inodes:
            continue
        host = decode_proc_address(fields[_LOCAL_ADDRESS_COLUMN])
        if host is not None:
            hosts.add(host)
    return hosts


def socket_inodes() -> set[str] | None:
    """The inode of every socket this process holds open, or None if it could not look.

    None rather than an empty set for an unreadable /proc/self/fd, because the
    two mean different things to a caller that must fail closed: "this process
    holds no sockets" is an answer, and "I cannot see this process's file
    descriptors" is not.
    """
    if not PROC_SELF_FD.is_dir():
        return None
    inodes: set[str] = set()
    try:
        entries = list(PROC_SELF_FD.iterdir())
    except OSError:
        # NOT a blind except (rule 12's BLE001 note): OSError only, and the
        # failure leaves the function returning None, which the caller is
        # required to treat as "could not establish" rather than as "clean".
        return None
    for entry in entries:
        try:
            target = str(entry.readlink())
        except OSError:
            # A descriptor that closed between the listing and the readlink.
            # Ordinary, and it is not an answer about any socket, so it is
            # skipped rather than failing the whole read.
            continue
        if target.startswith(_SOCKET_LINK_PREFIX):
            inodes.add(target[len(_SOCKET_LINK_PREFIX):-1])
    return inodes


def listening_addresses() -> set[str] | None:
    """Every host THIS process is accepting TCP connections on, or None if unknowable.

    The return value is the only thing in this tree that answers "is this server
    reachable off-box" with evidence instead of with intent. See the module
    header for what each weaker signal can and cannot establish.

    None  -- could not look. No /proc, or /proc/self/fd unreadable, or not one
             of the socket tables could be read. A caller guarding a control
             surface must refuse on None; a caller writing a banner may say
             "not established".
    set() -- looked, and this process holds no listening TCP socket. Under
             gunicorn that should be impossible: the master binds and every
             worker inherits the listening socket, so it appears in the worker's
             own /proc/self/fd. Under pytest it is the normal case, which is why
             the HTTP tests for the ALLOW direction bind a real loopback
             listener in the test process rather than mocking this out.
    """
    inodes = socket_inodes()
    if inodes is None:
        return None
    hosts: set[str] = set()
    read_any = False
    for path in PROC_NET_TCP_FILES:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            # /proc/net/tcp6 is genuinely absent on a host built without IPv6 --
            # measured on this machine 2026-10-02, where it does not exist -- so
            # a missing table is not a failure as long as one of them was read.
            continue
        read_any = True
        hosts |= listening_hosts_in(text, inodes)
    if not read_any:
        return None
    return hosts


#: Where the kernel records this process's routing table, inside a container as on a host.
#: /proc/net/route rather than `ip route`: no subprocess, no PATH dependency, and the file
#: is present in every container image this tree builds -- the web image is python:slim and
#: has no iproute2 at all, so a subprocess here would have failed on the only deployment
#: that needs this.
PROC_NET_ROUTE = "/proc/net/route"

#: The three fields this parse needs out of a /proc/net/route line: Iface, Destination,
#: Gateway, in that order. Named so the length check below is a statement about the format
#: rather than a bare 3 (ruff PLR2004, and it is right -- the number means something).
_ROUTE_MIN_FIELDS = 3

#: A /proc/net/route address field is a 32-bit value as 8 hex characters. A field of any
#: other length is not one, and is skipped rather than parsed into something plausible.
_ROUTE_HEX_WIDTH = 8

#: The destination of a DEFAULT route, and the gateway value that means "none recorded".
#: Both are the all-zero address written in this file's hex form.
_ROUTE_DEFAULT_DESTINATION = "00000000"

#: `0.0.0.0` as a GATEWAY means the route is direct -- on-link, no next hop -- so there is
#: no host address to admit and the scan continues. Spelled as a constant because ruff
#: flags the literal as a possible all-interfaces bind (S104) and it is not one: this is a
#: value being READ from the kernel and rejected, never a value being bound to.
_ROUTE_NO_GATEWAY = "0.0.0.0"  # noqa: S104 -- CHECKED: this is a value COMPARED AGAINST, never bound. default_gateway() reads it out of /proc/net/route and SKIPS the route it names; nothing in this module opens a socket or passes a host to a server. The comment block above says the same thing at length.


def default_gateway(path: str = PROC_NET_ROUTE) -> str | None:
    """The address this container's default route points at, or None if it cannot be read.

    =========================================================================
    WHY THIS EXISTS: THE OPERATOR'S CONTROLS WERE REFUSING THEIR OWN MACHINE
    =========================================================================

    Measured on their host 2026-10-10, off the rendered /admin/controls page:

        THESE CONTROLS ARE REFUSING. No button is rendered below.
        This request arrived from 172.18.0.1, which is not this machine.
        bind  SWAP_TERMINAL_PUBLISH_HOST: '127.0.0.1' -- declared loopback

    Both halves of that are worth reading together. The BIND guard passed -- the publish
    is declared loopback, so services/kill_switch.refuse_off_box() was satisfied. What
    refused was the PEER check, on 172.18.0.1.

    172.18.0.1 IS THE DOCKER BRIDGE GATEWAY, which is to say it is the operator's own
    machine, reaching the published port from the host side of the bridge. The peer check
    requires a loopback address and a bridged container never sees one from outside
    itself: the host's packets arrive from the gateway, and the container's loopback is
    reachable only from the container. So on the only deployment this repository actually
    ships -- docker compose, bridge network, published port -- the controls could not be
    reached by anybody, ever. The operator said it five times ("no controls. no buttons.
    nothing.") and the cause was this line, not the styling.

    THIS IS A RECOGNITION, NOT A RELAXATION, and the distinction is the whole argument for
    writing it rather than adding an override flag. The peer check exists to establish
    that the caller is the machine running the desk. On a bridged container the gateway IS
    that machine. Admitting it answers the check's own question correctly; it does not
    lower the bar.

    AND IT IS READ FROM THE KERNEL, NOT FROM THE REQUEST. Every other signal the controls
    refuse on is something a caller can write -- the Host header, the Origin, even the
    peer address behind a proxy. This one is the container's own routing table. A caller
    cannot change what this container's default route points at by sending a request, so
    using it as the comparison keeps the check evidential.

    WHAT IT DELIBERATELY DOES NOT DO. It does not say the request came from the host. It
    says the peer address EQUALS the gateway, which every other container on the same
    bridge does NOT -- they arrive from their own 172.18.0.x address, not from .1. That is
    the precise gap OPEN_FINDINGS finding 4 names (containers on this bridge reach this
    port directly, bypassing the publish), and this leaves it exactly where it was: a
    sibling container is still refused. Only the gateway is admitted, and only with the
    publish declared loopback, which the caller cannot declare.

    =========================================================================
    THE FORMAT, because a wrong parse here would admit the wrong address
    =========================================================================

    /proc/net/route is a header line then one route per line, tab-separated, with the
    addresses as LITTLE-ENDIAN HEX of the 32-bit value. A default route is the one whose
    Destination is 00000000; its Gateway field holds the address. Read on the operator's
    own container:

        Iface  Destination  Gateway   Mask      ...
        eth0   00000000     010012AC  00000000  ...
        eth0   000012AC     00000000  0000FFFF  ...

    010012AC little-endian is AC.12.00.01 = 172.18.0.1. The byte order is the trap: read
    big-endian it is 1.0.18.172, which is a real routable address belonging to somebody
    else, and a check that admitted it would be admitting a stranger. Hence the explicit
    reversal below and tests/test_loopback.py's vector for exactly this value.

    RETURNS None RATHER THAN GUESSING on anything it cannot read or parse -- a missing
    file, a short line, a malformed field, no default route. The caller treats None as "no
    gateway established" and refuses, which is the same posture
    listening_addresses() takes: a control surface that cannot establish a fact must not
    assume the permissive answer.
    """
    try:
        with Path(path).open(encoding="ascii") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in lines[1:]:
        fields = line.split()
        # Iface, Destination, Gateway, ... -- three is the minimum this needs.
        if len(fields) < _ROUTE_MIN_FIELDS:
            continue
        destination, gateway = fields[1], fields[2]
        if destination != _ROUTE_DEFAULT_DESTINATION:
            continue
        if len(gateway) != _ROUTE_HEX_WIDTH:
            continue
        try:
            packed = bytes.fromhex(gateway)
        except ValueError:
            return None
        # LITTLE-ENDIAN, which is what the docstring's 010012AC vector pins. Reversed
        # here rather than by int.from_bytes(..., "little") plus a format, because the
        # four bytes ARE the four octets and reversing them is the whole conversion.
        octets = ".".join(str(byte) for byte in reversed(packed))
        if octets == _ROUTE_NO_GATEWAY:
            continue
        return octets
    return None
