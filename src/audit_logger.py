import json
import time
import logging
import os
from typing import List, Dict, Optional

log = logging.getLogger(__name__)

class AuditLogger:
    def __init__(self, log_file: str = "audit.jsonl"):
        self.log_file = log_file
        # Ensure file exists
        if not os.path.exists(self.log_file):
            with open(self.log_file, "w", encoding="utf-8") as f:
                pass

    def log_event(self, action: str, user_id: str, details: Optional[Dict] = None, ip: Optional[str] = None):
        """
        Log an audit event.
        
        Args:
            action: The action performed (e.g., "login", "upload_credential", "delete_user").
            user_id: The ID of the user performing the action.
            details: Additional context (e.g., filename, target_user).
            ip: IP address of the user.
        """
        entry = {
            "timestamp": time.time(),
            "action": action,
            "user_id": user_id,
            "ip": ip or "unknown",
            "details": details or {}
        }
        
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"Failed to write audit log: {e}")

    def get_logs(
        self, 
        page: int = 1, 
        page_size: int = 100, 
        action_filter: Optional[str] = None, 
        user_id_filter: Optional[str] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None
    ) -> Dict[str, object]:
        """
        Get recent audit logs with filtering and pagination.
        Returns: {"total": int, "items": List[Dict]}
        """
        logs = []
        try:
            if not os.path.exists(self.log_file):
                return {"total": 0, "items": []}
                
            with open(self.log_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        logs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            
            # --- Filtering ---
            if action_filter:
                logs = [l for l in logs if l.get("action") == action_filter]
            
            if user_id_filter:
                logs = [l for l in logs if user_id_filter in l.get("user_id", "")]
                
            if start_time is not None:
                logs = [l for l in logs if l.get("timestamp", 0) >= start_time]
                
            if end_time is not None:
                logs = [l for l in logs if l.get("timestamp", 0) <= end_time]
                
            # --- Sorting & Pagination ---
            # Sort by timestamp desc (newest first)
            logs.sort(key=lambda x: x["timestamp"], reverse=True)
            
            total_count = len(logs)
            
            # Pagination
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            
            paginated_logs = logs[start_idx:end_idx]
            
            return {
                "total": total_count,
                "items": paginated_logs,
                "page": page,
                "page_size": page_size
            }
            
        except Exception as e:
            log.error(f"Failed to read audit logs: {e}")
            return {"total": 0, "items": []}

# Global Instance
audit_logger = AuditLogger()
