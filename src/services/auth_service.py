import asyncio
import socket
import threading
import time
import uuid
import json
import os
from datetime import timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from config import get_config_value, get_oauth_client_id, get_oauth_client_secret
from log import log
from src.storage_adapter import get_storage_adapter
from src.google_oauth_api import (
    Credentials,
    Flow,
    enable_required_apis,
    get_user_projects,
    select_default_project,
)

# Constants
SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]
CALLBACK_HOST = "localhost"
MAX_AUTH_FLOWS = 20
CLIENT_ID = "hardcoded_placeholder" # Will be ignored as we use get_oauth_client_id
CLIENT_SECRET = "hardcoded_placeholder" # Will be ignored as we use get_oauth_client_secret

class AuthCallbackHandler(BaseHTTPRequestHandler):
    """OAuth回调处理器"""

    def do_GET(self):
        query_components = parse_qs(urlparse(self.path).query)
        code = query_components.get("code", [None])[0]
        state = query_components.get("state", [None])[0]

        log.info(f"收到OAuth回调: code={'已获取' if code else '未获取'}, state={state}")
        
        auth_service = AuthService.get_instance_if_initialized()
        if not auth_service:
             log.error("AuthService not initialized during callback")
             self.send_error(500, "Internal Server Error")
             return

        if code and state and auth_service.handle_callback(state, code):
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            # 成功页面
            self.wfile.write(
                b"<h1>OAuth authentication successful!</h1><p>You can close this window. Please return to the original page and click 'Get Credentials' button.</p>"
            )
        else:
            self.send_response(400)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authentication failed.</h1><p>Please try again.</p>")

    def log_message(self, format, *args):
        # 减少日志噪音
        pass

class AuthService:
    _instance = None

    def __init__(self):
        self.auth_flows = {}
        self.lock = asyncio.Lock() # For async operations if needed, currently dict ops are somewhat atomic but good practice
        
    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = AuthService()
        return cls._instance
        
    @classmethod
    def get_instance_if_initialized(cls):
        return cls._instance

    async def get_callback_port(self):
        """获取OAuth回调端口"""
        return int(await get_config_value("oauth_callback_port", "8080", "OAUTH_CALLBACK_PORT"))

    def cleanup_auth_flows(self):
        """清理认证流程"""
        # Remove flows older than 30 mins
        now = time.time()
        expired = [state for state, data in self.auth_flows.items() if now - data.get("created_at", 0) > 1800]
        for state in expired:
            self._shutdown_flow_server(state)
            del self.auth_flows[state]
            
        # Hard limit cleanup
        if len(self.auth_flows) > 10:
             sorted_flows = sorted(
                self.auth_flows.items(), key=lambda x: x[1].get("created_at", 0), reverse=True
            )
             new_auth_flows = dict(sorted_flows[:10])
             
             for state, flow_data in self.auth_flows.items():
                 if state not in new_auth_flows:
                     self._shutdown_flow_server(state)
             
             self.auth_flows = new_auth_flows

    def _shutdown_flow_server(self, state):
        flow_data = self.auth_flows.get(state)
        if flow_data and flow_data.get("server"):
            try:
                server = flow_data["server"]
                server.shutdown()
                server.server_close()
            except Exception as e:
                pass # Already closed or error

    async def find_available_port(self, start_port: int = None) -> int:
        """动态查找可用端口"""
        if start_port is None:
            start_port = await self.get_callback_port()

        # 首先尝试默认端口
        for port in range(start_port, start_port + 100):  # 尝试100个端口
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("0.0.0.0", port))
                    return port
            except OSError:
                continue

        # 如果都不可用，让系统自动分配端口
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("0.0.0.0", 0))
                port = s.getsockname()[1]
                return port
        except OSError as e:
            log.error(f"无法找到可用端口: {e}")
            raise RuntimeError("无法找到可用端口")

    def create_callback_server(self, port: int) -> HTTPServer:
        """创建指定端口的回调服务器"""
        try:
            server = HTTPServer(("0.0.0.0", port), AuthCallbackHandler)
            server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.timeout = 1.0
            return server
        except OSError as e:
            log.error(f"创建端口{port}的服务器失败: {e}")
            raise

    def handle_callback(self, state: str, code: str) -> bool:
        """处理回调逻辑"""
        if state in self.auth_flows:
            self.auth_flows[state]["code"] = code
            self.auth_flows[state]["completed"] = True
            log.info(f"OAuth回调成功处理: state={state}")
            return True
        return False

    async def create_auth_url(
        self, project_id: Optional[str] = None, user_session: str = None, get_all_projects: bool = False
    ) -> Dict[str, Any]:
        """创建认证URL"""
        try:
            callback_port = await self.find_available_port()
            callback_url = f"http://{CALLBACK_HOST}:{callback_port}"

            try:
                callback_server = self.create_callback_server(callback_port)
                server_thread = threading.Thread(
                    target=callback_server.serve_forever,
                    daemon=True,
                    name=f"OAuth-Server-{callback_port}",
                )
                server_thread.start()
                log.info(f"OAuth回调服务器已启动，端口: {callback_port}")
            except Exception as e:
                log.error(f"启动回调服务器失败: {e}")
                return {"success": False, "error": str(e)}

            flow = Flow(
                client_id=await get_oauth_client_id(),
                client_secret=await get_oauth_client_secret(),
                scopes=SCOPES,
                redirect_uri=callback_url,
            )

            if user_session:
                state = f"{user_session}_{str(uuid.uuid4())}"
            else:
                state = str(uuid.uuid4())

            auth_url = flow.get_auth_url(state=state)

            if len(self.auth_flows) >= MAX_AUTH_FLOWS:
                self.cleanup_auth_flows()

            self.auth_flows[state] = {
                "flow": flow,
                "project_id": project_id,
                "user_session": user_session,
                "callback_port": callback_port,
                "callback_url": callback_url,
                "server": callback_server,
                "server_thread": server_thread,
                "code": None,
                "completed": False,
                "created_at": time.time(),
                "auto_project_detection": project_id is None,
                "get_all_projects": get_all_projects,
            }

            return {
                "auth_url": auth_url,
                "state": state,
                "callback_port": callback_port,
                "success": True,
                "auto_project_detection": project_id is None,
                "detected_project_id": project_id,
            }

        except Exception as e:
            log.error(f"创建认证URL失败: {e}")
            return {"success": False, "error": str(e)}

    async def save_credentials(self, creds: Credentials, project_id: str) -> str:
        """保存凭证"""
        timestamp = int(time.time())
        filename = f"{project_id}-{timestamp}.json"

        creds_data = {
            "client_id": await get_oauth_client_id(),
            "client_secret": await get_oauth_client_secret(),
            "token": creds.access_token,
            "refresh_token": creds.refresh_token,
            "scopes": SCOPES,
            "token_uri": "https://oauth2.googleapis.com/token",
            "project_id": project_id,
        }

        if creds.expires_at:
            if creds.expires_at.tzinfo is None:
                expiry_utc = creds.expires_at.replace(tzinfo=timezone.utc)
            else:
                expiry_utc = creds.expires_at
            creds_data["expiry"] = expiry_utc.isoformat()

        storage_adapter = await get_storage_adapter()
        success = await storage_adapter.store_credential(filename, creds_data)

        if success:
            try:
                default_state = {
                    "error_codes": [],
                    "disabled": False,
                    "last_success": time.time(),
                    "user_email": None,
                }
                await storage_adapter.update_credential_state(filename, default_state)
            except Exception as e:
                log.warning(f"创建默认状态记录失败 {filename}: {e}")
            return filename
        else:
            raise Exception(f"保存凭证失败: {filename}")

    async def get_auth_flow(self, project_id: Optional[str] = None, user_session: str = None) -> tuple[Optional[str], Optional[dict]]:
        """Find matching auth flow"""
        state = None
        flow_data = None

        if project_id:
            for s, data in self.auth_flows.items():
                if data["project_id"] == project_id:
                    if user_session and data.get("user_session") == user_session:
                        return s, data
                    elif not state:
                        state = s
                        flow_data = data
        
        if not state:
             for s, data in self.auth_flows.items():
                 if data.get("auto_project_detection", False):
                     if user_session and data.get("user_session") == user_session:
                         return s, data
                     elif not state:
                         state = s
                         flow_data = data
                         
        return state, flow_data

    async def complete_auth_flow(self, project_id: Optional[str] = None, user_session: str = None) -> Dict[str, Any]:
        """完成认证流程"""
        try:
            state, flow_data = await self.get_auth_flow(project_id, user_session)
            
            if not state or not flow_data:
                return {"success": False, "error": "未找到对应的认证流程"}

            if not project_id and not flow_data.get("auto_project_detection"):
                 project_id = flow_data.get("project_id")
                 if not project_id:
                     return {"success": False, "error": "Missing Project ID"}

            # Sync wait for callback if not ready
            if not flow_data.get("code"):
                log.info(f"Wait for callback state={state}")
                start_time = time.time()
                while time.time() - start_time < 300:
                    if flow_data.get("code"):
                        break
                    await asyncio.sleep(0.5)
                
                if not flow_data.get("code"):
                    return {"success": False, "error": "Callback timeout"}

            auth_code = flow_data["code"]
            flow = flow_data["flow"]
            
            # Monkey patch oauthlib if needed (reference implementation)
            import oauthlib.oauth2.rfc6749.parameters
            original_validate = oauthlib.oauth2.rfc6749.parameters.validate_token_parameters
            oauthlib.oauth2.rfc6749.parameters.validate_token_parameters = lambda params: original_validate(params) if not any(isinstance(p, Exception) for p in params) else None # Simplified patch logic access

            try:
                credentials = await flow.exchange_code(auth_code)
                
                # Auto detection logic
                if flow_data.get("auto_project_detection") and not project_id:
                    user_projects = await get_user_projects(credentials)
                    if not user_projects:
                        return {"success": False, "error": "No projects found"}
                    
                    if len(user_projects) == 1:
                        project_id = user_projects[0].get("projectId")
                    else:
                        project_id = await select_default_project(user_projects)
                        if not project_id:
                             return {
                                "success": False, 
                                "error": "Multiple projects found",
                                "requires_project_selection": True,
                                "available_projects": user_projects
                             }
                
                if not project_id:
                    return {"success": False, "error": "Missing Project ID"}
                
                await enable_required_apis(credentials, project_id)
                saved_filename = await self.save_credentials(credentials, project_id)
                
                # Prepare response
                creds_data = {
                    "token": credentials.access_token,
                    "project_id": project_id,
                    "expires_at": credentials.expires_at.isoformat() if credentials.expires_at else None
                }
                
                # Cleanup
                self._shutdown_flow_server(state)
                del self.auth_flows[state]
                
                return {
                    "success": True,
                    "credentials": creds_data,
                    "file_path": saved_filename
                }
                
            except Exception as e:
                log.error(f"Exchange failed: {e}")
                return {"success": False, "error": str(e)}

        except Exception as e:
            log.error(f"Complete flow failed: {e}")
            return {"success": False, "error": str(e)}

    async def auto_load_env_credentials(self):
        """Load credentials from environment variables"""
        env_creds = {}
        for key, value in os.environ.items():
            if key.startswith("GCLI_CREDS_"):
                try:
                    cred_name = key.replace("GCLI_CREDS_", "")
                    cred_data = json.loads(value)
                    env_creds[cred_name] = cred_data
                except Exception as e:
                    log.error(f"Failed to parse env cred {key}: {e}")

        if not env_creds:
            return

        storage_adapter = await get_storage_adapter()
        for name, data in env_creds.items():
            filename = f"env-{name}.json"
            await storage_adapter.store_credential(filename, data)
            log.info(f"Loaded env credential: {filename}")

# Global instance getter
auth_service = AuthService.get_instance()
