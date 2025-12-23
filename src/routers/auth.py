import os
import io
import json
import zipfile
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, File, UploadFile, Request, Form
from fastapi.responses import JSONResponse

from log import log
from src.dependencies import get_current_user_id
from src.schemas.auth import LoginRequest, RegisterRequest, AuthStartRequest, AuthCallbackRequest, AuthCallbackUrlRequest
from src.user_manager import user_manager
from src.services.auth_service import auth_service
from src.credential_manager import get_credential_manager
from src.utils import get_user_filename, strip_user_prefix
from src.audit_logger import audit_logger

router = APIRouter()

async def ensure_credential_manager_initialized():
    await get_credential_manager()

# --- 用户认证 (登录/注册) ---

@router.post("/auth/register")
async def register(request: RegisterRequest):
    """Register a new user"""
    success = await user_manager.register(request.username, request.password)
    if success:
        audit_logger.log_event("register", "system", {"username": request.username}, "unknown")
        return {"message": "注册成功"}
    else:
        raise HTTPException(status_code=400, detail="注册失败，用户名可能已存在")

@router.post("/auth/login")
async def login(request: LoginRequest, http_request: Request):
    """Login and get token"""
    token, is_admin, user_id = await user_manager.login(request.username, request.password)
    if token:
        audit_logger.log_event("login_success", user_id, {"is_admin": is_admin}, http_request.client.host)
        return {"token": token, "is_admin": is_admin, "user_id": user_id}
    else:
        audit_logger.log_event("login_failed", "unknown", {"username": request.username}, http_request.client.host)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

@router.post("/auth/refresh-token")
async def refresh_user_token(
    request: Request,
    user_id: str = Depends(get_current_user_id)
):
    """Refresh JWT token"""
    try:
        user_info = await user_manager.get_user(user_id)
        if not user_info:
            raise HTTPException(status_code=404, detail="User not found")
        
        # Determine admin status
        is_admin = (await user_manager.get_user_role(user_id)) == 'admin'
        
        # Generate new token
        new_token = await user_manager.create_access_token(user_id)
        
        return {
             "token": new_token,
             "is_admin": is_admin,
             "user_id": user_id
        }
    except Exception as e:
        log.error(f"Failed to refresh token: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- OAuth 流程 ---

@router.post("/auth/start")
async def start_auth(request: AuthStartRequest, user_id: str = Depends(get_current_user_id)):
    """Start OAuth Flow"""
    result = await auth_service.create_auth_url(
        request.project_id, user_session=user_id, get_all_projects=request.get_all_projects
    )
    return result

@router.post("/auth/callback")
async def auth_callback_endpoint(request: AuthCallbackRequest, user_id: str = Depends(get_current_user_id)):
    """Process OAuth Callback"""
    # Use background task logic if necessary, but here we can wait as auth_service optimizes wait time
    result = await auth_service.complete_auth_flow(
        request.project_id, user_session=user_id
    )
    
    if result.get("success"):
        # If success, and we have a specific user (not global), we might want to rename/move the credential
        # But auth_service standardizes names as project_id-timestamp.json
        # User isolation demands prefixes.
        
        saved_filename = result.get("file_path")
        if saved_filename:
             # Move to user-specific name
             credential_manager = await get_credential_manager()
             storage = credential_manager._storage_adapter
             
             cred_data = await storage.get_credential(saved_filename)
             if cred_data:
                 new_filename = get_user_filename(user_id, saved_filename)
                 await credential_manager.add_credential(new_filename, cred_data)
                 # Remove global temp file
                 await credential_manager.remove_credential(saved_filename)
                 
                 result["file_path"] = strip_user_prefix(user_id, new_filename)
                 
    return result

@router.post("/auth/callback-url")
async def auth_callback_url(request: AuthCallbackUrlRequest):
    """Receive callback code manually (if needed)"""
    success = auth_service.handle_callback(request.state, request.code)
    return {"success": success}


# --- Env Creds ---
@router.post("/auth/load-env-creds")
async def api_load_env_creds(user_id: str = Depends(get_current_user_id)):
    """Load env creds and copy to user"""
    try:
        credential_manager = await get_credential_manager()
        storage = credential_manager._storage_adapter
        
        # Trigger reload in service (global)
        await auth_service.auto_load_env_credentials()
        
        # Copy to user
        all_creds = await storage.list_credentials()
        env_creds = [f for f in all_creds if f.startswith("env-")]
        
        count = 0
        for filename in env_creds:
            cred_data = await storage.get_credential(filename)
            user_filename = get_user_filename(user_id, filename)
            
            # Check if execution allowed or logic... for now just copy
            await credential_manager.add_credential(user_filename, cred_data)
            count += 1
            
        return {"success": True, "count": count, "message": f"Loaded {count} env credentials"}
    except Exception as e:
        log.error(f"Load env creds failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# --- Upload ---

@router.post("/auth/upload")
async def upload_credential(
    files: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
):
    """Upload credential JSON or ZIP"""
    try:
        filename = files.filename
        content = await files.read()
        
        credential_manager = await get_credential_manager()
        
        if filename.endswith(".zip"):
            # ZIP extraction
            try:
                zip_file = zipfile.ZipFile(io.BytesIO(content))
                valid_files = [f for f in zip_file.namelist() if f.endswith(".json")]
                
                success_count = 0
                for json_file in valid_files:
                    try:
                        file_content = zip_file.read(json_file).decode("utf-8")
                        cred_data = json.loads(file_content)
                        # Validate basic fields
                        if "client_id" in cred_data or "type" in cred_data:
                            target_filename = get_user_filename(user_id, os.path.basename(json_file))
                            await credential_manager.add_credential(target_filename, cred_data)
                            success_count += 1
                    except Exception as e:
                        log.warning(f"Skipping zip entry {json_file}: {e}")
                
                return {"message": f"Successfully imported {success_count} credentials from ZIP"}
            except Exception as e:
                 raise HTTPException(status_code=400, detail=f"Invalid ZIP file: {e}")
                 
        elif filename.endswith(".json"):
            try:
                cred_data = json.loads(content)
                target_filename = get_user_filename(user_id, filename)
                await credential_manager.add_credential(target_filename, cred_data)
                return {"message": "Credential uploaded successfully", "filename": filename}
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON format")
        else:
            raise HTTPException(status_code=400, detail="Only .json or .zip files allowed")
            
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Upload failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
