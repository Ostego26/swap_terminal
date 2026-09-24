const { PublicKey } = require('@solana/web3.js');

const address = "Eypf7jY7pocut9A8hEoJPmkFpXSRDjtNYozo2jKEgs2H";  // Replace with your address

try {
  const publicKey = new PublicKey(address);
  console.log(`Valid PublicKey: ${publicKey.toBase58()}`);
} catch (error) {
  console.error("Invalid PublicKey format:", address);
}
