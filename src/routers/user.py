import os
import io # if needed
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from log import log
from src.dependencies import get_current_user_id
from src.user_manager import user_manager
from src.audit_logger import audit_logger
from src.schemas.user import ChangePasswordRequest, UsageResetRequest
from src.usage_stats import get_usage_stats, get_usage_stats_instance
from src.credential_manager import get_credential_manager
from src.utils import get_user_filename, strip_user_prefix

router = APIRouter()

async def ensure_credential_manager_initialized():
    await get_credential_manager()

@router.post("/user/api-key")
async def regenerate_api_key(
    user_id: str = Depends(get_current_user_id)
):
    """重新生成当前用户的 API Key"""
    new_key = await user_manager.regenerate_api_key(user_id)
    return {"api_key": new_key}

@router.post("/user/password")
async def change_user_password(
    request: ChangePasswordRequest,
    user_id: str = Depends(get_current_user_id)
):
    """修改当前用户的密码"""
    if not request.new_password:
        raise HTTPException(status_code=400, detail="新密码不能为空")
    
    success = await user_manager.change_password(user_id, request.new_password)
    if success:
        return {"message": "密码修改成功"}
    else:
        raise HTTPException(status_code=500, detail="密码修改失败")

@router.get("/usage/stats")
async def get_usage_statistics(filename: Optional[str] = None, user_id: str = Depends(get_current_user_id)):
    """
    获取使用统计信息
    """
    try:
        real_filename = None
        if filename:
            real_filename = get_user_filename(user_id, filename)
            
        if real_filename:
            stats = await get_usage_stats(real_filename)
            return JSONResponse(content={"success": True, "data": stats})
        else:
            all_stats = await get_usage_stats(None)
            if isinstance(all_stats, dict):
                user_prefix = f"u_{user_id}_"
                user_stats = {}
                for k, v in all_stats.items():
                    if k.startswith(user_prefix):
                        user_stats[strip_user_prefix(user_id, k)] = v
                return JSONResponse(content={"success": True, "data": user_stats})
            else:
                return JSONResponse(content={"success": True, "data": all_stats})
    except Exception as e:
        log.error(f"获取使用统计失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/usage/aggregated")
async def get_aggregated_usage_statistics(user_id: str = Depends(get_current_user_id)):
    """
    获取聚合使用统计信息 (包含当前用户的凭证详情)
    """
    try:
        # 获取所有统计
        all_stats = await get_usage_stats(None)
        
        # 过滤当前用户
        user_prefix = f"u_{user_id}_"
        user_stats_values = []
        user_files_details = []
        
        if isinstance(all_stats, dict):
            for k, v in all_stats.items():
                if k.startswith(user_prefix):
                    user_stats_values.append(v)
                    # Add detail for breakdown
                    user_files_details.append({
                        "filename": strip_user_prefix(user_id, k),
                        "calls_24h": v.get("calls_24h", 0)
                    })

        total_files = len(user_stats_values)
        total_calls = sum(s.get("calls_24h", 0) for s in user_stats_values)
        
        # Get direct user calls
        user_stats_key = f"USER_stats_{user_id}"
        direct_user_calls = 0
        if isinstance(all_stats, dict) and user_stats_key in all_stats:
            direct_user_calls = all_stats[user_stats_key].get("calls_24h", 0)
        
        stats = {
            "total_files": total_files,
            "total_calls_24h": total_calls,
            "user_direct_calls_24h": direct_user_calls,
            "avg_calls_per_file": total_calls / max(total_files, 1),
            "files_breakdown": user_files_details # New field
        }
        
        return JSONResponse(content={"success": True, "data": stats})
    except Exception as e:
        log.error(f"获取聚合统计失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/usage/reset")
async def reset_usage_statistics(request: UsageResetRequest, user_id: str = Depends(get_current_user_id)):
    """
    重置使用统计
    """
    try:
        stats_instance = await get_usage_stats_instance()

        if request.filename:
            real_filename = get_user_filename(user_id, request.filename)
            await stats_instance.reset_stats(filename=real_filename)
            message = f"已重置 {request.filename} 的使用统计"
        else:
            credential_manager = await get_credential_manager()
            storage_adapter = credential_manager._storage_adapter
            all_credentials = await storage_adapter.list_credentials()
            user_prefix = f"u_{user_id}_"
            for filename in all_credentials:
                if filename.startswith(user_prefix):
                    await stats_instance.reset_stats(filename=filename)
            
            message = "已重置所有文件的使用统计"

        return JSONResponse(content={"success": True, "message": message})

    except Exception as e:
        log.error(f"重置使用统计失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
