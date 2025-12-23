from fastapi import WebSocket
from collections import deque
from starlette.websockets import WebSocketState
import time
from log import log

class ConnectionManager:
    def __init__(self, max_connections: int = 3):  # 进一步降低最大连接数
        # 使用双端队列严格限制内存使用
        self.active_connections: deque = deque(maxlen=max_connections)
        self.max_connections = max_connections
        self._last_cleanup = 0
        self._cleanup_interval = 120  # 120秒清理一次死连接

    async def connect(self, websocket: WebSocket):
        # 自动清理死连接
        self._auto_cleanup()

        # 限制最大连接数，防止内存无限增长
        if len(self.active_connections) >= self.max_connections:
            await websocket.close(code=1008, reason="Too many connections")
            return False

        await websocket.accept()
        self.active_connections.append(websocket)
        log.debug(f"WebSocket连接建立，当前连接数: {len(self.active_connections)}")
        return True

    def disconnect(self, websocket: WebSocket):
        # 使用更高效的方式移除连接
        try:
            self.active_connections.remove(websocket)
        except ValueError:
            pass  # 连接已不存在
        log.debug(f"WebSocket连接断开，当前连接数: {len(self.active_connections)}")

    async def send_personal_message(self, message: str, websocket: WebSocket):
        try:
            await websocket.send_text(message)
        except Exception:
            self.disconnect(websocket)

    async def broadcast(self, message: str):
        # 使用更高效的方式处理广播，避免索引操作
        dead_connections = []
        for conn in self.active_connections:
            try:
                await conn.send_text(message)
            except Exception:
                dead_connections.append(conn)

        # 批量移除死连接
        for dead_conn in dead_connections:
            self.disconnect(dead_conn)

    def _auto_cleanup(self):
        """自动清理死连接"""
        current_time = time.time()
        if current_time - self._last_cleanup > self._cleanup_interval:
            self.cleanup_dead_connections()
            self._last_cleanup = current_time

    def cleanup_dead_connections(self):
        """清理已断开的连接"""
        original_count = len(self.active_connections)
        # 使用列表推导式过滤活跃连接，更高效
        alive_connections = deque(
            [
                conn
                for conn in self.active_connections
                if hasattr(conn, "client_state")
                and conn.client_state != WebSocketState.DISCONNECTED
            ],
            maxlen=self.max_connections,
        )

        self.active_connections = alive_connections
        cleaned = original_count - len(self.active_connections)
        if cleaned > 0:
            log.debug(f"清理了 {cleaned} 个死连接，剩余连接数: {len(self.active_connections)}")

# Global instance
manager = ConnectionManager()
