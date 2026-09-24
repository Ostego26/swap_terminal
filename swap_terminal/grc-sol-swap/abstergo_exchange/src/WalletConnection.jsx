import React, { useState } from 'react';

function WalletConnection({ onConnect }) {
  const [walletAddress, setWalletAddress] = useState('');
  const [walletType, setWalletType] = useState('');
  const [error, setError] = useState('');

  const connectWallet = async (walletType) => {
    try {
      setError('');

      if (walletType === 'Phantom') {
        // Phantom (Solana)
        if (window.solana && window.solana.isPhantom) {
          const response = await window.solana.connect();
          const address = response.publicKey.toString();
          setWalletAddress(address);
          setWalletType('Phantom');
          onConnect(address, response);
        } else {
          throw new Error('Please install the Phantom wallet extension.');
        }
      } 
      
      else if (walletType === 'MetaMask') {
        // MetaMask (EVM)
        if (window.ethereum && window.ethereum.isMetaMask) {
          const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
          const address = accounts[0];
          setWalletAddress(address);
          setWalletType('MetaMask');
          // You can store the entire provider or additional keys if you like
          onConnect(address, { provider: window.ethereum });
        } else {
          throw new Error('Please install the MetaMask extension.');
        }
      } 
      
      else if (walletType === 'Coinbase') {
        // Coinbase Wallet (EVM)
        if (window.ethereum && window.ethereum.isCoinbaseWallet) {
          const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
          const address = accounts[0];
          setWalletAddress(address);
          setWalletType('Coinbase');
          onConnect(address, { provider: window.ethereum });
        } else {
          throw new Error('Please install the Coinbase Wallet extension.');
        }
      } 
      
      else if (walletType === 'Brave') {
        // Brave Wallet (EVM)
        if (window.ethereum && window.ethereum.isBraveWallet) {
          const accounts = await window.ethereum.request({ method: 'eth_requestAccounts' });
          const address = accounts[0];
          setWalletAddress(address);
          setWalletType('Brave');
          onConnect(address, { provider: window.ethereum });
        } else {
          throw new Error('Please enable Brave Wallet in your browser or install a compatible extension.');
        }
      }

    } catch (err) {
      setError(err.message);
    }
  };

  // Disconnect just resets internal state; doesn't remove the wallet’s site connection
  const disconnectWallet = () => {
    setWalletAddress('');
    setWalletType('');
    setError('');
    onConnect(null, null);
  };

  return (
    <div style={styles.container}>
      <h3>Connect a Wallet</h3>
      {!walletAddress && (
        <>
          <button style={styles.button} onClick={() => connectWallet('Phantom')}>
            Phantom
          </button>
          <button style={styles.button} onClick={() => connectWallet('MetaMask')}>
            MetaMask
          </button>
          <button style={styles.button} onClick={() => connectWallet('Coinbase')}>
            Coinbase
          </button>
          <button style={styles.button} onClick={() => connectWallet('Brave')}>
            Brave
          </button>
        </>
      )}

      {/* Show connected wallet info and a disconnect button */}
      {walletAddress && (
        <>
          <p>
            Connected ({walletType}): {walletAddress}
          </p>
          <button style={styles.disconnectButton} onClick={disconnectWallet}>
            Disconnect
          </button>
        </>
      )}

      {/* Error message */}
      {error && <p style={{ color: 'red', marginTop: '10px' }}>{error}</p>}
    </div>
  );
}

export default WalletConnection;

/* 
  Inline styles to give a Solana (purple/teal) + Gridcoin (green) inspired look.
  You can move these into your CSS/SCSS files for more flexibility.
*/
const styles = {
  container: {
    margin: '20px',
    padding: '20px',
    borderRadius: '10px',
    background: 'linear-gradient(135deg, #0e0e0e, #1a1a1a)',
    color: '#ffffff',
    maxWidth: '350px',
    marginLeft: 'auto',
    marginRight: 'auto',
    textAlign: 'center',
  },
  button: {
    background: 'linear-gradient(135deg, #9945FF, #14F195, #8BC34A)',
    backgroundSize: '200% 200%',
    color: '#ffffff',
    fontWeight: 'bold',
    border: 'none',
    borderRadius: '8px',
    padding: '10px 16px',
    margin: '5px',
    cursor: 'pointer',
    fontFamily: 'Roboto, sans-serif',
    transition: 'background-position 0.5s',
    // Hover effect:
    ':hover': {
      backgroundPosition: 'right center',
    },
  },
  disconnectButton: {
    background: '#8BC34A',
    color: '#0e0e0e',
    fontWeight: 'bold',
    border: 'none',
    borderRadius: '8px',
    padding: '10px 16px',
    margin: '5px',
    cursor: 'pointer',
    fontFamily: 'Roboto, sans-serif',
  },
};
