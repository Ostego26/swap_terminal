#!/usr/bin/env python3
"""
File: rpc_clients.py

Description:
  A generic RPC client for making JSON-RPC calls to a daemon (Bitcoin, Litecoin, Gridcoin).
"""

import requests
import json
import os
import logging
from typing import Any, List, Optional, Union
from dotenv import load_dotenv

# Load environment variables from the .env file
load_dotenv()

# Configure module logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    ch.setFormatter(formatter)
    logger.addHandler(ch)


class RPCClient:
    def __init__(self, rpc_user: str, rpc_password: str, rpc_host: str, rpc_port: Union[str, int], wallet: Optional[str] = None) -> None:
        """
        Initialize the generic RPC client with provided credentials.

        Args:
            rpc_user (str): RPC username.
            rpc_password (str): RPC password.
            rpc_host (str): Host for the RPC server.
            rpc_port (Union[str, int]): Port for the RPC server.
            wallet (Optional[str]): Optional wallet name to include in the URL.
        """
        # Fetching RPC configurations from environment variables
        if wallet:
            self.url = f"http://{rpc_user}:{rpc_password}@{rpc_host}:{rpc_port}/wallet/{wallet}"
        else:
            self.url = f"http://{rpc_user}:{rpc_password}@{rpc_host}:{rpc_port}"

        logger.debug(f"Initialized RPCClient with URL: {self.url}")

    def call(self, method: str, *params: Any) -> Any:
        """
        Make a JSON-RPC call to the daemon.

        Args:
            method (str): The RPC method name.
            *params (Any): Positional parameters for the RPC call.

        Returns:
            Any: The 'result' field from the JSON-RPC response.

        Raises:
            Exception: If the RPC call returns an error or fails.
        """
        payload = {
            "jsonrpc": "1.0",
            "id": "abstergo",
            "method": method,
            "params": list(params)
        }
        headers = {"content-type": "application/json"}

        # Log the request payload and headers
        logger.debug(f"Making RPC call with method: {method}")
        logger.debug(f"Request payload: {json.dumps(payload)}")
        logger.debug(f"Headers: {headers}")

        try:
            # Perform the RPC request
            response = requests.post(self.url, data=json.dumps(payload), headers=headers, timeout=30)

            # Log the response status and text
            logger.debug(f"RPC response status code: {response.status_code}")
            logger.debug(f"RPC response text: {response.text}")
            response.raise_for_status()  # Check for HTTP errors

            # Parse the JSON response
            data = response.json()

            # Log the full response from the server
            logger.debug(f"RPC response JSON: {json.dumps(data)}")

            # Check if there's an error in the response
            if data.get("error"):
                logger.error(f"RPC Error: {data['error']}")
                raise Exception(f"RPC Error: {data['error']}")

            # Return the result from the response
            logger.debug(f"RPC call result: {data['result']}")
            return data["result"]

        except requests.exceptions.RequestException as e:
            logger.exception(f"RPC call failed: {e}")
            raise Exception(f"RPC call failed: {e}")
        except Exception as e:
            logger.exception(f"An error occurred during the RPC call: {e}")
            raise


# Example: Initialize the RPC clients for Bitcoin, Litecoin, and Gridcoin using environment variables

# Bitcoin Client
btc_client = RPCClient(
    rpc_user=os.getenv("BTC_RPC_USER"),
    rpc_password=os.getenv("BTC_RPC_PASS"),
    rpc_host=os.getenv("BTC_RPC_URL"),
    rpc_port=18332,  # Typically the testnet port for Bitcoin
    wallet=os.getenv("BTC_RPC_WALLET")
)

# Litecoin Client
ltc_client = RPCClient(
    rpc_user=os.getenv("LTC_RPC_USER"),
    rpc_password=os.getenv("LTC_RPC_PASS"),
    rpc_host=os.getenv("LTC_RPC_URL"),
    rpc_port=19332,  # Testnet port for Litecoin
)

# Gridcoin Client
grc_client = RPCClient(
    rpc_user=os.getenv("GRC_RPC_USER"),
    rpc_password=os.getenv("GRC_RPC_PASS"),
    rpc_host=os.getenv("GRC_RPC_URL"),
    rpc_port=25779,  # Testnet port for Gridcoin
)

# Example of testing the RPC client with verbose logging:
if __name__ == "__main__":
    try:
        # Test the RPC call with a method (replace with valid method and params)
        result = btc_client.call("getblockchaininfo")
        print(f"Result from getblockchaininfo: {result}")

        result = ltc_client.call("getblockchaininfo")
        print(f"Result from getblockchaininfo: {result}")

        result = grc_client.call("getblockchaininfo")
        print(f"Result from getblockchaininfo: {result}")

    except Exception as e:
        logger.error(f"Error during RPC test: {e}")
