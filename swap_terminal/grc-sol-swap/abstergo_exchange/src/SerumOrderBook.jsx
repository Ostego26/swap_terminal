import React, { useEffect, useState } from 'react';
import { Market } from '@project-serum/serum';

const SerumOrderBook = ({ market }) => {
  const [orderBook, setOrderBook] = useState({ bids: [], asks: [] });

  useEffect(() => {
    const fetchOrderBook = async () => {
      if (!market) return;

      try {
        const bids = await market.loadBids();
        const asks = await market.loadAsks();
        setOrderBook({ bids: bids.getL2(5), asks: asks.getL2(5) });
      } catch (err) {
        console.error('Error fetching order book:', err);
      }
    };

    fetchOrderBook();
  }, [market]);

  return (
    <div>
      <h2>Serum Order Book (Top 5)</h2>
      <div>
        <h3>Bids:</h3>
        {orderBook.bids.length ? orderBook.bids.map(([price, size], index) => (
          <div key={index}>Price: {price} | Size: {size}</div>
        )) : <p>No bids available.</p>}
      </div>
      <div>
        <h3>Asks:</h3>
        {orderBook.asks.length ? orderBook.asks.map(([price, size], index) => (
          <div key={index}>Price: {price} | Size: {size}</div>
        )) : <p>No asks available.</p>}
      </div>
    </div>
  );
};

export default SerumOrderBook;
