// Backend process for handling Gridcoin deposit and wrapping to wGRC
import express from 'express';
import { Connection, PublicKey, Keypair, Transaction, SystemProgram } from '@solana/web3.js';
import axios from 'axios';
import dotenv from 'dotenv';
import cors from 'cors';

// Load environment variables
dotenv.config();

// Setup express app
const app = express();
const port = process.env.PORT || 5000;
const solanaConnection = new Connection(process.env.VITE_DEVNET_RPC_URL, 'confirmed');
app.use(cors());
app.use(express.json()); // Allow POST requests to be parsed as JSON

// API Endpoint to handle Gridcoin deposit
app.post('/deposit', async (req, res) => {
  const { amount } = req.body;

  if (!amount || isNaN(amount) || parseFloat(amount) <= 0) {
    return res.status(400).json({ error: 'Invalid amount provided' });
  }

  try {
    console.log(`Depositing ${amount} GRC to exchange wallet...`);
    const depositResult = await handleGridcoinSwap(amount, process.env.EXCHANGE_WALLET_ADDRESS);

    if (depositResult) {
      console.log(`Deposit successful for ${amount} GRC.`);

      // Wrap the GRC into wGRC
      await wrapGrcToWgrc(amount, process.env.EXCHANGE_WALLET_ADDRESS);
      return res.json({ success: true, message: `Successfully deposited ${amount} GRC.` });
    } else {
      return res.status(400).json({ error: 'Deposit failed' });
    }
  } catch (error) {
    console.error('Error handling deposit:', error);
    return res.status(500).json({ error: 'Internal Server Error' });
  }
});

// Handle Gridcoin deposit transfer to exchange wallet
async function handleGridcoinSwap(grcAmount, exchangeWalletAddress) {
  try {
    const response = await axios.post(process.env.GRIDCOIN_RPC_URL, {
      jsonrpc: '2.0',
      method: 'sendtoaddress',
      params: [exchangeWalletAddress, grcAmount], // Send Gridcoin to the exchange wallet
      id: 1
    });

    if (response.data.error) {
      console.error('Gridcoin RPC error:', response.data.error);
      throw new Error('Error during Gridcoin transfer');
    }

    return response.data.result;
  } catch (error) {
    console.error('Error during Gridcoin transfer:', error);
    throw new Error('Gridcoin RPC failed');
  }
}

// Wrap GRC into wGRC logic (implement this part with smart contract)
async function wrapGrcToWgrc(amount, userAddress) {
  console.log(`Wrapping ${amount} GRC into wGRC for user: ${userAddress}`);
  // Logic for wrapping GRC into wGRC (minting the wGRC token) goes here
  // You should mint the wrapped token here and confirm the transaction
}

app.listen(port, () => {
  console.log(`Server running at http://localhost:${port}`);
});
