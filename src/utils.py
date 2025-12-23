import os
import json
import random
from typing import Optional

def get_user_filename(user_id: str, filename: str) -> str:
    """生成带用户前缀的文件名"""
    prefix = f"u_{user_id}_"
    if filename.startswith(prefix):
        return filename
    return f"{prefix}{filename}"

def strip_user_prefix(user_id: str, filename: str) -> str:
    """移除用户前缀，展示给前端"""
    prefix = f"u_{user_id}_"
    if filename.startswith(prefix):
        return filename[len(prefix):]
    return filename

def get_user_agent() -> str:
    """随机获取一个User-Agent"""
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    ]
    return random.choice(user_agents)

def parse_quota_reset_timestamp(error_data: dict) -> Optional[float]:
    """
    尝试从错误响应中解析配额重置时间
    返回: UTC时间戳 (seconds) 或 None
    """
    try:
        # Pydantic/JSON structure analysis for quota error details
        # Usually inside error.details
        if "error" in error_data and "details" in error_data["error"]:
            for detail in error_data["error"]["details"]:
                # Check for QuotaFailure or RetryInfo
                if "fieldViolations" in detail: # Sometimes here
                    pass
                
                # Check for standard google.rpc.RetryInfo which contains retryDelay
                # Or specific quota fields
                pass
        return None
    except Exception:
        return None
