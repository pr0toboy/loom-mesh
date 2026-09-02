"""Test configuration for the autonomy engine.

``fable_gate`` knows no agent of its own: its review tiers come from the
environment (``LOOM_FABLE_AGENTS``, ``LOOM_SPECIALIST_REVIEWER``,
``LOOM_GENERAL_REVIEWER``) with EMPTY defaults — a published repository must not
ship the composition of a real fleet.

Those values are read **at import time** and then serve as default argument
values (``def is_fable(agent, fable_agents=DEFAULT_FABLE_AGENTS)``): once the
function is defined, a ``monkeypatch.setattr`` on the constant changes nothing.
So the environment has to be set **before** pytest imports the test modules —
which is exactly what a ``conftest.py`` does, being loaded first.

The test fleet below is fictional and bears no relation to any deployment.
"""
import os

os.environ.setdefault("LOOM_FABLE_AGENTS", "carol,erin")
os.environ.setdefault("LOOM_SPECIALIST_REVIEWER", "erin")   # scopes jeu / PMD
os.environ.setdefault("LOOM_GENERAL_REVIEWER", "carol")     # everything else
