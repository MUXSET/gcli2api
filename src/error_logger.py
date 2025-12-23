import time
import logging
from collections import deque
from typing import List, Dict, Optional

log = logging.getLogger(__name__)

class ErrorLogManager:
    def __init__(self, max_len: int = 100):
        # Thread-safe deque with max length
        self._logs = deque(maxlen=max_len)

    def log_error(self, credential: str, user_id: str, status_code: int, error_msg: str):
        """
        Log an API error.
        
        Args:
            credential: The credential filename involved.
            user_id: The ID of the user who made the request (or 'System'/'Global').
            status_code: HTTP status code or 0 for internal exceptions.
            error_msg: Brief error description or response body snippet.
        """
        entry = {
            "timestamp": time.time(),
            "credential": credential,
            "user_id": user_id or "Unknown",
            "status_code": status_code,
            "error_msg": str(error_msg)[:500]  # Truncate to avoid huge logs
        }
        self._logs.append(entry)
        # log.debug(f"Recorded error log: {entry}")

    def get_recent_errors(self, limit: int = 50) -> List[Dict]:
        """Get recent error logs, newest first."""
        # Convert to list and reverse to show newest first
        all_logs = list(self._logs)
        return sorted(all_logs, key=lambda x: x["timestamp"], reverse=True)[:limit]

    def clear_logs(self):
        self._logs.clear()

# Global Instance
error_logger = ErrorLogManager()
