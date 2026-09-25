#!/usr/bin/env python3
"""Tkinter front end for the atomic-swap modules: validate, quote, and SWAP.

Role: file (an operator-facing GUI and the only caller of the atomic-swap
      modules)
Reads: BTC/LTC/GRC wallet RPC through the three atomic clients
      (getreceivedbyaddress, listunspent), CoinGecko through
      modules/market_data.py, and BTC_RPC_*/LTC_RPC_*/GRC_RPC_* from the
      environment
Writes: nothing to disk. THE CHAIN, through Swapper.start_swap().
Can move funds: YES. The "Swap" button calls modules/atomic_swapper.py's
      start_swap(), which funds an HTLC on the initiator's chain. There is no
      confirmation step between the button and the broadcast beyond a balance
      check.
Mainnet-safe: NO, and it cannot be made mainnet-safe by configuration:
      modules/atomic_htlc_scripts.py hardcodes testnet version bytes, so every
      contract address this GUI can produce is a testnet address.

BEFORE USING THE SWAP BUTTON, READ modules/atomic_swapper.py's HEADER. The
three measured defects that used to sit between this button and a working swap
-- a locktime encoded as a varint rather than a script number, a locktime
hardcoded to a block height already in the past, and a participant address that
was the operator's own -- were fixed together on 2026-09-24. None of them has
been proven on a chain: no contract built by this GUI has ever been funded and
then refunded after expiry, and that is the only proof that settles the refund
branch.

THAT FIX ADDED A FIELD TO THIS WINDOW. "Counterparty's Address (chain you
fund)" is the participant address -- THEIRS, on the chain being funded -- and
the three "Your Testnet <coin> Address" fields are yours. The one for the coin
being sent becomes the refund address. The swap refuses to start if the two are
the same string.

The swap result shown in the "Swap Complete" message box CONTAINS THE HTLC
PREIMAGE, on purpose -- the initiator needs it to redeem the counterparty's leg
and there is no other channel. Do not paste that box anywhere. It is the one
value in this system that cannot be un-revealed.
"""

import logging
import os
import tkinter as tk
from decimal import Decimal
from tkinter import messagebox

from dotenv import load_dotenv

# Load environment variables.
load_dotenv()

# RPC settings.
BTC_RPC_URL = os.environ.get("BTC_RPC_URL")
BTC_RPC_USER = os.environ.get("BTC_RPC_USER")
BTC_RPC_PASS = os.environ.get("BTC_RPC_PASS")
LTC_RPC_URL = os.environ.get("LTC_RPC_URL")
LTC_RPC_USER = os.environ.get("LTC_RPC_USER")
LTC_RPC_PASS = os.environ.get("LTC_RPC_PASS")
GRC_RPC_URL = os.environ.get("GRC_RPC_URL")
GRC_RPC_USER = os.environ.get("GRC_RPC_USER")
GRC_RPC_PASS = os.environ.get("GRC_RPC_PASS")

# THESE IMPORTS MUST STAY BELOW load_dotenv(), WHICH IS WHY E402 IS SUPPRESSED
# ON EACH OF THEM RATHER THAN THE BLOCK BEING MOVED UP.
#
# modules/atomic_grc_client.py reads GRC_WALLET_PASSPHRASE in a DEFAULT
# ARGUMENT -- `wallet_passphrase: str = os.environ.get(...)` -- which Python
# evaluates once, when the class body executes, i.e. at import. Importing it
# before load_dotenv() therefore bakes in an EMPTY passphrase, and
# ensure_fully_unlocked() then silently skips the wallet unlock, so every GRC
# contract and redeem fails on a locked wallet with a confusing error.
#
# The real fix is to move that lookup into GRCClient.__init__, which is a
# fund-path change (it decides whether the wallet gets unlocked) and is handed
# to the operator rather than made here (rule 16). Until then, this ordering is
# load-bearing and the suppression records why.
from modules.atomic_btc_client import BTCClient  # noqa: E402
from modules.atomic_grc_client import GRCClient  # noqa: E402
from modules.atomic_htlc_scripts import parse_and_reencode_as_testnet_p2pkh  # noqa: E402
from modules.atomic_ltc_client import LTCClient  # noqa: E402
from modules.atomic_swapper import Swapper  # noqa: E402

# Import market data functions.
from modules.market_data import fetch_btc_ltc_prices, fetch_grc_price  # noqa: E402

# Minimum length accepted for a Gridcoin address by the GUI's field check.
# See the note at its use site: this is a length check, not validation.
MIN_GRC_ADDRESS_LEN = 10

# THIS FILE IS AN APPLICATION, so it is allowed to decide logging policy --
# and after 2026-09-25 it is the only thing that does, for everything it
# imports. It used to attach a DEBUG StreamHandler to its OWN logger only,
# which was two defects at once:
#
#   - it left modules.atomic_*_client to configure themselves, and they did,
#     at DEBUG with their own handler, which is how a preimage reached stderr
#     (see describe_rpc_payload() in modules/htlc_rpc.py);
#   - and now that those modules configure nothing, a per-module handler here
#     would leave every line the swap path emits with nowhere to go, which is
#     rule 14's silence.
#
# basicConfig fixes both: one handler at the root, at INFO. INFO rather than
# DEBUG is the substantive choice -- the DEBUG lines in this package are the
# RPC request and response bodies, and the only reason to want them is the one
# reason nobody should have them by default.
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class AtomicSwapGUI:
    def __init__(self, master: tk.Tk) -> None:
        """
        Initialize the Atomic Swap GUI.
        
        Args:
            master (tk.Tk): The root Tkinter window.
        """
        self.master = master
        master.title("Atomic Swap (BTC, LTC, GRC)")
        
        # Instantiate client objects once for balance queries.
        self.btc_client = BTCClient(BTC_RPC_URL, BTC_RPC_USER, BTC_RPC_PASS)
        self.ltc_client = LTCClient(LTC_RPC_URL, LTC_RPC_USER, LTC_RPC_PASS)
        self.grc_client = GRCClient(GRC_RPC_URL, GRC_RPC_USER, GRC_RPC_PASS)
        
        # Track validation status and validated addresses.
        self.validation_status: dict[str, bool] = {"BTC": False, "LTC": False, "GRC": False}
        self.validated_addresses: dict[str, str] = {}
        
        self.setup_ui()
        self.update_field_states()

    def setup_ui(self) -> None:
        """Sets up the GUI elements."""
        # Swap Direction
        self.label_direction = tk.Label(self.master, text="Swap Direction:")
        self.label_direction.grid(row=0, column=0, sticky="e")
        self.direction_var = tk.StringVar(self.master, value="BTC2LTC")
        directions = ["BTC2LTC", "LTC2BTC", "BTC2GRC", "GRC2BTC", "LTC2GRC", "GRC2LTC"]
        self.optionmenu_direction = tk.OptionMenu(self.master, self.direction_var, *directions, command=self.direction_changed)
        self.optionmenu_direction.grid(row=0, column=1, padx=5, pady=5, sticky="w")
        
        # Address Fields with additional balance labels.
        self.setup_address_field("BTC", 1)
        self.setup_address_field("LTC", 2)
        self.setup_address_field("GRC", 3)
        
        # COUNTERPARTY (PARTICIPANT) ADDRESS. This field did not exist before
        # 2026-09-24, and its absence was the third of the three HTLC defects:
        # modules/atomic_swapper.py had nowhere to get a counterparty address
        # from, so it passed the operator's OWN address as both the participant
        # and the refund address and built a contract whose two branches needed
        # the same key. The three fields above are "Your <coin> address"; this
        # one is THEIRS, on the chain being funded, and the two can never be
        # the same string -- start_swap() refuses that.
        self.label_counterparty = tk.Label(self.master, text="Counterparty's Address (chain you fund):")
        self.label_counterparty.grid(row=4, column=0, sticky="e")
        self.entry_counterparty = tk.Entry(self.master, width=45)
        self.entry_counterparty.grid(row=4, column=1, padx=5, pady=5)

        # Swap Amount
        self.label_swap_amount = tk.Label(self.master, text="Swap Amount (BTC for BTC2LTC):")
        self.label_swap_amount.grid(row=5, column=0, sticky="e")
        self.entry_swap_amount = tk.Entry(self.master, width=10)
        self.entry_swap_amount.grid(row=5, column=1, padx=5, pady=5, sticky="w")
        self.entry_swap_amount.insert(0, "0.001")
        
        # Execute Swap Button
        self.button_swap = tk.Button(self.master, text="Execute Swap", command=self.swap_coins, state="disabled")
        self.button_swap.grid(row=6, column=1, pady=10, sticky="e")
        
        # Marquee for market data
        self.marquee_label = tk.Label(self.master, text="", bg="black", fg="lime", font=("Courier", 12, "bold"))
        self.marquee_label.grid(row=7, column=0, columnspan=4, sticky="we", pady=5)
        self.marquee_text = ""
        self.start_marquee()

    def setup_address_field(self, coin: str, row: int) -> None:
        """
        Creates and sets up the address field for a coin,
        along with status and balance labels.
        
        Args:
            coin (str): The coin symbol (e.g., "BTC", "LTC", "GRC").
            row (int): The row number in the grid.
        """
        label = tk.Label(self.master, text=f"Your Testnet {coin} Address:")
        label.grid(row=row, column=0, sticky="e")
        entry = tk.Entry(self.master, width=45)
        entry.grid(row=row, column=1, padx=5, pady=5)
        status_label = tk.Label(self.master, text="", fg="red")
        status_label.grid(row=row, column=2, padx=5)
        balance_label = tk.Label(self.master, text="", fg="blue")
        balance_label.grid(row=row, column=3, padx=5)
        
        # Bind events to validate the address.
        entry.bind("<FocusOut>", lambda e: self.validate_address(coin))
        entry.bind("<KeyRelease>", lambda e: self.validate_address(coin))
        
        setattr(self, f"entry_{coin.lower()}_address", entry)
        setattr(self, f"status_{coin.lower()}", status_label)
        setattr(self, f"balance_{coin.lower()}", balance_label)

    def update_field_states(self) -> None:
        """
        Enable or disable address fields based on the selected swap direction.
        For coins not required for the swap, mark them as validated.
        """
        direction = self.direction_var.get()
        fields = {
            "BTC": self.entry_btc_address,
            "LTC": self.entry_ltc_address,
            "GRC": self.entry_grc_address
        }
        required = {
            "BTC2LTC": ["BTC", "LTC"],
            "LTC2BTC": ["BTC", "LTC"],
            "BTC2GRC": ["BTC", "GRC"],
            "GRC2BTC": ["BTC", "GRC"],
            "LTC2GRC": ["LTC", "GRC"],
            "GRC2LTC": ["LTC", "GRC"]
        }.get(direction, [])
        
        for coin, field in fields.items():
            if coin in required:
                field.config(state="normal")
            else:
                field.config(state="disabled")
                self.validation_status[coin] = True
                self.validated_addresses.pop(coin, None)
                # Clear any displayed balance.
                getattr(self, f"balance_{coin.lower()}").config(text="")
        
        self.update_swap_button_state()

    def validate_address(self, coin: str) -> None:
        """
        Validate the user-entered address for a coin.
        For BTC and LTC, it attempts to parse and re-encode using testnet rules.
        For GRC, a minimal length check is performed.
        If valid, automatically check and display the balance.
        
        Args:
            coin (str): The coin symbol.
        """
        fields = {
            "BTC": self.entry_btc_address,
            "LTC": self.entry_ltc_address,
            "GRC": self.entry_grc_address
        }
        status_label = getattr(self, f"status_{coin.lower()}")
        address = fields[coin].get().strip()
        
        status_label.config(text="")
        valid = False
        validated = None
        if not address:
            logger.debug(f"No {coin} address provided.")
            valid = False
        else:
            try:
                if coin in ["BTC", "LTC"]:
                    validated = parse_and_reencode_as_testnet_p2pkh(address)
                    valid = True
                elif coin == "GRC":
                    # "Minimal validation" means a LENGTH CHECK: any string of
                    # at least MIN_GRC_ADDRESS_LEN characters is accepted as a
                    # Gridcoin address and shown with a green tick. It is not
                    # checked against the daemon, its checksum is not verified,
                    # and its version byte is not read. The other two coins go
                    # through parse_and_reencode_as_testnet_p2pkh(), which at
                    # least decodes. Tightening this is address validation on
                    # the fund path (rule 16) -- reported, not changed.
                    if len(address) >= MIN_GRC_ADDRESS_LEN:
                        validated = address
                        valid = True
                    else:
                        valid = False
            except Exception as e:  # noqa: BLE001 -- checked: an address that fails to parse IS an invalid address for this field, and the GUI shows a red mark either way. Note it does not distinguish "malformed" from "the parser raised for another reason"; neither reaches a broadcast, because the swap path re-derives from self.validated_addresses.
                logger.debug(f"Validation error for {coin} address '{address}': {e}")
                valid = False
        
        if valid and validated:
            status_label.config(text="✓", fg="green")
            self.validated_addresses[coin] = validated
            # Automatically check balance for valid address.
            self.check_balance(coin, validated)
        else:
            status_label.config(text="✗", fg="red")
            self.validated_addresses.pop(coin, None)
            getattr(self, f"balance_{coin.lower()}").config(text="")
        
        self.validation_status[coin] = valid
        self.update_swap_button_state()

    def check_balance(self, coin: str, address: str) -> None:
        """
        Check the balance for the given coin and address, and update the balance label.
        
        Args:
            coin (str): The coin symbol.
            address (str): The validated address.
        """
        try:
            if coin == "BTC":
                balance = self.btc_client.get_address_balance(address)
            elif coin == "LTC":
                balance = self.ltc_client.get_address_balance(address)
            elif coin == "GRC":
                balance = self.grc_client.get_address_balance(address)
            else:
                balance = Decimal(0)
            # Format the balance with 9 decimal places.
            balance_text = f"Balance: {balance:.9f}"
            getattr(self, f"balance_{coin.lower()}").config(text=balance_text)
            logger.debug(f"{coin} balance for {address}: {balance}")
        except Exception as e:  # noqa: BLE001 -- checked: the field shows "Balance: Error", which is visibly different from a number and from a zero. That is rule 14's "make did-nothing look different from did-work" at widget level, and it is why the broad catch is acceptable: the operator cannot mistake the failure for a balance.
            logger.error(f"Error checking {coin} balance for {address}: {e}")
            getattr(self, f"balance_{coin.lower()}").config(text="Balance: Error")

    def update_swap_button_state(self) -> None:
        """
        Enable or disable the swap button based on the validation status of the required addresses.
        """
        direction = self.direction_var.get()
        required = {
            "BTC2LTC": ["BTC", "LTC"],
            "LTC2BTC": ["BTC", "LTC"],
            "BTC2GRC": ["BTC", "GRC"],
            "GRC2BTC": ["BTC", "GRC"],
            "LTC2GRC": ["LTC", "GRC"],
            "GRC2LTC": ["LTC", "GRC"]
        }.get(direction, [])
        
        if all(self.validation_status.get(coin, True) for coin in required):
            self.button_swap.config(state="normal")
        else:
            self.button_swap.config(state="disabled")

    def direction_changed(self, value: str) -> None:
        """
        Handle changes in the swap direction.
        
        Args:
            value (str): The new swap direction.
        """
        logger.debug(f"Swap direction changed to {value}")
        self.update_field_states()
        label_map = {
            "BTC2LTC": "Swap Amount (BTC for BTC2LTC):",
            "LTC2BTC": "Swap Amount (LTC for LTC2BTC):",
            "BTC2GRC": "Swap Amount (BTC for BTC2GRC):",
            "GRC2BTC": "Swap Amount (GRC for GRC2BTC):",
            "LTC2GRC": "Swap Amount (LTC for LTC2GRC):",
            "GRC2LTC": "Swap Amount (GRC for GRC2LTC):"
        }
        self.label_swap_amount.config(text=label_map.get(value, "Swap Amount:"))

    def swap_coins(self) -> None:
        """
        Execute the coin swap based on the selected swap direction.
        It validates the swap amount, checks balances, and initiates the swap through the Swapper.
        """
        direction = self.direction_var.get()
        try:
            swap_amount = Decimal(self.entry_swap_amount.get().strip())
        except Exception as e:  # noqa: BLE001 -- checked: Decimal() on operator input raises InvalidOperation and several ValueError subclasses depending on what was typed. Every one of them means "that is not an amount", the box says so, and the function RETURNS -- no swap is started.
            messagebox.showerror("Error", f"Invalid swap amount: {e}")
            return
        
        # Check balance of the source coin.
        from_coin, balance, fee = self.get_swap_parameters(direction)
        if from_coin is None:
            return
        if swap_amount + fee > balance:
            messagebox.showerror("Insufficient Funds", f"Insufficient funds in your {from_coin} address.")
            return
        
        addresses = self.get_validated_addresses(direction)
        if not addresses:
            return

        # The refund address is OURS on the chain being funded -- from_coin is
        # that chain, so the operator's own validated address for it is the
        # only correct choice and is not a free parameter. The participant
        # address is the COUNTERPARTY's on the same chain, which only they can
        # supply; there is nothing in this process that could derive it.
        refund_address = addresses.get(from_coin, "")
        participant_address = self.entry_counterparty.get().strip()
        if not participant_address:
            messagebox.showerror(
                "Counterparty Address Required",
                f"Enter the counterparty's {from_coin} address (the chain you are funding).\n\n"
                "It is the address that can claim this contract by revealing the preimage. "
                "Your own address goes in the refund branch and is taken from the field above.",
            )
            return
        if participant_address == refund_address:
            messagebox.showerror(
                "Addresses Must Differ",
                "The counterparty address and your own address are the same.\n\n"
                "Both branches of the HTLC would then need the same key, so the counterparty could never "
                "redeem it with the preimage.",
            )
            return

        swapper = Swapper(self.btc_client, self.ltc_client, self.grc_client)
        try:
            result = swapper.start_swap(
                swap_direction=direction,
                participant_address=participant_address,
                refund_address=refund_address,
                swap_amount=swap_amount
            )
            messagebox.showinfo("Swap Complete", f"Swap {direction} Completed!\n\n{result}")
        # Checked, and this is the most consequential handler in the file: it
        # wraps start_swap(), which BROADCASTS. A failure here is reported as
        # "Swap failed" -- but an exception raised AFTER create_contract() has
        # already sent funds to the P2SH address (for example a timeout inside
        # wait_for_tx_output) shows the operator the same message as a failure
        # that sent nothing. The contract txid is then only in the log.
        # Distinguishing them means returning partial state from start_swap(),
        # which is a fund-path change (rule 16) and is reported, not made.
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Swap Error", f"Swap failed:\n{e}")

    def get_swap_parameters(self, direction: str) -> tuple:
        """
        Returns the source coin, balance, and fee based on the selected swap direction.
        
        Args:
            direction (str): The swap direction.
            
        Returns:
            tuple: (from_coin, balance, fee) or (None, None, None) if error.
        """
        if direction in ["BTC2LTC", "BTC2GRC"]:
            from_coin = "BTC"
            balance = self.btc_client.get_address_balance(self.validated_addresses["BTC"])
            fee = Decimal("0.0001")
        elif direction in ["LTC2BTC", "LTC2GRC"]:
            from_coin = "LTC"
            balance = self.ltc_client.get_address_balance(self.validated_addresses["LTC"])
            fee = Decimal("0.0001")
        elif direction in ["GRC2BTC", "GRC2LTC"]:
            from_coin = "GRC"
            balance = self.grc_client.get_address_balance(self.validated_addresses["GRC"])
            fee = Decimal("0.01")
        else:
            messagebox.showerror("Error", "Invalid swap direction.")
            return None, None, None
        return from_coin, balance, fee

    def get_validated_addresses(self, direction: str) -> dict[str, str]:
        """
        Retrieve the validated addresses based on the swap direction.
        
        Args:
            direction (str): The swap direction.
            
        Returns:
            Dict[str, str]: A dictionary of validated addresses for the required coins.
        """
        mapping = {
            "BTC2LTC": ["BTC", "LTC"],
            "LTC2BTC": ["BTC", "LTC"],
            "BTC2GRC": ["BTC", "GRC"],
            "GRC2BTC": ["BTC", "GRC"],
            "LTC2GRC": ["LTC", "GRC"],
            "GRC2LTC": ["LTC", "GRC"]
        }
        required = mapping.get(direction, [])
        try:
            return {coin: self.validated_addresses[coin] for coin in required}
        except KeyError as e:
            messagebox.showerror("Error", f"Missing or invalid address for {e.args[0]}")
            return {}

    # --- Marquee Functionality ---
    def start_marquee(self) -> None:
        """Begin updating market data and scrolling the marquee."""
        self.update_marquee_prices()
        self.scroll_marquee()

    def update_marquee_prices(self) -> None:
        """
        Fetch and update the displayed prices for BTC, LTC, and GRC.
        Market data is refreshed every 3 minutes. Prices are displayed with 2 decimal places.
        """
        prices = fetch_btc_ltc_prices()  # Uses cached data.
        btc_price = prices.get("BTC")
        ltc_price = prices.get("LTC")
        grc_price = fetch_grc_price()
        try:
            btc_price_str = f"{float(btc_price):,.2f}" if btc_price != "N/A" else "N/A"
            ltc_price_str = f"{float(ltc_price):,.2f}" if ltc_price != "N/A" else "N/A"
            grc_price_str = f"{float(grc_price):,.2f}" if grc_price != "N/A" else "N/A"
        except Exception:  # noqa: BLE001 -- checked: the price sources return the STRING "N/A" on failure (see modules/market_data.py), so float() on one raises. The fallback shows the raw value in the marquee, which reads as "N/A" rather than as a price. Display only; nothing decides on it.
            btc_price_str, ltc_price_str, grc_price_str = btc_price, ltc_price, grc_price

        self.marquee_text = f" BTC: ${btc_price_str} | LTC: ${ltc_price_str} | GRC: ${grc_price_str} | "
        self.master.after(180000, self.update_marquee_prices)  # Refresh every 3 minutes.

    def scroll_marquee(self) -> None:
        """Scroll the marquee text one character at a time."""
        if self.marquee_text:
            self.marquee_label.config(text=self.marquee_text)
            self.marquee_text = self.marquee_text[1:] + self.marquee_text[0]
        self.master.after(150, self.scroll_marquee)

def main() -> None:
    root = tk.Tk()
    # The GUI object is not bound to a name: Tk keeps it alive through the
    # widget tree, and an unused local is rule 9's dead name.
    AtomicSwapGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
