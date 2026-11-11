"""Safely install and update a local Qlib China A-share dataset."""

from .api import health_check_cn, status_cn, update_cn
from .cn.update import UpdateError, UpdateResult

__version__ = "0.1.0"

__all__ = ["UpdateError", "UpdateResult", "health_check_cn", "status_cn", "update_cn", "__version__"]
