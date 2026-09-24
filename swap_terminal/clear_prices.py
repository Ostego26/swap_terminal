import json

path = "/home/mpjones26/Documents/Prototypes/transactions.json"

with open(path, "r", encoding="utf-8") as f:
    txs = json.load(f)

for tx in txs:
    tx.pop("Amount(USD)", None)
    tx.pop("PricePerGRC_USD", None)
    tx.pop("PriceSource", None)
    tx.pop("PriceSourceDate", None)

with open(path, "w", encoding="utf-8") as f:
    json.dump(txs, f, indent=2)

print(f"Cleared cached price fields on {len(txs)} transactions.")
