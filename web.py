"""
Main Web Integration - Integrates all routers and modules
集合router并开启主服务
"""

import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from config import get_server_host, get_server_port
from log import log

# Import managers and utilities
from src.credential_manager import get_credential_manager
from src.routers.gemini import router as gemini_router
from src.routers.openai import router as openai_router
from src.routers.auth import router as auth_router
from src.routers.admin import router as admin_router
from src.routers.user import router as user_router
from src.routers.credentials import router as credentials_router
from src.routers.dashboard import router as dashboard_router
from src.task_manager import shutdown_all_tasks

# Note: web_router is removed in favor of split routers

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    log.info("启动 GCLI2API 主服务")

    # 初始化全局凭证管理器
    try:
        # 使用 src/credential_manager.py 中的单例
        cm = await get_credential_manager()
        # get_credential_manager 内部会自动 initialize
        log.info("凭证管理器初始化成功")
        
        # 初始化用户管理器
        from src.user_manager import user_manager
        await user_manager.initialize()
        log.info("用户管理器初始化成功")
        
    except Exception as e:
        log.error(f"管理器初始化失败: {e}")

    # 自动从环境变量加载凭证（异步执行）
    try:
        import asyncio
        from src.services.auth_service import auth_service

        async def load_env_creds():
            try:
                await auth_service.auto_load_env_credentials()
            except Exception as e:
                log.error(f"自动加载环境变量凭证失败: {e}")

        asyncio.create_task(load_env_creds())
    except Exception as e:
        log.error(f"创建自动加载环境变量凭证任务失败: {e}")

    yield

    # 清理资源
    log.info("开始关闭 GCLI2API 主服务")

    try:
        await shutdown_all_tasks(timeout=10.0)
        log.info("所有异步任务已关闭")
    except Exception as e:
        log.error(f"关闭异步任务时出错: {e}")

    # 关闭凭证管理器
    try:
        cm = await get_credential_manager()
        await cm.close()
        log.info("凭证管理器已关闭")
    except Exception as e:
        log.error(f"关闭凭证管理器时出错: {e}")

    log.info("GCLI2API 主服务已停止")


# 创建FastAPI应用
app = FastAPI(
    title="GCLI2API",
    description="Gemini API proxy with OpenAI compatibility",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 挂载路由器
app.include_router(openai_router, prefix="", tags=["OpenAI Compatible API"])
app.include_router(gemini_router, prefix="", tags=["Gemini Native API"])
app.include_router(dashboard_router, prefix="", tags=["Dashboard"])
app.include_router(auth_router, prefix="", tags=["Authentication"])
app.include_router(admin_router, prefix="", tags=["Administration"])
app.include_router(user_router, prefix="", tags=["User Profile"])
app.include_router(credentials_router, prefix="", tags=["Credentials"])

# 静态文件路由
app.mount("/docs", StaticFiles(directory="docs"), name="docs")


# 保活接口
@app.head("/keepalive")
async def keepalive() -> Response:
    return Response(status_code=200)

# 导出给其他模块使用
__all__ = ["app", "get_credential_manager"]


async def main():
    """异步主启动函数"""
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    # 日志系统现在直接使用环境变量，无需初始化
    # 从环境变量或配置获取端口和主机
    port = await get_server_port()
    host = await get_server_host()

    log.info("=" * 60)
    log.info("启动 GCLI2API")
    import os
    log.info(f"运行目录: {os.getcwd()}")
    log.info("=" * 60)
    log.info(f"控制面板: http://127.0.0.1:{port}")
    log.info("=" * 60)
    log.info("API端点:")
    log.info(f"   OpenAI兼容: http://127.0.0.1:{port}/v1")
    log.info(f"   Gemini原生: http://127.0.0.1:{port}")

    # 配置hypercorn
    config = Config()
    config.bind = [f"{host}:{port}"]
    config.accesslog = "-"
    config.errorlog = "-"
    config.loglevel = "INFO"
    config.use_colors = True

    # 设置请求体大小限制为100MB
    config.max_request_body_size = 100 * 1024 * 1024

    # 设置连接超时
    config.keep_alive_timeout = 300  # 5分钟
    config.read_timeout = 300  # 5分钟读取超时
    config.write_timeout = 300  # 5分钟写入超时

    # 增加启动超时时间以支持大量凭证的场景
    config.startup_timeout = 120  # 2分钟启动超时

    await serve(app, config)


if __name__ == "__main__":
    asyncio.run(main())
