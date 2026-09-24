import React, { useState } from 'react';
import axios from 'axios';

const SerumTrade = ({ userWalletKeypair, market }) => {
  const [side, setSide] = useState('buy');
  const [price, setPrice] = useState('');
  const [size, setSize] = useState('');
  const [txId, setTxId] = useState('');
  const [error, setError] = useState('');

  const handlePlaceOrder = async () => {
    setError('');
    try {
      const orderParams = { side, price, size, userWalletKeypair, market };
      const tx = await axios.post('http://localhost:5000/place-order', orderParams);  // Backend endpoint for placing orders
      setTxId(`Order placed! Tx Id: ${tx.data.transaction}`);
    } catch (err) {
      setError('Error placing order.');
    }
  };

  return (
    <div>
      <h2>Place Order on Serum</h2>
      <div>
        <label>
          Side:
          <select value={side} onChange={(e) => setSide(e.target.value)}>
            <option value="buy">Buy</option>
            <option value="sell">Sell</option>
          </select>
        </label>
      </div>
      <div>
        <label>
          Price:
          <input type="number" value={price} onChange={(e) => setPrice(e.target.value)} />
        </label>
      </div>
      <div>
        <label>
          Size:
          <input type="number" value={size} onChange={(e) => setSize(e.target.value)} />
        </label>
      </div>
      <button onClick={handlePlaceOrder}>Place Order</button>
      {txId && <p>{txId}</p>}
      {error && <p style={{ color: 'red' }}>{error}</p>}
    </div>
  );
};

export default SerumTrade;
