import os
import sys
import time
import datetime
import io
import json
import zipfile
import signal
import psutil
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File, Form, WebSocket, Query
from fastapi.responses import JSONResponse, FileResponse
from starlette.websockets import WebSocketState

from log import log
from src.dependencies import require_admin, get_current_user_id, verify_token
from src.user_manager import user_manager
from src.credential_manager import get_credential_manager
from src.audit_logger import audit_logger
from src.call_logger import call_logger
from src.schemas.admin import (
    ConfigSaveRequest, AnnouncementRequest, MigrateRequest, ExportRequest, BatchActionRequest
)
from src.schemas.user import UserUpdateModel, ChangePasswordRequest
from src.schemas.credential import CredFileBatchActionRequest
from src.services.connection_manager import manager
from src.utils import get_user_filename, strip_user_prefix
from src.usage_stats import get_usage_stats, get_usage_stats_instance
import config
import toml

router = APIRouter()

async def ensure_credential_manager_initialized():
    await get_credential_manager()

@router.get("/admin/users")
async def list_users(_: None = Depends(require_admin)):
    """列出所有用户 (仅管理员)"""
    return await user_manager.list_users()

@router.post("/admin/users/{user_id}/status")
async def admin_update_user_status(
    user_id: str,
    data: UserUpdateModel,
    request: Request,
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Update user status (quota, disabled)"""
    await user_manager.update_user_status(user_id, disabled=data.disabled, quota_daily=data.quota_daily)
    audit_logger.log_event("update_user_status", current_user_id, {"target_user": user_id, "updates": data.dict(exclude_unset=True)}, request.client.host)
    return {"success": True}

@router.post("/admin/users/{user_id}/impersonate")
async def admin_impersonate_user(
    user_id: str,
    request: Request,
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Impersonate a user"""
    result = await user_manager.impersonate_user(user_id)
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    
    audit_logger.log_event("impersonate_user", current_user_id, {"target_user": user_id}, request.client.host)
    return result

@router.get("/admin/stats/trends")
async def admin_get_usage_trends(_: None = Depends(require_admin)):
    """获取过去24小时的流量趋势数据"""
    stats_manager = await get_usage_stats_instance()
    return await stats_manager.get_hourly_usage_trends()

@router.get("/admin/stats/latency")
async def admin_get_latency_trends(_: None = Depends(require_admin)):
    """获取过去24小时的响应延迟趋势数据 (平均/P95)"""
    stats_manager = await get_usage_stats_instance()
    return await stats_manager.get_hourly_latency_trends()

@router.post("/admin/credentials/test")
async def admin_test_credential(
    request: Request,
    _: None = Depends(require_admin)
):
    """测试指定凭证的可用性"""
    try:
        data = await request.json()
        filename = data.get("filename")
        if not filename:
             raise HTTPException(status_code=400, detail="Filename required")
             
        cm = await get_credential_manager()
        load_result = await cm._load_credential_by_name(filename)
        
        if not load_result:
            raise HTTPException(status_code=404, detail="Credential not found or failed to load")
            
        _, cred_data = load_result
        token = cred_data.get("token") or cred_data.get("access_token")
        project_id = cred_data.get("project_id")
        
        if not project_id and "quota_project_id" in cred_data:
             project_id = cred_data["quota_project_id"]
        
        if not token:
             return JSONResponse({
                 "success": False,
                 "latency": 0,
                 "status": "Token Error",
                 "log": "Failed to obtain access token from credential."
             })

        import httpx
        from config import get_code_assist_endpoint, BASE_MODELS
        
        endpoint = await get_code_assist_endpoint()
        url = f"{endpoint}/v1internal:generateContent"
        
        model_name = data.get("model")
        if not model_name:
             model_name = BASE_MODELS[0]
             
        test_content = {
            "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
            "generationConfig": {"maxOutputTokens": 10}
        }
        
        payload = {
            "model": model_name,
            "project": project_id or "default-project", 
            "request": test_content
        }
        
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        
        log.info(f"Testing credential {filename}: URL={url}, Model={model_name}, Project={payload['project']}")
        
        start_time = time.time()
        status_code = 0
        response_text = ""
        
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload, headers=headers)
            status_code = resp.status_code
            response_text = resp.text

        latency = (time.time() - start_time) * 1000
        success = 200 <= status_code < 300
        
        try:
            if success:
                await cm.record_api_call_result(filename, True)
            else:
                await cm.record_api_call_result(filename, False, status_code)
        except Exception as e:
            log.warning(f"Failed to record test result for {filename}: {e}")
        
        log_content = response_text
        if len(log_content) > 500:
            log_content = log_content[:500] + "..."
            
        return JSONResponse({
            "success": success,
            "latency": f"{latency:.0f}ms",
            "status": status_code,
            "log": f"Response [{status_code}]: {log_content}"
        })
        
    except Exception as e:
        log.error(f"Test credential failed: {e}")
        import traceback
        return JSONResponse(status_code=500, content={"error": str(e), "trace": traceback.format_exc()})

@router.get("/admin/global/credentials")
async def admin_list_global_credentials(_: None = Depends(require_admin)):
    """List all global credentials with detailed status"""
    try:
        credential_manager = await get_credential_manager()
        storage_adapter = credential_manager._storage_adapter
        all_creds = await storage_adapter.list_credentials()
        all_states = await credential_manager.get_creds_status()
        usage_data = await get_usage_stats(None)
        
        result = []
        for filename in all_creds:
            if filename.startswith("USER_stats_") or filename.startswith("u_") or filename.startswith("_"):
                continue

            state = all_states.get(filename, {})
            stats = usage_data.get(filename, {})
            result.append({
                "filename": filename,
                "owner": "Global",
                "disabled": state.get("disabled", False),
                "cooldown_until": state.get("cooldown_until"),
                "error_codes": state.get("error_codes", []),
                "last_success": state.get("last_success"),
                "calls_24h": stats.get("calls_24h", 0),
                "user_email": state.get("user_email")
            })
            
        result.sort(key=lambda x: x["filename"])
        return result
    except Exception as e:
        log.error(f"Failed to list global credentials: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/stats/health")
async def admin_get_health_stats(_: None = Depends(require_admin)):
    """Get system health and ownership statistics"""
    try:
        credential_manager = await get_credential_manager()
        all_creds = await credential_manager._storage_adapter.list_credentials()
        all_states = await credential_manager.get_creds_status()
        
        health_stats = {"healthy": 0, "error": 0, "disabled": 0}
        ownership_stats = {"user": 0, "global": 0}
        
        for filename in all_creds:
            if filename.startswith("USER_stats_") or filename.startswith("_"):
                continue
                
            state = all_states.get(filename, {})
            
            # Health
            if state.get("disabled"):
                health_stats["disabled"] += 1
            elif state.get("error_codes") and len(state.get("error_codes")) > 0:
                health_stats["error"] += 1
            else:
                health_stats["healthy"] += 1
                
            # Ownership
            if filename.startswith("u_"):
                ownership_stats["user"] += 1
            else:
                ownership_stats["global"] += 1

        return {
            "health_stats": health_stats,
            "ownership_stats": ownership_stats
        }
    except Exception as e:
        log.error(f"Failed to get health stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/stats/errors")
async def admin_get_error_logs(limit: int = 50, _: None = Depends(require_admin)):
    """Get recent error logs"""
    from src.error_logger import error_logger
    return error_logger.get_recent_errors(limit=limit)

@router.get("/admin/audit_logs")
async def admin_get_audit_logs(
    page: int = 1,
    limit: int = 100,
    action: Optional[str] = None,
    user_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    _: None = Depends(require_admin)
):
    """Get audit logs with pagination and filters."""
    start_ts = None
    end_ts = None
    
    try:
        if start_date:
            dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
            start_ts = dt.timestamp()
            
        if end_date:
            dt = datetime.datetime.strptime(end_date, "%Y-%m-%d")
            dt = dt.replace(hour=23, minute=59, second=59)
            end_ts = dt.timestamp()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format (use YYYY-MM-DD)")

    result = audit_logger.get_logs(
        page=page, 
        page_size=limit, 
        action_filter=action, 
        user_id_filter=user_id,
        start_time=start_ts,
        end_time=end_ts
    )
    
    user_cache = {}
    for log_entry in result.get("items", []):
        uid = log_entry.get("user_id")
        if uid and uid != "unknown":
            if uid not in user_cache:
                user_info = await user_manager.get_user(uid)
                user_cache[uid] = user_info.get("username") if user_info else None
            log_entry["username"] = user_cache.get(uid)
    
    return result

@router.get("/admin/audit_logs/export")
async def admin_export_audit_logs(
    action: Optional[str] = None,
    user_id: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    _: None = Depends(require_admin)
):
    """Export filtered audit logs as CSV"""
    import csv
    
    start_ts = None
    end_ts = None
    try:
        if start_date:
            dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
            start_ts = dt.timestamp()
        if end_date:
            dt = datetime.datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            end_ts = dt.timestamp()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format")

    result = audit_logger.get_logs(
        page=1, 
        page_size=100000, 
        action_filter=action,
        user_id_filter=user_id,
        start_time=start_ts,
        end_time=end_ts
    )
    
    logs = result.get("items", [])
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Date", "Action", "User ID", "IP", "Details"])
    
    for log_entry in logs:
        ts = log_entry.get("timestamp", 0)
        date_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
        details_str = json.dumps(log_entry.get("details", {}), ensure_ascii=False)
        writer.writerow([
            ts,
            date_str,
            log_entry.get("action", ""),
            log_entry.get("user_id", ""),
            log_entry.get("ip", ""),
            details_str
        ])
    
    output.seek(0)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit_logs_{int(time.time())}.csv"}
    )

@router.get("/admin/credentials/all")
async def admin_list_all_credentials(_: None = Depends(require_admin)):
    """List ALL credentials (Global + User) with detailed status for Master View"""
    try:
        credential_manager = await get_credential_manager()
        storage_adapter = credential_manager._storage_adapter
        all_creds = await storage_adapter.list_credentials()
        all_states = await credential_manager.get_creds_status()
        usage_data = await get_usage_stats(None)
        
        all_users = await user_manager.get_all_users()
        user_map = {u['id']: u['username'] for u in all_users}
        
        result = []
        for filename in all_creds:
            if filename.startswith("USER_stats_") or filename.startswith("_"):
                continue

            state = all_states.get(filename, {})
            stats = usage_data.get(filename, {})
            
            owner = "Global"
            owner_id = None
            username = None
            if filename.startswith("u_"):
                parts = filename.split("_")
                if len(parts) >= 2:
                    owner_id = parts[1]
                    username = user_map.get(owner_id, "Unknown")
                    owner = f"User: {username} ({owner_id})"
            
            result.append({
                "filename": filename,
                "owner": owner,
                "owner_id": owner_id,
                "username": username,
                "disabled": state.get("disabled", False),
                "cooldown_until": state.get("cooldown_until"),
                "error_codes": state.get("error_codes", []),
                "last_success": state.get("last_success"),
                "calls_24h": stats.get("calls_24h", 0),
                "user_email": state.get("user_email")
            })
            
        result.sort(key=lambda x: (x["owner"] == "Global", x["filename"]))
        return result
    except Exception as e:
        log.error(f"Failed to list all credentials: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/admin/credentials/upload")
async def admin_upload_credential(
    request: Request,
    file: UploadFile = File(...),
    target_user_id: str = Form(None),
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Upload a new credential file (Global or specific User)"""
    try:
        credential_manager = await get_credential_manager()
        
        filename = file.filename
        if not filename.endswith(".json"):
            raise HTTPException(status_code=400, detail="Credential file must be a JSON file (.json)")
        
        if target_user_id and target_user_id != "Global":
            safe_filename = filename.replace(" ", "_").replace("/", "").replace("\\", "")
            filename = f"u_{target_user_id}_{safe_filename}"
        
        content = await file.read()
        try:
            credential_data = json.loads(content)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid JSON file")

        is_service_account = "type" in credential_data and "project_id" in credential_data
        is_oauth_credential = "refresh_token" in credential_data or "client_id" in credential_data
        
        if not is_service_account and not is_oauth_credential:
             raise HTTPException(status_code=400, detail="Invalid credential file: must be a Service Account Key or OAuth credential")
             
        await credential_manager.add_credential(filename, credential_data)
        
        log.info(f"Admin uploaded credential: {filename} (Target User: {target_user_id})")
        
        audit_logger.log_event("upload_credential", current_user_id, {"filename": filename, "target_user": target_user_id}, request.client.host)
        return {"success": True, "filename": filename, "message": "Credential uploaded successfully"}
        
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to upload credential: {e}")
        audit_logger.log_event("upload_credential_failed", current_user_id, {"error": str(e)}, request.client.host)
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/credentials/{filename}/trace")
async def admin_trace_credential(
    filename: str,
    limit: int = 100,
    _: None = Depends(require_admin)
):
    """Get trace logs for a credential"""
    return call_logger.get_logs(limit=limit, credential_filter=filename)

@router.get("/admin/credentials/{filename}/download")
async def admin_download_credential(
    filename: str,
    request: Request,
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Download a credential file"""
    try:
        credential_manager = await get_credential_manager()
        cred_data = await credential_manager._storage_adapter.get_credential(filename)
        if not cred_data:
             raise HTTPException(status_code=404, detail="Credential not found")
             
        audit_logger.log_event("download_credential", current_user_id, {"filename": filename}, request.client.host)
        return Response(
            content=json.dumps(cred_data, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to download credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/admin/credentials/{filename}/toggle")
async def admin_toggle_credential(
    filename: str,
    request: Request,
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Toggle credential enabled/disabled status"""
    try:
        credential_manager = await get_credential_manager()
        
        state = await credential_manager._storage_adapter.get_credential_state(filename)
        current_disabled = state.get("disabled", False)
        new_disabled = not current_disabled
        
        await credential_manager.set_cred_disabled(filename, new_disabled)
        log.info(f"Admin toggled {filename}: disabled={new_disabled}")
        audit_logger.log_event("toggle_credential", current_user_id, {"filename": filename, "disabled": new_disabled}, request.client.host)
        return {"success": True, "disabled": new_disabled}
    except Exception as e:
        log.error(f"Failed to toggle credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.delete("/admin/credentials/{filename}")
async def admin_delete_credential(
    filename: str,
    request: Request,
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Permanently delete a credential"""
    try:
        credential_manager = await get_credential_manager()
        await credential_manager.remove_credential(filename)
        log.info(f"Admin deleted credential: {filename}")
        audit_logger.log_event("delete_credential", current_user_id, {"filename": filename}, request.client.host)
        return {"success": True, "message": "Credential deleted"}
    except Exception as e:
        log.error(f"Failed to delete credential: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/admin/global/credentials/batch-action")
async def admin_global_credential_batch_action(
    request: CredFileBatchActionRequest,
    _: None = Depends(require_admin)
):
    """Perform batch action (enable, disable, delete) on global credential files"""
    try:
        credential_manager = await get_credential_manager()
        action = request.action
        filenames = request.filenames
        
        results = []
        for filename in filenames:
            if filename.startswith("u_"):
                results.append({"filename": filename, "status": "failed", "message": "Cannot perform action on user-specific credential via global endpoint"})
                continue

            try:
                if action == "enable":
                    await credential_manager.set_cred_disabled(filename, False)
                    message = f"Global credential '{filename}' enabled."
                elif action == "disable":
                    await credential_manager.set_cred_disabled(filename, True)
                    message = f"Global credential '{filename}' disabled."
                elif action == "delete":
                    await credential_manager._storage_adapter.delete_credential(filename)
                    credential_manager.invalidate_credential_cache(filename)
                    message = f"Global credential '{filename}' deleted."
                else:
                    raise ValueError("Invalid action. Must be 'enable', 'disable', or 'delete'.")
                
                results.append({"filename": filename, "status": "success", "message": message})
            except Exception as e:
                log.error(f"Batch action '{action}' failed for '{filename}': {e}")
                results.append({"filename": filename, "status": "failed", "message": str(e)})
            
        return {"message": f"Batch action '{action}' completed.", "results": results}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Failed to perform batch action on global credentials: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/admin/creds/batch")
async def admin_batch_credential_action(
    request: BatchActionRequest,
    http_request: Request,
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Perform batch action (enable/disable/delete) on any credentials"""
    try:
        credential_manager = await get_credential_manager()
        action = request.action
        filenames = request.filenames
        
        success_count = 0
        failed_count = 0
        errors = []
        
        for filename in filenames:
            try:
                if action == "enable":
                    await credential_manager.set_cred_disabled(filename, False)
                elif action == "disable":
                    await credential_manager.set_cred_disabled(filename, True)
                elif action == "delete":
                    await credential_manager.remove_credential(filename)
                else:
                    raise ValueError(f"Invalid action: {action}")
                
                success_count += 1
            except Exception as e:
                failed_count += 1
                errors.append({"filename": filename, "error": str(e)})
        
        audit_logger.log_event(
            f"batch_{action}", 
            current_user_id, 
            {"count": success_count, "failed": failed_count, "filenames": filenames}, 
            http_request.client.host
        )
        
        return {"success": success_count, "failed": failed_count, "errors": errors}
    except Exception as e:
        log.error(f"Batch action failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/admin/credentials/{filename}/migrate")
async def admin_migrate_credential(
    filename: str,
    request: MigrateRequest,
    http_request: Request,
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Migrate a credential from one user to another (or to Global)"""
    try:
        credential_manager = await get_credential_manager()
        storage = credential_manager._storage_adapter
        
        cred_data = await storage.get_credential(filename)
        if not cred_data:
            raise HTTPException(status_code=404, detail="Credential not found")
        
        target_user_id = request.target_user_id
        
        base_name = filename
        if filename.startswith("u_"):
            parts = filename.split("_", 2)
            if len(parts) >= 3:
                base_name = parts[2]
        
        if target_user_id == "global" or not target_user_id:
            new_filename = base_name
        else:
            new_filename = f"u_{target_user_id}_{base_name}"
        
        existing = await storage.get_credential(new_filename)
        if existing:
            raise HTTPException(status_code=409, detail=f"Target credential '{new_filename}' already exists")
        
        await credential_manager.add_credential(new_filename, cred_data)
        await credential_manager.remove_credential(filename)
        
        audit_logger.log_event(
            "migrate_credential",
            current_user_id,
            {"from": filename, "to": new_filename, "target_user": target_user_id},
            http_request.client.host
        )
        
        return {"success": True, "old_filename": filename, "new_filename": new_filename}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Migration failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/admin/creds/export")
async def admin_export_credentials(
    request: ExportRequest,
    http_request: Request,
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Export selected credentials as encrypted ZIP"""
    try:
        credential_manager = await get_credential_manager()
        storage = credential_manager._storage_adapter
        
        filenames = request.filenames
        password = request.password
        
        if not password or len(password) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
        
        temp_zip = io.BytesIO()
        
        with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
            for filename in filenames:
                cred_data = await storage.get_credential(filename)
                if cred_data:
                    json_content = json.dumps(cred_data, indent=2)
                    zf.writestr(filename, json_content)
        
        temp_zip.seek(0)
        zip_content = temp_zip.getvalue()
        
        password_bytes = password.encode('utf-8')
        encrypted = bytearray()
        for i, byte in enumerate(zip_content):
            encrypted.append(byte ^ password_bytes[i % len(password_bytes)])
        
        audit_logger.log_event(
            "export_credentials",
            current_user_id,
            {"count": len(filenames), "filenames": filenames},
            http_request.client.host
        )
        
        return Response(
            content=bytes(encrypted),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename=credentials_backup_{int(time.time())}.gcli"}
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Export failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/admin/creds/import")
async def admin_import_credentials(
    http_request: Request,
    file: UploadFile = File(...),
    password: str = Form(...),
    target_user_id: str = Form(None),
    _: None = Depends(require_admin),
    current_user_id: str = Depends(get_current_user_id)
):
    """Import credentials from encrypted backup"""
    try:
        credential_manager = await get_credential_manager()
        
        encrypted_content = await file.read()
        
        password_bytes = password.encode('utf-8')
        decrypted = bytearray()
        for i, byte in enumerate(encrypted_content):
            decrypted.append(byte ^ password_bytes[i % len(password_bytes)])
        
        try:
            zip_buffer = io.BytesIO(bytes(decrypted))
            import_count = 0
            errors = []
            
            with zipfile.ZipFile(zip_buffer, 'r') as zf:
                for name in zf.namelist():
                    if not name.endswith('.json'):
                        continue
                    
                    try:
                        content = zf.read(name)
                        cred_data = json.loads(content.decode('utf-8'))
                        
                        if target_user_id:
                            base_name = name
                            if name.startswith("u_"):
                                parts = name.split("_", 2)
                                if len(parts) >= 3:
                                    base_name = parts[2]
                            final_name = f"u_{target_user_id}_{base_name}"
                        else:
                            final_name = name
                        
                        await credential_manager.save_credential(final_name, cred_data)
                        import_count += 1
                    except Exception as e:
                        errors.append({"filename": name, "error": str(e)})
            
            audit_logger.log_event(
                "import_credentials",
                current_user_id,
                {"count": import_count, "target_user": target_user_id},
                http_request.client.host
            )
            
            return {"success": True, "imported": import_count, "errors": errors}
            
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid backup file or wrong password")
            
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Import failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/config")
async def admin_get_config(_: None = Depends(require_admin)):
    """获取当前配置 (仅管理员)"""
    from config import get_config_instance
    cfg = get_config_instance()
    return cfg.get_all_config()

@router.post("/admin/config")
async def admin_save_config(
    request: ConfigSaveRequest,
    _: None = Depends(require_admin)
):
    """保存配置 (仅管理员)"""
    from config import get_config_instance
    cfg = get_config_instance()
    try:
        cfg.update_config(request.config)
        return {"message": "配置保存成功"}
    except Exception as e:
        log.error(f"保存配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"保存配置失败: {e}")

@router.post("/admin/config/reload")
async def admin_reload_config(_: None = Depends(require_admin)):
    """重新加载配置 (仅管理员)"""
    from config import get_config_instance
    cfg = get_config_instance()
    try:
        cfg.reload_config()
        return {"message": "配置重新加载成功"}
    except Exception as e:
        log.error(f"重新加载配置失败: {e}")
        raise HTTPException(status_code=500, detail=f"重新加载配置失败: {e}")

@router.post("/admin/system/shutdown")
async def admin_shutdown_system(_: None = Depends(require_admin)):
    """关闭系统 (仅管理员)"""
    log.warning("Admin initiated system shutdown.")
    import os
    import signal
    os.kill(os.getpid(), signal.SIGINT)
    return {"message": "System is shutting down..."}

@router.post("/admin/system/restart")
async def admin_restart_system(_: None = Depends(require_admin)):
    """重启系统 (仅管理员)"""
    log.warning("Admin initiated system restart.")
    import os
    import sys
    os.execv(sys.executable, ['python'] + sys.argv)
    return {"message": "System is restarting..."}

@router.get("/admin/system/logs")
async def admin_get_logs(tail: int = 100, _: None = Depends(require_admin)):
    """获取系统日志 (仅管理员)"""
    try:
        log_file_path = "app.log"
        if not os.path.exists(log_file_path):
            return {"logs": "Log file not found."}
        
        with open(log_file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            return {"logs": "".join(lines[-tail:])}
    except Exception as e:
        log.error(f"Failed to read logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/admin/system/status")
async def admin_get_system_status(_: None = Depends(require_admin)):
    """获取系统状态 (仅管理员)"""
    start_time = time.time() # This is incorrect, start_time is usually global.
    # We should get uptime from psutil or global variable
    # For now, just current process creation time
    p = psutil.Process(os.getpid())
    uptime_seconds = time.time() - p.create_time()
    
    return {
        "status": "running",
        "uptime": f"{uptime_seconds:.0f} seconds",
        "python_version": sys.version,
        "platform": sys.platform,
        "process_id": os.getpid(),
        "memory_usage": f"{p.memory_info().rss / (1024 * 1024):.2f} MB"
    }

@router.get("/admin/system/version")
async def admin_get_version(_: None = Depends(require_admin)):
    """获取应用版本信息 (仅管理员)"""
    try:
        # Assuming version.py is in the root and accessible via import or reading file
        # If import fails, read file
        try:
            from version import __version__
            return {"version": __version__}
        except ImportError:
             with open("version.py", "r") as f:
                 for line in f:
                     if line.startswith("__version__"):
                         return {"version": line.split("=")[1].strip().strip('"')}
             return {"version": "unknown"}
    except Exception:
        return {"version": "unknown"}

@router.get("/admin/system/dependencies")
async def admin_get_dependencies(_: None = Depends(require_admin)):
    """获取已安装的依赖包及其版本 (仅管理员)"""
    import pkg_resources
    dependencies = []
    for pkg in pkg_resources.working_set:
        dependencies.append({"name": pkg.project_name, "version": pkg.version})
    return dependencies

@router.get("/admin/system/environment")
async def admin_get_environment(_: None = Depends(require_admin)):
    """获取环境变量 (仅管理员)"""
    return dict(os.environ)

@router.get("/admin/system/healthcheck")
async def admin_healthcheck(_: None = Depends(require_admin)):
    """执行系统健康检查 (仅管理员)"""
    try:
        credential_manager = await get_credential_manager()
        storage_adapter = credential_manager._storage_adapter
        await storage_adapter.list_credentials()
        await user_manager.get_all_users()
        return {"status": "healthy", "message": "All core components are operational."}
    except Exception as e:
        log.error(f"Health check failed: {e}")
        raise HTTPException(status_code=500, detail=f"Health check failed: {e}")

@router.delete("/admin/users/{target_user_id}")
async def admin_delete_user(
    target_user_id: str,
    _: None = Depends(require_admin)
):
    """Delete a user"""
    success = await user_manager.delete_user(target_user_id)
    if success:
        return {"message": "用户删除成功"}
    else:
        raise HTTPException(status_code=500, detail="删除用户失败")

@router.post("/admin/users/{target_user_id}/password")
async def admin_reset_password(
    target_user_id: str,
    request: ChangePasswordRequest,
    _: None = Depends(require_admin)
):
    """Admin reset user password"""
    success = await user_manager.change_password(target_user_id, request.new_password)
    if success:
        return {"message": "密码重置成功"}
    else:
        raise HTTPException(status_code=500, detail="密码重置失败")

@router.get("/admin/users/{target_user_id}/credentials")
async def admin_get_user_credentials(
    target_user_id: str,
    _: None = Depends(require_admin)
):
    """Get all credentials for a specific user"""
    cm = await get_credential_manager()
    storage_adapter = cm._storage_adapter
    all_creds = await storage_adapter.list_credentials()
    
    user_prefix = f"u_{target_user_id}_"
    user_creds = []
    for filename in all_creds:
        if filename.startswith(user_prefix):
            user_creds.append(filename)
    return user_creds

@router.get("/admin/users/{target_user_id}/usage")
async def admin_get_user_usage(
    target_user_id: str,
    _: None = Depends(require_admin)
):
    """Get usage statistics for a specific user"""
    try:
        all_stats = await get_usage_stats(None)
        
        user_prefix = f"u_{target_user_id}_"
        user_stats_key = f"USER_stats_{target_user_id}"
        user_usage = {}
        
        if isinstance(all_stats, dict):
            for filename, stats in all_stats.items():
                base = os.path.basename(filename)
                if base.startswith(user_prefix) or base == user_stats_key:
                    user_usage[filename] = stats
        
        return user_usage
    except Exception as e:
        log.error(f"Failed to get user usage: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/auth/logs/clear")
async def clear_logs(_: None = Depends(require_admin)):
    """清空日志文件"""
    try:
        log_file_path = os.getenv("LOG_FILE", "log.txt")
        if os.path.exists(log_file_path):
            try:
                with open(log_file_path, "w", encoding="utf-8", newline="") as f:
                    f.write("")
                    f.flush()
                log.info(f"日志文件已清空: {log_file_path}")
                await manager.broadcast("--- 日志文件已清空 ---")
                return JSONResponse(content={"message": f"日志文件已清空: {os.path.basename(log_file_path)}"})
            except Exception as e:
                log.error(f"清空日志文件失败: {e}")
                raise HTTPException(status_code=500, detail=f"清空日志文件失败: {str(e)}")
        else:
            return JSONResponse(content={"message": "日志文件不存在"})
    except Exception as e:
        log.error(f"清空日志文件失败: {e}")
        raise HTTPException(status_code=500, detail=f"清空日志文件失败: {str(e)}")

@router.get("/auth/logs/download")
async def download_logs(_: None = Depends(require_admin)):
    """下载日志文件"""
    try:
        log_file_path = os.getenv("LOG_FILE", "log.txt")
        if not os.path.exists(log_file_path):
            raise HTTPException(status_code=404, detail="日志文件不存在")

        file_size = os.path.getsize(log_file_path)
        if file_size == 0:
            raise HTTPException(status_code=404, detail="日志文件为空")

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"gcli2api_logs_{timestamp}.txt"

        return FileResponse(
            path=log_file_path,
            filename=filename,
            media_type="text/plain",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"下载日志文件失败: {e}")
        raise HTTPException(status_code=500, detail=f"下载日志文件失败: {str(e)}")

@router.websocket("/auth/logs/stream")
async def websocket_logs(websocket: WebSocket, token: Optional[str] = Query(None)):
    """WebSocket端点，用于实时日志流"""
    if not await manager.connect(websocket):
        return

    try:
        if not token:
            await websocket.send_text("Error: Authentication token required")
            await websocket.close()
            return
            
        user_id = await user_manager.verify_token(token)
        if not user_id:
            await websocket.send_text("Error: Invalid token")
            await websocket.close()
            return
            
        role = await user_manager.get_user_role(user_id)
        if role != 'admin':
            await websocket.send_text("Error: Admin privileges required for logs")
            await websocket.close()
            return
    except Exception as e:
        log.error(f"WebSocket auth error: {e}")
        await websocket.close()
        return

    try:
        log_file_path = os.getenv("LOG_FILE", "log.txt")

        if os.path.exists(log_file_path):
            try:
                with open(log_file_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                    for line in lines[-50:]:
                        if line.strip():
                            await websocket.send_text(line.strip())
            except Exception as e:
                await websocket.send_text(f"Error reading log file: {e}")

        last_size = os.path.getsize(log_file_path) if os.path.exists(log_file_path) else 0
        max_read_size = 8192
        check_interval = 2

        async def listen_for_disconnect():
            try:
                while True:
                    await websocket.receive_text()
            except Exception:
                pass

        listener_task = asyncio.create_task(listen_for_disconnect())

        try:
            while websocket.client_state == WebSocketState.CONNECTED:
                done, pending = await asyncio.wait(
                    [listener_task],
                    timeout=check_interval,
                    return_when=asyncio.FIRST_COMPLETED
                )

                if listener_task in done:
                    break

                if os.path.exists(log_file_path):
                    current_size = os.path.getsize(log_file_path)
                    if current_size > last_size:
                        read_size = min(current_size - last_size, max_read_size)

                        try:
                            with open(log_file_path, "r", encoding="utf-8", errors="replace") as f:
                                f.seek(last_size)
                                new_content = f.read(read_size)

                                if not new_content:
                                    last_size = current_size
                                    continue

                                lines = new_content.splitlines(keepends=True)
                                if lines:
                                    if not lines[-1].endswith("\n") and len(lines) > 1:
                                        for line in lines[:-1]:
                                            if line.strip():
                                                await websocket.send_text(line.rstrip())
                                        last_size += len(new_content.encode("utf-8")) - len(
                                            lines[-1].encode("utf-8")
                                        )
                                    else:
                                        for line in lines:
                                            if line.strip():
                                                await websocket.send_text(line.rstrip())
                                        last_size += len(new_content.encode("utf-8"))
                        except UnicodeDecodeError as e:
                            log.warning(f"WebSocket日志读取编码错误: {e}, 跳过部分内容")
                            last_size = current_size
                        except Exception as e:
                            await websocket.send_text(f"Error reading new content: {e}")
                            last_size = current_size
                    elif current_size < last_size:
                        last_size = 0
                        await websocket.send_text("--- 日志已清空 ---")

        finally:
            if not listener_task.done():
                listener_task.cancel()
                try:
                    await listener_task
                except asyncio.CancelledError:
                    pass

    except Exception:
        pass
    finally:
        manager.disconnect(websocket)

@router.post("/admin/announcement")
async def set_announcement(
    request: AnnouncementRequest,
    _: None = Depends(require_admin)
):
    """Set system announcement (Admin only)"""
    try:
        credential_manager = await get_credential_manager()
        storage_adapter = credential_manager._storage_adapter
        
        data = {
            "content": request.content,
            "level": request.level,
            "enabled": request.enabled,
            "updated_at": datetime.datetime.now().isoformat()
        }
        
        await storage_adapter.set_config("system_announcement", data)
        return {"message": "Announcement updated", "data": data}
    except Exception as e:
        log.error(f"Failed to set announcement: {e}")
        raise HTTPException(status_code=500, detail=str(e))
