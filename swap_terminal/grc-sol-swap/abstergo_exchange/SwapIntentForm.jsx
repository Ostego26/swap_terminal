import React, { useMemo, useState } from 'react';

const initialState = {
  sourceAmount: '',
  destinationAddress: '',
  destinationAsset: 'SOL',
  notes: '',
};

function formatQuote(sourceAmount, grcPrice, solPrice) {
  const grc = Number(sourceAmount);
  const grcUsd = Number(grcPrice);
  const solUsd = Number(solPrice);
  if (!Number.isFinite(grc) || grc <= 0 || !Number.isFinite(grcUsd) || !Number.isFinite(solUsd) || solUsd <= 0) {
    return null;
  }
  const grossSol = (grc * grcUsd) / solUsd;
  const feeRate = 0.01;
  const netSol = grossSol * (1 - feeRate);
  return {
    grossSol,
    feeRate,
    netSol,
  };
}

export default function IntentForm({ prices, onIntentCreated }) {
  const [form, setForm] = useState(initialState);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState('');

  const quote = useMemo(
    () => formatQuote(form.sourceAmount, prices.gridcoinPrice, prices.solanaPrice),
    [form.sourceAmount, prices.gridcoinPrice, prices.solanaPrice],
  );

  const updateField = (field) => (event) => {
    setForm((current) => ({ ...current, [field]: event.target.value }));
  };

  const resetForm = () => {
    setForm(initialState);
    setError('');
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    setError('');

    if (!form.sourceAmount || Number(form.sourceAmount) <= 0) {
      setError('Enter a valid Gridcoin amount.');
      return;
    }

    if (!form.destinationAddress.trim()) {
      setError('Enter the destination Solana address.');
      return;
    }

    setIsSubmitting(true);
    try {
      const response = await fetch('http://localhost:5000/swap-intents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          sourceAsset: 'GRC',
          sourceAmount: Number(form.sourceAmount),
          destinationAsset: form.destinationAsset,
          destinationAddress: form.destinationAddress.trim(),
          notes: form.notes.trim(),
        }),
      });

      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload.intent) {
        throw new Error(payload.error || 'Could not create swap intent.');
      }

      onIntentCreated(payload.intent);
      resetForm();
    } catch (submitError) {
      setError(submitError.message || 'Could not create swap intent.');
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <section className="panel panel--wide">
      <div className="panel__header">
        <div>
          <p className="eyebrow">Step 1</p>
          <h2>Create swap intent</h2>
        </div>
        <span className="chip chip--accent">Custodial desk</span>
      </div>

      <form className="intent-form" onSubmit={handleSubmit}>
        <label>
          Gridcoin amount
          <input
            type="number"
            min="0"
            step="any"
            value={form.sourceAmount}
            onChange={updateField('sourceAmount')}
            placeholder="50000"
          />
        </label>

        <label>
          Destination asset
          <select value={form.destinationAsset} onChange={updateField('destinationAsset')}>
            <option value="SOL">SOL</option>
            <option value="wGRC">wGRC</option>
          </select>
        </label>

        <label className="intent-form__full">
          Destination Solana address
          <input
            type="text"
            value={form.destinationAddress}
            onChange={updateField('destinationAddress')}
            placeholder="Enter the recipient Solana address"
          />
        </label>

        <label className="intent-form__full">
          Notes for operator
          <textarea
            rows="3"
            value={form.notes}
            onChange={updateField('notes')}
            placeholder="Optional reference or routing note"
          />
        </label>

        <div className="quote-box intent-form__full">
          <div>
            <p className="quote-box__label">Indicative output</p>
            <strong>{quote ? `${quote.netSol.toFixed(6)} ${form.destinationAsset}` : '—'}</strong>
          </div>
          <div>
            <p className="quote-box__label">Desk fee</p>
            <strong>{quote ? `${(quote.feeRate * 100).toFixed(2)}%` : '—'}</strong>
          </div>
          <div>
            <p className="quote-box__label">Status</p>
            <strong>Quote at submission</strong>
          </div>
        </div>

        {error && <p className="message message--error">{error}</p>}

        <div className="intent-form__actions intent-form__full">
          <button type="submit" className="button button--primary" disabled={isSubmitting}>
            {isSubmitting ? 'Creating…' : 'Create intent'}
          </button>
          <button type="button" className="button button--ghost" onClick={resetForm} disabled={isSubmitting}>
            Clear
          </button>
        </div>
      </form>
    </section>
  );
}
