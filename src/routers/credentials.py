import os
import json
import io
import zipfile
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse

from log import log
from src.dependencies import get_current_user_id
from src.credential_manager import get_credential_manager
from src.utils import get_user_filename, strip_user_prefix
from src.schemas.credential import CredFileActionRequest, CredFileBatchActionRequest
import config

router = APIRouter()

async def ensure_credential_manager_initialized():
    return await get_credential_manager()

@router.get("/creds/status")
async def get_creds_status(user_id: str = Depends(get_current_user_id)):
    """获取当前用户的凭证文件的状态"""
    try:
        credential_manager = await get_credential_manager()
        storage_adapter = credential_manager._storage_adapter

        # 获取所有凭证和状态（状态通过 CredentialManager）
        all_credentials = await storage_adapter.list_credentials()
        all_states = await credential_manager.get_creds_status()
        
        # 过滤属于当前用户的凭证
        user_prefix = f"u_{user_id}_"
        user_credentials = [f for f in all_credentials if f.startswith(user_prefix)]

        backend_info = await storage_adapter.get_backend_info()
        backend_type = backend_info.get("backend_type", "unknown")

        import asyncio
        async def process_credential_data(filename):
            """并发处理单个凭证的数据获取"""
            file_status = all_states.get(filename)

            if not file_status:
                try:
                    default_state = {
                        "error_codes": [],
                        "disabled": False,
                        "last_success": time.time(),
                        "user_email": None,
                    }
                    await storage_adapter.update_credential_state(filename, default_state)
                    file_status = default_state
                    log.debug(f"为凭证 {filename} 创建了默认状态记录")
                except Exception as e:
                    log.warning(f"无法为凭证 {filename} 创建状态记录: {e}")
                    file_status = {
                        "error_codes": [],
                        "disabled": False,
                        "last_success": time.time(),
                        "user_email": None,
                    }

            try:
                credential_data = await storage_adapter.get_credential(filename)
                if credential_data:
                    result = {
                        "status": file_status,
                        "content": credential_data,
                        "filename": os.path.basename(filename),
                        "backend_type": backend_type,
                        "user_email": file_status.get("user_email"),
                    }

                    # 添加冷却状态信息
                    cooldown_until = file_status.get("cooldown_until")
                    if cooldown_until:
                        current_time = time.time()
                        if current_time < cooldown_until:
                            # 仍在冷却期
                            remaining_seconds = int(cooldown_until - current_time)
                            result["cooldown_status"] = "cooling"
                            result["cooldown_until"] = cooldown_until
                            result["cooldown_remaining_seconds"] = remaining_seconds
                        else:
                            # 冷却期已过
                            result["cooldown_status"] = "ready"
                    else:
                        # 没有冷却
                        result["cooldown_status"] = "ready"

                    if backend_type == "file" and os.path.exists(filename):
                        result.update(
                            {
                                "size": os.path.getsize(filename),
                                "modified_time": os.path.getmtime(filename),
                            }
                        )

                    return filename, result
                else:
                    return filename, {
                        "status": file_status,
                        "content": None,
                        "filename": os.path.basename(filename),
                        "error": "凭证数据不存在",
                    }

            except Exception as e:
                log.error(f"读取凭证文件失败 {filename}: {e}")
                return filename, {
                    "status": file_status,
                    "content": None,
                    "filename": os.path.basename(filename),
                    "error": str(e),
                }

        # 并发处理所有凭证数据获取
        log.debug(f"开始并发获取 {len(user_credentials)} 个凭证数据...")
        concurrent_tasks = [process_credential_data(filename) for filename in user_credentials]
        results = await asyncio.gather(*concurrent_tasks, return_exceptions=True)

        # 组装结果
        creds_info = {}
        for result in results:
            if isinstance(result, Exception):
                log.error(f"处理凭证状态异常: {result}")
            else:
                filename, credential_info = result
                # 去掉前缀返回给前端
                clean_filename = strip_user_prefix(user_id, filename)
                credential_info['filename'] = clean_filename
                creds_info[clean_filename] = credential_info

        return JSONResponse(content={"creds": creds_info})

    except Exception as e:
        log.error(f"获取凭证状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/creds/action")
async def creds_action(request: CredFileActionRequest, user_id: str = Depends(get_current_user_id)):
    """对凭证文件执行操作（启用/禁用/删除）"""
    try:
        credential_manager = await get_credential_manager()

        log.info(f"Received request: {request}")

        # 添加用户前缀
        filename = get_user_filename(user_id, request.filename)
        action = request.action

        log.info(f"Performing action '{action}' on file: {filename}")

        if not filename.endswith(".json"):
            log.error(f"无效的文件名: {filename}（不是.json文件）")
            raise HTTPException(status_code=400, detail=f"无效的文件名: {filename}")

        storage_adapter = credential_manager._storage_adapter

        if action != "delete":
            credential_data = await storage_adapter.get_credential(filename)
            if not credential_data:
                log.error(f"凭证未找到: {filename}")
                raise HTTPException(status_code=404, detail="凭证文件不存在")

        if action == "enable":
            await credential_manager.set_cred_disabled(filename, False)
            return JSONResponse(content={"message": f"已启用凭证文件 {strip_user_prefix(user_id, filename)}"})

        elif action == "disable":
            await credential_manager.set_cred_disabled(filename, True)
            return JSONResponse(content={"message": f"已禁用凭证文件 {strip_user_prefix(user_id, filename)}"})

        elif action == "delete":
            try:
                success = await credential_manager.remove_credential(filename)
                if success:
                    return JSONResponse(
                        content={"message": f"已删除凭证文件 {os.path.basename(filename)}"}
                    )
                else:
                    raise HTTPException(status_code=500, detail="删除凭证失败")
            except Exception as e:
                log.error(f"删除凭证 {filename} 时出错: {e}")
                raise HTTPException(status_code=500, detail=f"删除文件失败: {str(e)}")

        else:
            raise HTTPException(status_code=400, detail="无效的操作类型")

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"凭证文件操作失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/creds/batch-action")
async def creds_batch_action(
    request: CredFileBatchActionRequest, user_id: str = Depends(get_current_user_id)
):
    """批量对凭证文件执行操作（启用/禁用/删除）"""
    try:
        credential_manager = await get_credential_manager()

        action = request.action
        filenames = request.filenames

        if not filenames:
            raise HTTPException(status_code=400, detail="文件名列表不能为空")

        log.info(f"对 {len(filenames)} 个文件执行批量操作 '{action}'")

        success_count = 0
        errors = []

        storage_adapter = credential_manager._storage_adapter

        for filename in filenames:
            try:
                real_filename = get_user_filename(user_id, filename)
                
                if not real_filename.endswith(".json"):
                    errors.append(f"{filename}: 无效的文件类型")
                    continue

                if action != "delete":
                    credential_data = await storage_adapter.get_credential(real_filename)
                    if not credential_data:
                        errors.append(f"{filename}: 凭证不存在")
                        continue

                if action == "enable":
                    await credential_manager.set_cred_disabled(real_filename, False)
                    success_count += 1

                elif action == "disable":
                    await credential_manager.set_cred_disabled(real_filename, True)
                    success_count += 1

                elif action == "delete":
                    try:
                        delete_success = await credential_manager.remove_credential(real_filename)
                        if delete_success:
                            success_count += 1
                        else:
                            errors.append(f"{filename}: 删除失败")
                            continue
                    except Exception as e:
                        errors.append(f"{filename}: 删除文件失败 - {str(e)}")
                        continue
                else:
                    errors.append(f"{filename}: 无效的操作类型")
                    continue

            except Exception as e:
                log.error(f"处理 {filename} 时出错: {e}")
                errors.append(f"{filename}: 处理失败 - {str(e)}")
                continue

        result_message = f"批量操作完成：成功处理 {success_count}/{len(filenames)} 个文件"
        if errors:
            result_message += "\n错误详情:\n" + "\n".join(errors)

        response_data = {
            "success_count": success_count,
            "total_count": len(filenames),
            "errors": errors,
            "message": result_message,
        }

        return JSONResponse(content=response_data)

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"批量凭证文件操作失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/creds/download/{filename}")
async def download_cred_file(filename: str, user_id: str = Depends(get_current_user_id)):
    """下载单个凭证文件"""
    try:
        if not filename.endswith(".json"):
            raise HTTPException(status_code=404, detail="无效的文件名")

        credential_manager = await get_credential_manager()
        storage_adapter = credential_manager._storage_adapter

        real_filename = get_user_filename(user_id, filename)
        credential_data = await storage_adapter.get_credential(real_filename)
        if not credential_data:
            raise HTTPException(status_code=404, detail="文件不存在")

        content = json.dumps(credential_data, ensure_ascii=False, indent=2)

        return Response(
            content=content,
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"下载凭证文件失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/creds/fetch-email/{filename}")
async def fetch_user_email(filename: str, user_id: str = Depends(get_current_user_id)):
    """获取指定凭证文件的用户邮箱地址"""
    try:
        filename_only = os.path.basename(filename)
        if not filename_only.endswith(".json"):
            raise HTTPException(status_code=404, detail="无效的文件名")

        credential_manager = await get_credential_manager()
        storage_adapter = credential_manager._storage_adapter
        real_filename = get_user_filename(user_id, filename_only)
        credential_data = await storage_adapter.get_credential(real_filename)
        if not credential_data:
            raise HTTPException(status_code=404, detail="凭证文件不存在")

        email = await credential_manager.get_or_fetch_user_email(real_filename)

        if email:
            return JSONResponse(
                content={
                    "filename": filename_only,
                    "user_email": email,
                    "message": "成功获取用户邮箱",
                }
            )
        else:
            return JSONResponse(
                content={
                    "filename": filename_only,
                    "user_email": None,
                    "message": "无法获取用户邮箱，可能凭证已过期或权限不足",
                },
                status_code=400,
            )

    except HTTPException:
        raise
    except Exception as e:
        log.error(f"获取用户邮箱失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/creds/refresh-all-emails")
async def refresh_all_user_emails(user_id: str = Depends(get_current_user_id)):
    """刷新所有凭证文件的用户邮箱地址"""
    try:
        credential_manager = await get_credential_manager()
        storage_adapter = credential_manager._storage_adapter

        all_credentials = await storage_adapter.list_credentials()
        user_prefix = f"u_{user_id}_"
        credential_filenames = [f for f in all_credentials if f.startswith(user_prefix)]

        results = []
        success_count = 0

        for filename in credential_filenames:
            try:
                email = await credential_manager.get_or_fetch_user_email(filename)
                if email:
                    success_count += 1
                    results.append(
                        {
                            "filename": strip_user_prefix(user_id, os.path.basename(filename)),
                            "user_email": email,
                            "success": True,
                        }
                    )
                else:
                    results.append(
                        {
                            "filename": strip_user_prefix(user_id, os.path.basename(filename)),
                            "user_email": None,
                            "success": False,
                            "error": "无法获取邮箱",
                        }
                    )
            except Exception as e:
                results.append(
                    {
                        "filename": strip_user_prefix(user_id, os.path.basename(filename)),
                        "user_email": None,
                        "success": False,
                        "error": str(e),
                    }
                )

        return JSONResponse(
            content={
                "success_count": success_count,
                "total_count": len(credential_filenames),
                "results": results,
                "message": f"成功获取 {success_count}/{len(credential_filenames)} 个邮箱地址",
            }
        )

    except Exception as e:
        log.error(f"批量获取用户邮箱失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/creds/download-all")
async def download_all_creds(user_id: str = Depends(get_current_user_id)):
    """打包下载所有凭证文件"""
    try:
        credential_manager = await get_credential_manager()
        storage_adapter = credential_manager._storage_adapter

        all_credentials = await storage_adapter.list_credentials()
        user_prefix = f"u_{user_id}_"
        credential_filenames = [f for f in all_credentials if f.startswith(user_prefix)]

        if not credential_filenames:
            raise HTTPException(status_code=404, detail="没有找到凭证文件")

        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
            for filename in credential_filenames:
                try:
                    credential_data = await storage_adapter.get_credential(filename)
                    if credential_data:
                        content = json.dumps(credential_data, ensure_ascii=False, indent=2)
                        clean_name = strip_user_prefix(user_id, os.path.basename(filename))
                        zip_file.writestr(clean_name, content)
                        log.debug(f"已添加到ZIP: {clean_name}")
                except Exception as e:
                    log.warning(f"处理凭证文件 {filename} 时出错: {e}")
                    continue

        zip_buffer.seek(0)
        return Response(
            content=zip_buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=credentials.zip"},
        )

    except Exception as e:
        log.error(f"打包下载失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
