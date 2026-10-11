"""ICP_MIN_CONFIRMATIONS above 1 is refused at swap creation, not discovered later.

Role: tests (chains/registry.validate_min_confirmations() and the chokepoint it guards)
Reads: a throwaway SQLite database under tmp_path; one stub ledger that opens no socket
Writes: nothing outside tmp_path
Can move funds: no
Mainnet-safe: yes

=============================================================================
IT STRANDED EVERY ICP DEPOSIT IN `confirming` FOREVER, SILENTLY
=============================================================================

MEASURED 2026-10-11 by running the real services/deposit_service.process_active_swaps()
forty times over one seeded ICP->GRC swap, with the deposit event carrying the
confirmations the REAL ICPAdapter.deposit_confirmations() returns:

    ICPAdapter.deposit_confirmations() = 1
    swaps.min_confirmations            = 2
    after 40 cycles: {'status': 'confirming', 'actual_input_amount': 2.44081155,
                      'credited_at': None}
    deposit_events: [{'txid': '2', 'amount': 2.44081155, 'confirmations': 1,
                      'credited_at': None}]
    under_review count: 0

The gate can never close. `confirmed_total` sums only events whose confirmations are at
or above the threshold, so it is permanently 0; `_advance_to_detected()` writes
`confirming` once and `_settle_confirmed_amount()` returns on `confirmed_total <= 0` on
every cycle after that. Nothing halts, nothing logs, and no root tool can move it -- the
customer has paid, the money is recorded, and the swap is dead with no sign of it.

ICP_MIN_CONFIRMATIONS IS AN OPERATOR KNOB, defaulting to 1 in config.py and plumbed
through docker-compose.web.yml, so this is reachable by editing one environment
variable.

WHY NOT AN ICP-SHAPED CHECK. XRP and SOL both refuse a bad threshold in their adapters'
CONSTRUCTORS, which fires earlier and harder -- no adapter is built at all. ICP has no
such check because it builds from a ledger id and a principal and never reads the
threshold. A third ICP-only validator would close this for ICP alone, so the refusal
lives at services/swap_service.get_min_confirmations(), the single place every asset's
threshold passes through on its way onto a swap row, and consults a table. The next
finality-at-one-block chain is one line in a frozenset.
"""

from __future__ import annotations

import pytest
import valid_addresses
from chains.icp import ICPAdapter
from chains.icp_account import account_identifier
from chains.registry import (
    FINAL_AT_ONE_CONFIRMATION,
    MinConfirmationsUnreachable,
    validate_min_confirmations,
)
from config import Config
from db import SCHEMA, apply_migrations, connect_db
from services import deposit_service
from services.swap_service import get_min_confirmations

PRINCIPAL = "2vxsx-fae"
ADDRESS = account_identifier(PRINCIPAL, None)
DEPOSIT_ICP = 2.44081155

CONFIG = {name: getattr(Config, name) for name in dir(Config) if name.isupper()}


class FinalAtOneLedger:
    """A ledger holding one deposit at the confirmations ICP actually reports.

    THE CONFIRMATIONS COME FROM THE REAL ADAPTER METHOD rather than from a literal 1,
    because the whole defect is the relationship between what the chain reports and what
    the threshold demands. A hardcoded 1 here would still pass if ICPAdapter started
    reporting something else, and the test would stop measuring the thing it is for.
    """

    asset = "ICP"

    def find_deposits_to_address(self, address, skip_txids=frozenset(), **_kwargs):
        return [{"txid": "2", "vout": 0, "address": address, "amount": DEPOSIT_ICP,
                 "confirmations": self.deposit_confirmations()}]

    def deposit_confirmations(self, *_args, **_kwargs):
        return ICPAdapter.deposit_confirmations(None)


def seed_icp_swap(conn, min_confirmations: int) -> None:
    conn.execute(
        "INSERT INTO quotes (id, from_asset, to_asset, input_amount, quoted_rate, fee_bps,"
        " network_fee_reserve, output_amount_estimate, expires_at, created_at)"
        " VALUES ('q','ICP','GRC',?,409.7,150,0.001,1000.0,"
        "'2999-01-01T00:00:00+00:00','2026-10-11T00:00:00+00:00')",
        (DEPOSIT_ICP,),
    )
    conn.execute(
        "INSERT INTO swaps (id, quote_id, from_asset, to_asset, deposit_address, payout_address,"
        " expected_input_amount, quoted_rate, fee_bps, network_fee_reserve,"
        " output_amount_estimate, status, min_confirmations, expires_at, created_at, updated_at)"
        " VALUES ('s_icp','q','ICP','GRC',?,?,?,409.7,150,0.001,1000.0,'awaiting_deposit',?,"
        "'2999-01-01T00:00:00+00:00','2026-10-11T00:00:00+00:00','2026-10-11T00:00:00+00:00')",
        (ADDRESS, valid_addresses.GRC_PAYOUT, DEPOSIT_ICP, min_confirmations),
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    connection = connect_db(str(tmp_path / "threshold.db"), create=True)
    connection.executescript(SCHEMA)
    apply_migrations(connection)
    return connection


def test_the_threshold_is_refused_at_the_chokepoint_every_asset_passes_through():
    """THE FIX. MUTATION: return `int(config[...])` as it did, and this fails.

    get_min_confirmations() is what writes `swaps.min_confirmations`, so refusing here is
    refusing BEFORE a customer is handed a deposit address.
    """
    assert get_min_confirmations({"ICP_MIN_CONFIRMATIONS": 1}, "ICP") == 1

    with pytest.raises(MinConfirmationsUnreachable) as caught:
        get_min_confirmations({"ICP_MIN_CONFIRMATIONS": 2}, "ICP")

    message = str(caught.value)
    assert "ICP_MIN_CONFIRMATIONS=2" in message, "the variable and its value are not named"
    assert "final at ONE block" in message, "WHY it can never be satisfied is not stated"
    assert "Set ICP_MIN_CONFIRMATIONS=1" in message, "no remedy on the screen (rule 14)"


def test_every_other_asset_passes_through_untouched():
    """BTC's six confirmations are a real chain fact and this has no opinion about them.

    MUTATION: drop the `asset in FINAL_AT_ONE_CONFIRMATION` guard and this fails on BTC
    -- which would be the far worse defect, since it would refuse every swap on every
    chain with a real depth requirement.
    """
    for asset, value in (("BTC", 6), ("LTC", 2), ("GRC", 6), ("SOL", 1), ("XRP", 1)):
        assert validate_min_confirmations(asset, value) == value
        assert get_min_confirmations({f"{asset}_MIN_CONFIRMATIONS": value}, asset) == value


def test_the_measured_stranding_cannot_be_reached_through_create_but_IS_what_happens_if_set(conn):
    """BOTH HALVES, because the fix is a refusal and not a change to the crediting path.

    A row that already carries 2 -- written before this refusal existed, or by hand --
    still strands, and that is correct: deposit_service is not the right place to
    second-guess a threshold a swap was created with. So this asserts the stranding is
    REAL (which is what makes the refusal worth having) and that the refusal is what
    stops a new swap being created that way.

    FORTY CYCLES, matching the original measurement, because one cycle proves only that
    it did not credit immediately -- the claim is that it never will.
    """
    seed_icp_swap(conn, min_confirmations=2)
    ledger = FinalAtOneLedger()

    for _ in range(40):
        deposit_service.process_active_swaps(conn, CONFIG, {"ICP": ledger})

    row = dict(conn.execute("SELECT * FROM swaps WHERE id = 's_icp'").fetchone())
    assert row["status"] == "confirming", "the stranding is not reproduced; this test proves nothing"
    assert row["credited_at"] is None
    # NOT HALTED EITHER, which is the part that makes it invisible: `under_review` would
    # at least be a state a person is meant to look at.
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM swaps WHERE status = 'under_review'"
    ).fetchone()["n"] == 0

    # AND THE SWAP THAT CANNOT BE CREATED THAT WAY ANY MORE.
    with pytest.raises(MinConfirmationsUnreachable):
        get_min_confirmations({"ICP_MIN_CONFIRMATIONS": 2}, "ICP")


def test_a_threshold_of_one_credits_the_same_deposit(conn):
    """The control. Without it, the test above could be measuring anything.

    Same ledger, same deposit, same forty cycles -- the only difference is the threshold,
    so the stranding is attributable to it and to nothing else in the fixture.
    """
    seed_icp_swap(conn, min_confirmations=1)

    deposit_service.process_active_swaps(conn, CONFIG, {"ICP": FinalAtOneLedger()})

    row = dict(conn.execute("SELECT * FROM swaps WHERE id = 's_icp'").fetchone())
    assert row["status"] != "confirming", f"a satisfiable threshold also stranded: {row['status']}"
    assert row["credited_at"] is not None


def test_it_raises_rather_than_clamping():
    """The decision, stated and pinned.

    A silent clamp to 1 would make the environment's stated intent quietly untrue, and an
    operator who set 2 on purpose -- having misread ICP as a chain with depth -- would get
    a system behaving differently from its own configuration with nothing saying so. Rule
    14: a value that was ignored has to say it was ignored, and here the loud version is a
    refusal before a deposit address is handed out.

    MUTATION: `return 1` instead of raising.
    """
    with pytest.raises(MinConfirmationsUnreachable):
        validate_min_confirmations("ICP", 2)


def test_the_table_names_only_chains_with_no_constructor_check():
    """Rule 8: XRP and SOL are deliberately absent, and the difference is the point.

    chains/xrp_units.validate_min_confirmations() refuses a bad XRP threshold inside the
    ADAPTER's constructor, and SOL does the same -- which fires earlier and harder, since
    no adapter is built at all. Listing them here would be two refusals for one fact and
    the second would never fire.

    SO THIS TABLE IS FOR CHAINS WHOSE THRESHOLD REACHES A SWAP ROW UNVALIDATED, and ICP
    is the one: it builds from a ledger id and a principal and never reads the value. A
    future entry means the same thing, which is why the assertion is on the membership
    and not merely on the size.
    """
    assert "ICP" in FINAL_AT_ONE_CONFIRMATION
    assert "XRP" not in FINAL_AT_ONE_CONFIRMATION, (
        "XRP is final at one block but its adapter already refuses a bad threshold; a second "
        "refusal here would never fire. See the comment on FINAL_AT_ONE_CONFIRMATION."
    )
    assert "SOL" not in FINAL_AT_ONE_CONFIRMATION
    # And no chain with real depth may ever be in here.
    for with_depth in ("BTC", "LTC", "GRC"):
        assert with_depth not in FINAL_AT_ONE_CONFIRMATION


def test_the_default_config_is_satisfiable_for_every_tradeable_asset():
    """A gate on the SHIPPED values, which is what an unconfigured checkout runs.

    Without this, the refusal above could be correct and config.py could still default
    ICP to something unreachable -- and the first ICP swap anybody created would refuse,
    which would read as the fix being broken rather than as the default being wrong.
    """
    assets = {asset for pair in Config.ALLOWED_PAIRS for asset in pair}
    for asset in sorted(assets):
        value = CONFIG.get(f"{asset}_MIN_CONFIRMATIONS")
        assert value is not None, f"{asset} is tradeable and has no {asset}_MIN_CONFIRMATIONS"
        assert validate_min_confirmations(asset, value) == int(value), (
            f"the SHIPPED default {asset}_MIN_CONFIRMATIONS={value} is not satisfiable"
        )
