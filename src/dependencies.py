from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from src.user_manager import user_manager
from typing import Optional

security = HTTPBearer()

async def get_current_user_id(
    credentials: HTTPAuthorizationCredentials = Depends(security)
) -> str:
    """
    验证 Token 并返回当前用户的 ID。
    """
    token = credentials.credentials
    user_id = await user_manager.verify_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="无效或过期的登录凭证")
    return user_id

# 兼容旧代码的依赖项
async def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    return await get_current_user_id(credentials)

async def get_current_user_role(
    user_id: str = Depends(get_current_user_id)
) -> str:
    return await user_manager.get_user_role(user_id)

async def require_admin(
    role: str = Depends(get_current_user_role)
):
    if role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")

def is_mobile_user_agent(user_agent: str) -> bool:
    """检测是否为移动设备用户代理"""
    if not user_agent:
        return False

    user_agent_lower = user_agent.lower()
    mobile_keywords = [
        "mobile", "android", "iphone", "ipad", "ipod", "blackberry",
        "windows phone", "samsung", "htc", "motorola", "nokia",
        "palm", "webos", "opera mini", "opera mobi", "fennec",
        "minimo", "symbian", "psp", "nintendo", "tablet",
    ]

    return any(keyword in user_agent_lower for keyword in mobile_keywords)
