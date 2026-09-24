import axios from 'axios';

// Fetch prices from CoinGecko
export async function fetchPricesFromAPI(API_URL) {
  try {
    console.log('Making API call to fetch prices from CoinGecko...');
    const response = await axios.get(API_URL);
    console.log('Prices fetched from CoinGecko:', response.data);
    return response.data;
  } catch (error) {
    console.error('ERROR: Failed to fetch prices from CoinGecko:', error);
    throw new Error('Failed to fetch prices from CoinGecko');
  }
}
