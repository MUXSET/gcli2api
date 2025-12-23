"""
MySQL数据库管理器，采用单行设计并兼容 UnifiedCacheManager。
实现与 postgres_manager.py 风格一致的接口（异步）。
需要环境变量: MYSQL_URI 或 MYSQL_DSN (例如: mysql://user:pass@host:port/dbname)
"""

import asyncio
import json
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiomysql

from log import log

from .cache_manager import CacheBackend, UnifiedCacheManager


class MySQLCacheBackend(CacheBackend):
    """MySQL缓存后端，数据存储为key, data(JSON), updated_at
    单行/单表设计：表名由管理器指定，每行以key区分。
    """

    def __init__(self, pool, table_name: str, row_key: str):
        self._pool = pool
        self._table_name = table_name
        self._row_key = row_key

    async def load_data(self) -> Dict[str, Any]:
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT data FROM {self._table_name} WHERE `key` = %s",
                        (self._row_key,)
                    )
                    row = await cur.fetchone()
                    if row and row[0] is not None:
                        data = row[0]
                        # JSON字段返回字符串，需要解析为字典
                        if isinstance(data, str):
                            return json.loads(data)
                        elif isinstance(data, dict):
                            return data
                        else:
                            log.warning(f"Unexpected data type from JSON field: {type(data)}")
                            return {}
                    return {}
        except Exception as e:
            log.error(f"Error loading data from MySQL row {self._row_key}: {e}")
            return {}

    async def write_data(self, data: Dict[str, Any]) -> bool:
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    json_data = json.dumps(data, default=str)
                    now = datetime.now(timezone.utc)
                    await cur.execute(
                        f"""INSERT INTO {self._table_name} (`key`, data, updated_at) 
                            VALUES (%s, %s, %s)
                            ON DUPLICATE KEY UPDATE data = VALUES(data), updated_at = VALUES(updated_at)""",
                        (self._row_key, json_data, now)
                    )
                    await conn.commit()
                    return True
        except Exception as e:
            log.error(f"Error writing data to MySQL row {self._row_key}: {e}")
            return False


class MySQLManager:
    """MySQL管理器。
    使用单表单行设计存储凭证和配置数据。
    """

    def __init__(self):
        self._pool: Optional[aiomysql.Pool] = None
        self._initialized = False
        self._lock = asyncio.Lock()

        self._uri = None
        self._table_name = "unified_storage"

        self._operation_count = 0

        self._operation_times = deque(maxlen=5000)

        self._credentials_cache_manager: Optional[UnifiedCacheManager] = None
        self._config_cache_manager: Optional[UnifiedCacheManager] = None

        self._credentials_row_key = "all_credentials"
        self._config_row_key = "config_data"

        self._write_delay = 1.0

    def _parse_mysql_uri(self, uri: str) -> dict:
        """解析 MySQL URI 为连接参数"""
        # 支持格式: mysql://user:pass@host:port/dbname
        # 或: mysql+aiomysql://user:pass@host:port/dbname
        import re
        
        pattern = r"mysql(?:\+aiomysql)?://(?:([^:]+):([^@]+)@)?([^:/]+)(?::(\d+))?/([^?]+)"
        match = re.match(pattern, uri)
        
        if not match:
            raise ValueError(f"Invalid MySQL URI format: {uri}")
        
        user, password, host, port, database = match.groups()
        
        return {
            "host": host or "localhost",
            "port": int(port) if port else 3306,
            "user": user or "root",
            "password": password or "",
            "db": database,
        }

    async def initialize(self):
        async with self._lock:
            if self._initialized:
                return
            try:
                self._uri = os.getenv("MYSQL_URI") or os.getenv("MYSQL_DSN")
                if not self._uri:
                    raise ValueError("MYSQL_URI or MYSQL_DSN environment variable is required")

                conn_params = self._parse_mysql_uri(self._uri)
                db_name = conn_params.pop("db", None)
                
                # Try to create database if it doesn't exist
                try:
                    # Connect without selecting a database
                    temp_conn = await aiomysql.connect(
                        **conn_params,
                        autocommit=True
                    )
                    async with temp_conn.cursor() as cur:
                        if db_name:
                            await cur.execute(f"CREATE DATABASE IF NOT EXISTS {db_name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                    temp_conn.close()
                except Exception as e:
                    log.warning(f"Failed to ensure database exists: {e}")
                    # Continue anyway, maybe the user has strict permissions and DB exists
                
                # Restore db name for pool creation
                if db_name:
                    conn_params["db"] = db_name

                self._pool = await aiomysql.create_pool(
                    **conn_params,
                    maxsize=20,
                    minsize=1,
                    autocommit=False,
                    charset="utf8mb4",
                )

                # 确保表存在
                await self._ensure_table()

                # 创建缓存管理器后端
                credentials_backend = MySQLCacheBackend(
                    self._pool, self._table_name, self._credentials_row_key
                )
                config_backend = MySQLCacheBackend(
                    self._pool, self._table_name, self._config_row_key
                )

                self._credentials_cache_manager = UnifiedCacheManager(
                    credentials_backend,
                    write_delay=self._write_delay,
                    name="credentials",
                )
                self._config_cache_manager = UnifiedCacheManager(
                    config_backend,
                    write_delay=self._write_delay,
                    name="config",
                )

                await self._credentials_cache_manager.start()
                await self._config_cache_manager.start()

                self._initialized = True
                log.info("MySQL connection established with unified cache")
            except Exception as e:
                log.error(f"Error initializing MySQL: {e}")
                raise

    async def _ensure_table(self):
        try:
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(f"""
                        CREATE TABLE IF NOT EXISTS {self._table_name} (
                            `key` VARCHAR(255) PRIMARY KEY,
                            data JSON,
                            updated_at DATETIME(6)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                    """)
                    await conn.commit()
        except Exception as e:
            log.error(f"Error ensuring MySQL table: {e}")
            raise

    async def close(self):
        if self._credentials_cache_manager:
            await self._credentials_cache_manager.stop()
        if self._config_cache_manager:
            await self._config_cache_manager.stop()
        if self._pool:
            self._pool.close()
            await self._pool.wait_closed()
            self._initialized = False
            log.info("MySQL connection closed with unified cache flushed")

    def _ensure_initialized(self):
        if not self._initialized:
            raise RuntimeError("MySQL manager not initialized")

    def _get_default_state(self) -> Dict[str, Any]:
        return {
            "error_codes": [],
            "disabled": False,
            "last_success": time.time(),
            "user_email": None,
        }

    def _get_default_stats(self) -> Dict[str, Any]:
        return {"call_timestamps": []}

    # 以下方法委托给 UnifiedCacheManager
    async def store_credential(self, filename: str, credential_data: Dict[str, Any]) -> bool:
        self._ensure_initialized()
        start_time = time.time()
        try:
            existing_data = await self._credentials_cache_manager.get(filename, {})
            credential_entry = {
                "credential": credential_data,
                "state": existing_data.get("state", self._get_default_state()),
                "stats": existing_data.get("stats", self._get_default_stats()),
            }
            success = await self._credentials_cache_manager.set(filename, credential_entry)
            self._operation_count += 1
            self._operation_times.append(time.time() - start_time)
            log.debug(f"Stored credential to unified cache (mysql): {filename}")
            return success
        except Exception as e:
            log.error(f"Error storing credential {filename} in MySQL: {e}")
            return False

    async def get_credential(self, filename: str) -> Optional[Dict[str, Any]]:
        self._ensure_initialized()
        try:
            credential_entry = await self._credentials_cache_manager.get(filename)
            self._operation_count += 1
            if credential_entry and "credential" in credential_entry:
                return credential_entry["credential"]
            return None
        except Exception as e:
            log.error(f"Error retrieving credential {filename} from MySQL: {e}")
            return None

    async def list_credentials(self) -> List[str]:
        self._ensure_initialized()
        try:
            all_data = await self._credentials_cache_manager.get_all()
            return list(all_data.keys())
        except Exception as e:
            log.error(f"Error listing credentials from MySQL: {e}")
            return []

    async def delete_credential(self, filename: str) -> bool:
        self._ensure_initialized()
        try:
            return await self._credentials_cache_manager.delete(filename)
        except Exception as e:
            log.error(f"Error deleting credential {filename} from MySQL: {e}")
            return False

    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any]) -> bool:
        self._ensure_initialized()
        try:
            existing_data = await self._credentials_cache_manager.get(filename, {})
            if not existing_data:
                existing_data = {
                    "credential": {},
                    "state": self._get_default_state(),
                    "stats": self._get_default_stats(),
                }
            existing_data["state"].update(state_updates)
            return await self._credentials_cache_manager.set(filename, existing_data)
        except Exception as e:
            log.error(f"Error updating credential state {filename} in MySQL: {e}")
            return False

    async def get_credential_state(self, filename: str) -> Dict[str, Any]:
        self._ensure_initialized()
        try:
            credential_entry = await self._credentials_cache_manager.get(filename)
            if credential_entry and "state" in credential_entry:
                return credential_entry["state"]
            return self._get_default_state()
        except Exception as e:
            log.error(f"Error getting credential state {filename} from MySQL: {e}")
            return self._get_default_state()

    async def get_all_credential_states(self) -> Dict[str, Dict[str, Any]]:
        self._ensure_initialized()
        try:
            all_data = await self._credentials_cache_manager.get_all()
            states = {
                fn: data.get("state", self._get_default_state()) for fn, data in all_data.items()
            }
            return states
        except Exception as e:
            log.error(f"Error getting all credential states from MySQL: {e}")
            return {}

    async def set_config(self, key: str, value: Any) -> bool:
        self._ensure_initialized()
        return await self._config_cache_manager.set(key, value)

    async def get_config(self, key: str, default: Any = None) -> Any:
        self._ensure_initialized()
        return await self._config_cache_manager.get(key, default)

    async def get_all_config(self) -> Dict[str, Any]:
        self._ensure_initialized()
        return await self._config_cache_manager.get_all()

    async def delete_config(self, key: str) -> bool:
        self._ensure_initialized()
        return await self._config_cache_manager.delete(key)

    async def update_usage_stats(self, filename: str, stats_updates: Dict[str, Any]) -> bool:
        self._ensure_initialized()
        try:
            existing_data = await self._credentials_cache_manager.get(filename, {})
            if not existing_data:
                existing_data = {
                    "credential": {},
                    "state": self._get_default_state(),
                    "stats": self._get_default_stats(),
                }
            existing_data["stats"].update(stats_updates)
            return await self._credentials_cache_manager.set(filename, existing_data)
        except Exception as e:
            log.error(f"Error updating usage stats for {filename} in MySQL: {e}")
            return False

    async def get_usage_stats(self, filename: str) -> Dict[str, Any]:
        self._ensure_initialized()
        try:
            credential_entry = await self._credentials_cache_manager.get(filename)
            if credential_entry and "stats" in credential_entry:
                return credential_entry["stats"]
            return self._get_default_stats()
        except Exception as e:
            log.error(f"Error getting usage stats for {filename} from MySQL: {e}")
            return self._get_default_stats()

    async def get_all_usage_stats(self) -> Dict[str, Dict[str, Any]]:
        self._ensure_initialized()
        try:
            all_data = await self._credentials_cache_manager.get_all()
            stats = {
                fn: data.get("stats", self._get_default_stats()) for fn, data in all_data.items()
            }
            return stats
        except Exception as e:
            log.error(f"Error getting all usage stats from MySQL: {e}")
            return {}

    # ============ 凭证顺序管理 ============

    async def get_credential_order(self) -> List[str]:
        """获取凭证轮换顺序"""
        self._ensure_initialized()

        try:
            # 从配置缓存中获取顺序
            order = await self._config_cache_manager.get("_credential_order", None)

            if order is None or not isinstance(order, list):
                # 如果没有保存的顺序，返回当前凭证列表作为默认顺序
                all_creds = await self.list_credentials()
                log.debug(f"No saved credential order, using default: {all_creds}")
                return all_creds

            log.debug(f"Loaded credential order: {order}")
            return order

        except Exception as e:
            log.error(f"Error getting credential order: {e}")
            return []

    async def set_credential_order(self, order: List[str]) -> bool:
        """设置凭证轮换顺序"""
        self._ensure_initialized()

        try:
            if not isinstance(order, list):
                log.error(f"Invalid credential order type: {type(order)}")
                return False

            # 保存顺序到配置缓存
            success = await self._config_cache_manager.set("_credential_order", order)

            if success:
                log.debug(f"Saved credential order: {order}")
            else:
                log.error("Failed to save credential order")

            return success

        except Exception as e:
            log.error(f"Error setting credential order: {e}")
            return False
