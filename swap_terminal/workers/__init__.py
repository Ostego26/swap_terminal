"""Package marker for the polling workers.

Role: package marker (no code)
Reads: nothing
Writes: nothing
Can move funds: no -- but workers/payout_worker.py does.
Mainnet-safe: yes

Deliberately empty. The reaper for every worker in this package is
swap_terminal/supervisor.py (rule 13).
"""
