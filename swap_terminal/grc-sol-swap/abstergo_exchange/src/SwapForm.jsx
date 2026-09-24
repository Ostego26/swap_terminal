import React, { useState } from 'react';
import axios from 'axios';

function SwapForm() {
  const [grcAmount, setGrcAmount] = useState('');
  const [solAmount, setSolAmount] = useState('');
  const [userAddress, setUserAddress] = useState('');
  const [swapType, setSwapType] = useState('grc-to-sol');
  const [transactionStatus, setTransactionStatus] = useState('');
  const [error, setError] = useState('');

  const handleSwap = async () => {
    setTransactionStatus('');
    setError('');

    if (!grcAmount || !userAddress) {
      setError('Please enter both the amount and your wallet address.');
      return;
    }

    try {
      const endpoint = swapType === 'grc-to-sol' ? '/swap/grc-to-sol' : '/swap/sol-to-grc';
      const response = await axios.post(`http://localhost:5000${endpoint}`, {
        grcAmount,
        solAmount,
        userAddress,
      });

      const resultAmount = swapType === 'grc-to-sol' ? response.data.solAmount : response.data.grcAmount;
      setTransactionStatus(`Swap successful! Amount: ${resultAmount}`);
    } catch (error) {
      console.error('Swap failed:', error);
      setError('Swap failed. Please try again.');
    }
  };

  return (
    <div>
      <h3>Swap Form</h3>
      <div>
        <label>
          Swap Type:
          <select value={swapType} onChange={(e) => setSwapType(e.target.value)}>
            <option value="grc-to-sol">GRC to SOL</option>
            <option value="sol-to-grc">SOL to GRC</option>
          </select>
        </label>
      </div>
      <div>
        <label>
          Amount:
          <input
            type="number"
            value={grcAmount || solAmount}
            onChange={(e) => swapType === 'grc-to-sol' ? setGrcAmount(e.target.value) : setSolAmount(e.target.value)}
          />
        </label>
      </div>
      <div>
        <label>
          User Wallet Address:
          <input
            type="text"
            value={userAddress}
            onChange={(e) => setUserAddress(e.target.value)}
          />
        </label>
      </div>
      <button onClick={handleSwap}>Swap</button>
      {transactionStatus && <p>{transactionStatus}</p>}
      {error && <p style={{ color: 'red' }}>{error}</p>}
    </div>
  );
}

export default SwapForm;
