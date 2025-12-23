import json
import time
import logging
import os
from typing import List, Dict, Optional

log = logging.getLogger(__name__)

class CallLogger:
    def __init__(self, log_file: str = "api_calls.jsonl"):
        self.log_file = log_file
        # Ensure file exists
        if not os.path.exists(self.log_file):
            with open(self.log_file, "w", encoding="utf-8") as f:
                pass

    def log_call(self, credential: str, user_id: str, model: str, latency: float, status_code: int, error: Optional[str] = None):
        """
        Log an API call.
        """
        entry = {
            "timestamp": time.time(),
            "credential": credential,
            "user_id": user_id or "unknown",
            "model": model or "unknown",
            "latency": round(latency, 2),
            "status_code": status_code,
            "error": error
        }
        
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            log.error(f"Failed to write call log: {e}")

    def get_logs(self, limit: int = 100, credential_filter: Optional[str] = None) -> List[Dict]:
        """
        Get recent call logs.
        Filtering by credential if provided.
        """
        logs = []
        try:
            if not os.path.exists(self.log_file):
                return []
                
            # Read from end if file is large? For now, read all.
            # TODO: Implement reading from end for efficiency.
            
            with open(self.log_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                        if credential_filter and entry.get("credential") != credential_filter:
                            continue
                        logs.append(entry)
                    except json.JSONDecodeError:
                        continue
            
            # Sort by timestamp desc
            logs.sort(key=lambda x: x["timestamp"], reverse=True)
            
            return logs[:limit]
            
        except Exception as e:
            log.error(f"Failed to read call logs: {e}")
            return []

# Global Instance
call_logger = CallLogger()
