from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from log import log

router = APIRouter()

@router.get("/", response_class=HTMLResponse)
@router.get("/v1", response_class=HTMLResponse)
@router.get("/auth", response_class=HTMLResponse)
async def serve_control_panel(request: Request):
    """提供统一控制面板"""
    try:
        html_file_path = "front/control_panel.html"

        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)

    except Exception as e:
        log.error(f"加载控制面板页面失败: {e}")
        raise HTTPException(status_code=500, detail="服务器内部错误")


@router.get("/admin", response_class=HTMLResponse)
async def serve_admin_panel(request: Request):
    """提供管理后台面板"""
    try:
        html_file_path = "front/admin_panel.html" # Assuming path is relative to CWD
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)

    except Exception as e:
        log.error(f"加载管理后台页面失败: {e}")
        raise HTTPException(status_code=500, detail="服务器内部错误")
