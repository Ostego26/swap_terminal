#!/usr/bin/env python3
"""
File: atomic_swap_gui.py

Description:
  This script implements an atomic swap GUI and swap logic for BTC, LTC, and GRC.
  It validates user-entered addresses, disables fields not required for the chosen swap direction,
  and initiates the swap using the unified Swapper class and coin client modules.
  
  Additionally, once a valid address is entered, it automatically queries and displays its balance
  (via RPC calls or UTXO summing).
  
  NOTE: Currently, only the BTC2LTC swap direction is fully implemented.
"""

import tkinter as tk
from tkinter import messagebox
from decimal import Decimal
import os
import logging
from dotenv import load_dotenv
from typing import Dict
from modules.utils import generate_secret, sha256_hash

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

# Import unified Swapper and coin clients.
from modules.atomic_swapper import Swapper
from modules.atomic_btc_client import BTCClient
from modules.atomic_ltc_client import LTCClient
from modules.atomic_grc_client import GRCClient
from modules.atomic_htlc_scripts import parse_and_reencode_as_testnet_p2pkh

# Import market data functions.
from modules.market_data import fetch_btc_ltc_prices, fetch_grc_price

# Configure logger.
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)


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
        self.validation_status: Dict[str, bool] = {"BTC": False, "LTC": False, "GRC": False}
        self.validated_addresses: Dict[str, str] = {}
        
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
        
        # Swap Amount
        self.label_swap_amount = tk.Label(self.master, text="Swap Amount (BTC for BTC2LTC):")
        self.label_swap_amount.grid(row=4, column=0, sticky="e")
        self.entry_swap_amount = tk.Entry(self.master, width=10)
        self.entry_swap_amount.grid(row=4, column=1, padx=5, pady=5, sticky="w")
        self.entry_swap_amount.insert(0, "0.001")
        
        # Execute Swap Button
        self.button_swap = tk.Button(self.master, text="Execute Swap", command=self.swap_coins, state="disabled")
        self.button_swap.grid(row=5, column=1, pady=10, sticky="e")
        
        # Marquee for market data
        self.marquee_label = tk.Label(self.master, text="", bg="black", fg="lime", font=("Courier", 12, "bold"))
        self.marquee_label.grid(row=6, column=0, columnspan=4, sticky="we", pady=5)
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
                    # Minimal validation for Gridcoin.
                    if len(address) >= 10:
                        validated = address
                        valid = True
                    else:
                        valid = False
            except Exception as e:
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
                balance = Decimal("0")
            # Format the balance with 9 decimal places.
            balance_text = f"Balance: {balance:.9f}"
            getattr(self, f"balance_{coin.lower()}").config(text=balance_text)
            logger.debug(f"{coin} balance for {address}: {balance}")
        except Exception as e:
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
        except Exception as e:
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
        
        swapper = Swapper(self.btc_client, self.ltc_client, self.grc_client)
        try:
            result = swapper.start_swap(
                swap_direction=direction,
                btc_address=addresses.get("BTC", ""),
                ltc_address=addresses.get("LTC", ""),
                grc_address=addresses.get("GRC", ""),
                swap_amount=swap_amount
            )
            messagebox.showinfo("Swap Complete", f"Swap {direction} Completed!\n\n{result}")
        except Exception as e:
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

    def get_validated_addresses(self, direction: str) -> Dict[str, str]:
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
        except Exception:
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
    gui = AtomicSwapGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
