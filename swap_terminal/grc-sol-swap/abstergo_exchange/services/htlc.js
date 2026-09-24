// src/components/HTLC.js

import { useState } from 'react';
import { PublicKey } from '@solana/web3.js';
import { Program, web3 } from '@project-serum/anchor';
import { handleGridcoinSwap } from '../utils/gridcoin'; // Import the Gridcoin swap function

const connection = new web3.Connection('https://api.devnet.solana.com');
const provider = new Program.Provider(connection, useWallet(), {});
const programId = new PublicKey('YourSolanaProgramID'); // Replace with your deployed program ID
const program = new Program(idl, programId, provider);

function HTLC() {
  const [grcAmount, setGrcAmount] = useState('');
  const [receiverAddress, setReceiverAddress] = useState('');
  const [secret, setSecret] = useState('');
  const [timelock, setTimelock] = useState('');
  const [txStatus, setTxStatus] = useState('');

  const handleSubmit = async () => {
    try {
      const secretHash = new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(secret)));
      
      // Create the HTLC contract on Solana
      await program.rpc.createHtlc(secretHash, new web3.BN(timelock), {
        accounts: {
          sender: provider.wallet.publicKey,
          receiver: new PublicKey(receiverAddress),
        },
      });

      setTxStatus('HTLC contract created successfully');
      
      // Call Gridcoin swap if needed
      const result = await handleGridcoinSwap(grcAmount, receiverAddress);
      console.log('Gridcoin transfer result:', result);
    } catch (err) {
      console.error('Error creating HTLC:', err);
      setTxStatus('Error creating HTLC');
    }
  };

  return (
    <div>
      <input type="text" placeholder="Enter GRC Amount" onChange={(e) => setGrcAmount(e.target.value)} />
      <input type="text" placeholder="Receiver Address" onChange={(e) => setReceiverAddress(e.target.value)} />
      <input type="text" placeholder="Enter Secret" onChange={(e) => setSecret(e.target.value)} />
      <input type="number" placeholder="Timelock in seconds" onChange={(e) => setTimelock(e.target.value)} />
      <button onClick={handleSubmit}>Create HTLC</button>
      <p>{txStatus}</p>
    </div>
  );
}

export default HTLC;
