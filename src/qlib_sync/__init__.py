"""Safely install and update a local Qlib China A-share dataset."""

import os

# Recent mlflow (a Qlib dependency) logs an AI-agent hint on import; it is noise for our users.
os.environ.setdefault("MLFLOW_DISABLE_AGENT_HINT", "1")

from .api import health_check_cn, status_cn, update_cn
from .cn.update import UpdateError, UpdateResult

__version__ = "0.1.0"

__all__ = ["UpdateError", "UpdateResult", "health_check_cn", "status_cn", "update_cn", "__version__"]
