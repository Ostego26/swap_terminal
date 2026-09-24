import React from 'react';

const PricesDisplay = ({ solanaPrice, gridcoinPrice }) => (
  <div className="container">
    <div className="price-card">
      <h2>Solana (SOL) Price</h2>
      {solanaPrice ? <p>${solanaPrice}</p> : <p>Loading...</p>}
    </div>
    <div className="price-card">
      <h2>Gridcoin (GRC) Price</h2>
      {gridcoinPrice ? <p>${gridcoinPrice}</p> : <p>Loading...</p>}
    </div>
  </div>
);

export default PricesDisplay;
