"""
Gemini Router - Handles native Gemini format API requests
处理原生Gemini格式请求的路由模块
"""

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import (
    get_available_models,
)
from log import log
from src.services.gemini_service import gemini_service
from src.credential_manager import get_credential_manager
from src.user_manager import user_manager
from src.task_manager import create_managed_task

# 创建路由器
router = APIRouter()
security = HTTPBearer()

async def authenticate(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    """验证用户密码或API Key（Bearer Token方式）"""
    token = credentials.credentials
    
    # 1. 尝试验证 API Key
    if token.startswith("sk-gcli-"):
        user_id = await user_manager.get_user_by_api_key(token)
        if user_id:
            return {"type": "user", "user_id": user_id, "token": token}

     # 2. 尝试验证 Bearer Token (OAuth Access Token)
    # 对于直接传递 Google Access Token 的情况，视为 "proxy" 模式，不关联特定用户
    # 但需要检查是否允许匿名/非注册用户使用 (Policies TBD)
    # 目前保持兼容性，允许透传
    return {"type": "oauth", "token": token}


@router.get("/v1/models")
async def list_models(
    auth: dict = Depends(authenticate),
):
    """
    列出所有可用模型
    """
    try:
        models = await gemini_service.list_models()
        return {"models": models}
    except Exception as e:
        log.error(f"列出模型失败: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list models: {str(e)}",
        )


@router.post("/v1/models/{model:path}:generateContent")
async def generate_content(
    request: Request,
    model: str = Path(..., description="The model to use for generation"),
    auth: dict = Depends(authenticate),
    x_goog_user_project: Optional[str] = Header(None, alias="x-goog-user-project"),
):
    """
    生成内容 (GenerateContent)
    支持流式和非流式
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # 1. 获取凭证
    credential_manager = await get_credential_manager()
    cred_to_use = None
    
    if auth["type"] == "user":
        # 用户API Key模式
        user_id = auth["user_id"]
        # 使用用户自己的凭证或系统分配的凭证
        # 这里简化逻辑：用户API Key请求默认使用系统轮换策略，或者用户指定的 specific credential (TBD)
        # 目前逻辑：API Key 对应 User，User 可能有自己的 Creds，也可能用全局
        # 简单起见，先用 credential_manager 的智能获取
        
        # 暂时没有传递 user_id 给 get_valid_credential，意味着使用全局池
        # TODO: Phase 3 增强 resource isolation
        cred_to_use = await credential_manager.get_valid_credential()
    else:
        # OAuth Token 透传模式
        # 构造临时凭证对象
        cred_to_use = {
            "token": auth["token"],
            # Project ID hidden or passed via header
             "project_id": x_goog_user_project or "unknown"
        }

    if not cred_to_use:
         raise HTTPException(status_code=503, detail="No available credentials")

    # 2. 调用服务
    try:
        # Check streaming
        # Gemini API stream is via `streamGenerateContent` usually, but sometimes via `alt=sse` or specific method
        # Standard Gemini REST: use :streamGenerateContent for streaming
        # But this endpoint catches :generateContent. 
        # So it is NON-STREAMING usually.
        
        response_data = await gemini_service.generate_content(
            model_id=model,
            payload=payload,
            credentials=cred_to_use,
            is_stream=False
        )
        return JSONResponse(content=response_data)

    except Exception as e:
        log.error(f"Generate content failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/v1/models/{model:path}:streamGenerateContent")
async def stream_generate_content(
    request: Request,
    model: str = Path(..., description="The model to use for generation"),
    auth: dict = Depends(authenticate),
    x_goog_user_project: Optional[str] = Header(None, alias="x-goog-user-project"),
):
    """
    流式生成内容 (StreamGenerateContent)
    """
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # 1. 获取凭证
    credential_manager = await get_credential_manager()
    cred_to_use = None
    
    if auth["type"] == "user":
        cred_to_use = await credential_manager.get_valid_credential()
    else:
        cred_to_use = {
            "token": auth["token"],
             "project_id": x_goog_user_project or "unknown"
        }

    if not cred_to_use:
         raise HTTPException(status_code=503, detail="No available credentials")

    # 2. 调用服务
    try:
        stream_iterator = await gemini_service.generate_content(
            model_id=model,
            payload=payload,
            credentials=cred_to_use,
            is_stream=True
        )
        
        return StreamingResponse(
            stream_iterator,
            media_type="text/event-stream"
        )

    except Exception as e:
        log.error(f"Stream generate content failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
