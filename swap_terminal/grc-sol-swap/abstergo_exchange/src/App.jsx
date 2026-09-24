import { useEffect, useMemo, useState } from 'react';
import { Connection, PublicKey } from '@solana/web3.js';
import { Market } from '@project-serum/serum';
import IntentForm from './IntentForm';
import IntentStatusCard from './IntentStatusCard';
import OperatorQueue from './OperatorQueue';
import MarketReference from './MarketReference';
import './App.css';

const API_BASE = 'http://localhost:5000';
const RPC_URL = import.meta.env.VITE_DEVNET_RPC_URL;
const PROGRAM_ID = import.meta.env.VITE_SERUM_DEX_PROGRAM_ID;
const MARKET_ADDRESS = import.meta.env.VITE_SERUM_MARKET_ADDRESS;

const connection = RPC_URL ? new Connection(RPC_URL, 'confirmed') : null;

function safePublicKey(value) {
  try {
    return value ? new PublicKey(value) : null;
  } catch {
    return null;
  }
}

export default function App() {
  const [prices, setPrices] = useState({ solanaPrice: null, gridcoinPrice: null });
  const [market, setMarket] = useState(null);
  const [currentIntent, setCurrentIntent] = useState(null);
  const [view, setView] = useState('desk');
  const [messages, setMessages] = useState({ error: '', info: '' });

  const marketProgramId = useMemo(() => safePublicKey(PROGRAM_ID), []);
  const marketAddress = useMemo(() => safePublicKey(MARKET_ADDRESS), []);

  useEffect(() => {
    const fetchPrices = async () => {
      try {
        const response = await fetch(`${API_BASE}/prices`);
        const data = await response.json();
        setPrices({
          solanaPrice: data.solana?.usd ?? null,
          gridcoinPrice: data['gridcoin-research']?.usd ?? null,
        });
      } catch {
        setMessages((current) => ({ ...current, error: 'Could not load price data from the desk backend.' }));
      }
    };
    fetchPrices();
  }, []);

  useEffect(() => {
    const fetchMarket = async () => {
      if (!connection || !marketProgramId || !marketAddress) return;
      try {
        const loadedMarket = await Market.load(connection, marketAddress, {}, marketProgramId);
        setMarket(loadedMarket);
      } catch {
        setMessages((current) => ({ ...current, info: 'Market reference is unavailable right now.' }));
      }
    };
    fetchMarket();
  }, [marketProgramId, marketAddress]);

  const handleIntentCreated = (intent) => {
    setCurrentIntent(intent);
    setView('status');
    setMessages({ error: '', info: 'Swap intent created. Send the exact GRC amount to the deposit address below.' });
  };

  const handleIntentUpdated = (intent) => {
    setCurrentIntent(intent);
  };

  return (
    <div className="desk-shell">
      <aside className="desk-sidebar">
        <div className="brand-block">
          <p className="brand-block__kicker">Rolice</p>
          <h1>GRC ⇄ SOL swap desk</h1>
          <p className="muted">
            Custodial flow: create intent, deposit Gridcoin, wait for confirmations, then receive payout.
          </p>
        </div>

        <nav className="nav-stack">
          <button type="button" className={`nav-link${view === 'desk' ? ' nav-link--active' : ''}`} onClick={() => setView('desk')}>
            Create intent
          </button>
          <button type="button" className={`nav-link${view === 'status' ? ' nav-link--active' : ''}`} onClick={() => setView('status')}>
            Intent status
          </button>
          <button type="button" className={`nav-link${view === 'operator' ? ' nav-link--active' : ''}`} onClick={() => setView('operator')}>
            Operator queue
          </button>
          <button type="button" className={`nav-link${view === 'market' ? ' nav-link--active' : ''}`} onClick={() => setView('market')}>
            Market reference
          </button>
        </nav>

        <div className="sidebar-card">
          <p className="eyebrow">Live notes</p>
          <ul className="bullet-list">
            <li>No direct order-book trading from the customer screen.</li>
            <li>Gridcoin deposit must confirm before payout is approved.</li>
            <li>Operator can use the market as a hedge or reference.</li>
          </ul>
        </div>
      </aside>

      <main className="desk-main">
        <div className="page-topbar">
          <div>
            <p className="eyebrow">Workflow</p>
            <h2>Intent → deposit → confirm → payout</h2>
          </div>
          {currentIntent?.intent_id && (
            <div className="topbar-intent">
              <span className="muted">Active intent</span>
              <strong>{currentIntent.intent_id}</strong>
            </div>
          )}
        </div>

        {messages.error && <p className="message message--error">{messages.error}</p>}
        {messages.info && <p className="message message--info">{messages.info}</p>}

        <div className="page-grid">
          {(view === 'desk' || view === 'status') && (
            <div className="stack">
              <IntentForm prices={prices} onIntentCreated={handleIntentCreated} />
              <IntentStatusCard intent={currentIntent} onRefresh={handleIntentUpdated} />
            </div>
          )}

          {view === 'operator' && (
            <OperatorQueue
              selectedIntentId={currentIntent?.intent_id}
              onSelectIntent={setCurrentIntent}
              onIntentUpdated={handleIntentUpdated}
            />
          )}

          {view === 'market' && <MarketReference prices={prices} market={market} />}
        </div>
      </main>
    </div>
  );
}
