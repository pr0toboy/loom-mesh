"""Roster for the docker demo: two agents, no human facade.

A real deployment generates this from its own registry (or gets `peers.py` from
bootstrap.sh). The demo ships one so the bus has something to validate against —
written here as a file rather than echoed from the compose file, where quoting
silently ate the quotes and produced a roster that raised NameError on import,
leaving every peer unknown.
"""
INBOX_PEERS = ("alice", "bob")
FACADE_PEERS = ()
SEND_PEERS = INBOX_PEERS + FACADE_PEERS
