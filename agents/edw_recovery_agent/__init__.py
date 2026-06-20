"""ADK agent package — app name = ``edw_recovery_agent``.

`adk web agents` / `adk run agents/edw_recovery_agent` discover this package and
read `agent.root_agent`. Core utilities (auth, data client, slack, recovery,
settings) live in ``src/``; only the agent definition lives here.

ADK's CLI loader puts the ``agents/`` dir on ``sys.path`` (not the repo root),
so ``import src`` would fail. We prepend the repo root here so the package loads
the same way under ``adk run``/``adk web``, ``uvicorn main:app``, and Docker.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from .agent import control_center, root_agent

__all__ = ["control_center", "root_agent"]
