"""Package marker for the swap_terminal application.

Role: package marker (no code)
Reads: nothing
Writes: nothing
Can move funds: no
Mainnet-safe: yes

Deliberately empty. The application imports its own modules rootlessly --
`from config import Config`, not `from swap_terminal.config import Config` --
so this file exists to make the directory a package for tooling, not to be
imported. Putting anything here would make it an import-time side effect for
every consumer (rule 12).
"""
