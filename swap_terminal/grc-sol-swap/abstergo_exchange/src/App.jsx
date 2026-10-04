/*
 * The swap-desk screen: create an intent, watch it, and work the operator queue.
 *
 * THE SERUM MARKET REFERENCE WAS REMOVED HERE ON 2026-10-04, with the rest of
 * the Serum/wGRC order-book work (see the directory's git history for
 * createMarket.js, createMarketdev.js, scripts/createMarket.js, testMarket.js,
 * src/serumFunctions.jsx, src/SerumOrderBook.jsx, src/SerumTrade.jsx and
 * services/solana.js). What this file carried was:
 *
 *   import { Market } from '@project-serum/serum';
 *   const PROGRAM_ID     = import.meta.env.VITE_SERUM_DEX_PROGRAM_ID;
 *   const MARKET_ADDRESS = import.meta.env.VITE_SERUM_MARKET_ADDRESS;
 *   ... Market.load(connection, marketAddress, {}, marketProgramId)
 *   ... {view === 'market' && <MarketReference prices={prices} market={market} />}
 *
 * WHY IT WENT, and the reason is not that the order book was unfinished. A
 * matching venue is orthogonal to hashlocked settlement: it does not help the
 * Solana atomicity problem, and settling a swap THROUGH it would require wGRC,
 * which reintroduces custodial trust at the wrapping step and adds a mint
 * authority as a new single point of failure. That mint authority was wgrc.json,
 * the keypair this repository published.
 *
 * THE `market` STATE WAS ALREADY UNREACHABLE, which is why removing it is a cull
 * and not a feature decision. `MarketReference` has no definition anywhere in
 * the tree -- grepped by NAME across every file type on 2026-10-04, not by the
 * import graph (rule 2) -- so `view === 'market'` could never render, and
 * Market.load() fed nothing but that branch. The nav button that set it is gone
 * with it, along with `.market-reference__book` in App.css, which was its only
 * style rule and had no other user.
 *
 * STILL BROKEN, AND DELIBERATELY NOT FIXED HERE (rule 17: this is measured, not
 * assumed -- the three names below have no definition anywhere in the tree
 * either, by the same NAME grep). `./IntentForm`, `./IntentStatusCard` and
 * `./OperatorQueue` do not resolve, so `vite build` cannot succeed on this file
 * regardless of the Serum removal. `./IntentForm` is the nearest to existing:
 * SwapIntentForm.jsx one directory up exports `default function IntentForm`, so
 * the component exists at the wrong path. Supplying the other two is writing a
 * frontend, not culling one, and it is outside what this pass was authorized to
 * do.
 *
 * Role: submodule (React component, browser only)
 * Reads: GET /prices on the Express server at API_BASE
 * Writes: nothing -- the child components post, this one does not
 * Can move funds: no
 * Mainnet-safe: yes, it makes no chain call at all now
 */
import { useEffect, useState } from 'react';
import IntentForm from './IntentForm';
import IntentStatusCard from './IntentStatusCard';
import OperatorQueue from './OperatorQueue';
import './App.css';

const API_BASE = 'http://localhost:5000';

export default function App() {
  const [prices, setPrices] = useState({ solanaPrice: null, gridcoinPrice: null });
  const [currentIntent, setCurrentIntent] = useState(null);
  const [view, setView] = useState('desk');
  const [messages, setMessages] = useState({ error: '', info: '' });

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
        </nav>

        <div className="sidebar-card">
          <p className="eyebrow">Live notes</p>
          <ul className="bullet-list">
            <li>No direct order-book trading from the customer screen.</li>
            <li>Gridcoin deposit must confirm before payout is approved.</li>
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
        </div>
      </main>
    </div>
  );
}
