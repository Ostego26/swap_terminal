import React, { useState, useEffect } from 'react';
import axios from 'axios';

const GridcoinDeposit = ({ onDepositConfirmed }) => {
  const [depositAmount, setDepositAmount] = useState('');
  const [exchangeWalletAddress, setExchangeWalletAddress] = useState('');
  const [error, setError] = useState('');
  const [depositSuccess, setDepositSuccess] = useState('');

  useEffect(() => {
    const fetchExchangeWallet = async () => {
      try {
        setError('');
        const response = await axios.get('http://localhost:5000/get-exchange-wallet');
        setExchangeWalletAddress(
          response.data.exchangeWalletAddress ||
          response.data.address ||
          ''
        );
      } catch {
        setError('Error fetching exchange wallet address');
      }
    };

    fetchExchangeWallet();
  }, []);

  const handleDeposit = async () => {
    setError('');
    setDepositSuccess('');

    if (!depositAmount || isNaN(depositAmount) || parseFloat(depositAmount) <= 0) {
      setError('Please enter a valid deposit amount.');
      return;
    }

    try {
      const response = await axios.post('http://localhost:5000/deposit', {
        amount: depositAmount,
      });

      if (response.data.success) {
        setDepositSuccess(`Successfully submitted deposit request for ${depositAmount} GRC.`);
        if (onDepositConfirmed) onDepositConfirmed(depositAmount);
      } else {
        setError(response.data.error || 'Deposit failed.');
      }
    } catch {
      setError('Error processing deposit. Please try again.');
    }
  };

  return (
    <div>
      <h2>Deposit Gridcoin</h2>
      <input
        type="number"
        value={depositAmount}
        onChange={(e) => setDepositAmount(e.target.value)}
        placeholder="Enter amount to deposit"
      />
      <button onClick={handleDeposit}>Deposit</button>
      <p>Send your deposit to: {exchangeWalletAddress || 'Loading...'}</p>
      {depositSuccess && <p style={{ color: 'green' }}>{depositSuccess}</p>}
      {error && <p style={{ color: 'red' }}>{error}</p>}
    </div>
  );
};

export default GridcoinDeposit;
