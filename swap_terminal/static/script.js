let latestQuote = null
let activeSwapId = null
let pollHandle = null

const quoteOutput = document.getElementById("quote_output")
const swapOutput = document.getElementById("swap_output")
const statusOutput = document.getElementById("status_output")
const marquee = document.getElementById("crypto-marquee")
const createSwapBtn = document.getElementById("create_swap_btn")

function showJson(el, data) {
  el.textContent = JSON.stringify(data, null, 2)
}

async function api(url, options = {}) {
  const response = await fetch(url, options)
  const data = await response.json()
  if (!response.ok) {
    throw new Error(data.error || JSON.stringify(data))
  }
  return data
}

function syncPairOptions() {
  const fromAsset = document.getElementById("from_asset")
  const toAsset = document.getElementById("to_asset")
  const validTargets = {
    GRC: ["BTC", "LTC"],
    BTC: ["GRC"],
    LTC: ["GRC"],
  }
  const currentFrom = fromAsset.value
  const currentTo = toAsset.value
  ;[...toAsset.options].forEach(opt => {
    opt.disabled = !validTargets[currentFrom].includes(opt.value)
  })
  if (![...toAsset.options].some(opt => opt.value === currentTo && !opt.disabled)) {
    toAsset.value = validTargets[currentFrom][0]
  }
}

async function refreshRates() {
  try {
    const data = await api("/api/rates")
    const prices = data.prices
    const pairs = data.pairs
    marquee.textContent = `BTC $${prices.BTC_USD} | LTC $${prices.LTC_USD} | GRC $${prices.GRC_USD} | GRC/BTC ${pairs.GRC_BTC || "-"} | GRC/LTC ${pairs.GRC_LTC || "-"}`
  } catch (err) {
    marquee.textContent = `Rates unavailable: ${err.message}`
  }
}

async function requestQuote() {
  const payload = {
    from_asset: document.getElementById("from_asset").value,
    to_asset: document.getElementById("to_asset").value,
    input_amount: Number(document.getElementById("input_amount").value),
  }
  try {
    latestQuote = await api("/api/quotes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
    createSwapBtn.disabled = false
    showJson(quoteOutput, latestQuote)
  } catch (err) {
    createSwapBtn.disabled = true
    quoteOutput.textContent = err.message
  }
}

async function createSwap() {
  if (!latestQuote) {
    swapOutput.textContent = "Request a quote first."
    return
  }
  const payload = {
    quote_id: latestQuote.id,
    payout_address: document.getElementById("payout_address").value.trim(),
  }
  try {
    const swap = await api("/api/swaps", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
    activeSwapId = swap.id
    document.getElementById("swap_id_lookup").value = swap.id
    showJson(swapOutput, swap)
    await loadSwap(swap.id)
    startPolling(swap.id)
  } catch (err) {
    swapOutput.textContent = err.message
  }
}

async function loadSwap(swapId) {
  try {
    const swap = await api(`/api/swaps/${swapId}`)
    showJson(statusOutput, swap)
  } catch (err) {
    statusOutput.textContent = err.message
  }
}

function startPolling(swapId) {
  if (pollHandle) {
    clearInterval(pollHandle)
  }
  pollHandle = setInterval(async () => {
    await loadSwap(swapId)
  }, 10000)
}

document.getElementById("from_asset").addEventListener("change", syncPairOptions)
document.getElementById("request_quote_btn").addEventListener("click", requestQuote)
document.getElementById("create_swap_btn").addEventListener("click", createSwap)
document.getElementById("lookup_swap_btn").addEventListener("click", async () => {
  const swapId = document.getElementById("swap_id_lookup").value.trim()
  if (!swapId) {
    statusOutput.textContent = "Enter a swap ID."
    return
  }
  activeSwapId = swapId
  await loadSwap(swapId)
  startPolling(swapId)
})
document.getElementById("refresh_rates_btn").addEventListener("click", refreshRates)

syncPairOptions()
refreshRates()
setInterval(refreshRates, 30000)
