import sqlite3
import hashlib
import secrets
import time
import os
import re
import asyncio
from typing import Optional, Tuple, Any, List, Dict
from log import log
from urllib.parse import urlparse, unquote

try:
    import aiomysql
    import pymysql
    import pymysql.cursors
    HAS_AIOMYSQL = True
except ImportError:
    HAS_AIOMYSQL = False

DB_PATH = os.getenv("USERS_DB_PATH", "users.db")

class UserManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        
        self._is_mysql = False
        self._mysql_config = {}
        self._pool = None
        
        # Check for MySQL configuration
        mysql_uri = os.getenv("MYSQL_URI") or os.getenv("MYSQL_DSN")
        if mysql_uri and HAS_AIOMYSQL:
            self._is_mysql = True
            self._mysql_config = self._parse_mysql_uri(mysql_uri)
            log.info(f"UserManager utilizing (Async) MySQL backend: {self._mysql_config.get('host')}:{self._mysql_config.get('port')}/{self._mysql_config.get('db')}")
        elif mysql_uri and not HAS_AIOMYSQL:
            log.warning("MYSQL_URI found but aiomysql is not installed. Falling back to SQLite.")
            
        self._initialized = True

    async def initialize(self):
        """Async initialization of database connection pool or schema"""
        if self._is_mysql:
            await self._init_mysql_pool()
        else:
            await self._init_sqlite_async()
            # Ensure default admin exists
            await self.create_admin_if_not_exists()

    def _parse_mysql_uri(self, uri: str) -> dict:
        """Parse MySQL URI into aiomysql connection parameters"""
        if uri.startswith("mysql+aiomysql://"):
             uri = uri.replace("mysql+aiomysql://", "mysql://")
             
        parsed = urlparse(uri)
        params = {
            "host": parsed.hostname or "localhost",
            "port": parsed.port or 3306,
            "user": parsed.username,
            "password": unquote(parsed.password) if parsed.password else None,
            "db": parsed.path.lstrip('/') or "gcli2api",
            "charset": "utf8mb4",
            "cursorclass": aiomysql.DictCursor,
            "autocommit": True
        }
        return params

    async def _init_mysql_pool(self):
        try:
            self._pool = await aiomysql.create_pool(**self._mysql_config)
            # Init schema
            await self._init_mysql_schema()
            await self.create_admin_if_not_exists()
        except Exception as e:
            log.error(f"Failed to initialize MySQL pool: {e}")
            raise e

    async def _init_mysql_schema(self):
        queries = [
            """
            CREATE TABLE IF NOT EXISTS users (
                id VARCHAR(64) PRIMARY KEY,
                username VARCHAR(255) UNIQUE NOT NULL,
                password_hash VARCHAR(255) NOT NULL,
                created_at DOUBLE,
                role VARCHAR(50) DEFAULT 'user',
                api_key VARCHAR(255) UNIQUE,
                quota_daily INTEGER DEFAULT 0,
                disabled BOOLEAN DEFAULT 0
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """,
            """
            CREATE TABLE IF NOT EXISTS tokens (
                token VARCHAR(255) PRIMARY KEY,
                user_id VARCHAR(64) NOT NULL,
                expires_at DOUBLE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            ) DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
            """
        ]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                for q in queries:
                    await cursor.execute(q)

    def _run_sqlite_sync(self, query: str, params: tuple):
        """Synchronous SQLite execution helper for running in executor"""
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(query, params)
            conn.commit()
            # Determine return based on query type or fetch strategy...
            # This simple wrapper is tricky because _execute expects different returns
            # So _execute does the heavy lifting, this just runs it.
            # But wait, cursor behavior differs.
            # Let's handle logic in _execute wrapper.
            pass
        
    def _sqlite_worker(self, query, params, fetch_one, fetch_all):
        """Worker function to run in thread pool for SQLite"""
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)
                if not query.strip().upper().startswith("SELECT"):
                    conn.commit()
                
                if fetch_one:
                    row = cursor.fetchone()
                    return dict(row) if row else None
                elif fetch_all:
                    rows = cursor.fetchall()
                    return [dict(row) for row in rows]
                else:
                    return cursor.lastrowid
        except Exception as e:
            log.error(f"SQLite Worker Error: {e}")
            raise e

    async def _init_sqlite_async(self):
        """Async wrapper for SQLite init"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._init_sqlite_sync)

    def _init_sqlite_sync(self):
        """Initialize SQLite tables and migrate schema if needed (Sync)"""
        with sqlite3.connect(DB_PATH) as conn:
            # Schema migration logic
            try:
                conn.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
            except sqlite3.OperationalError: pass
            try:
                conn.execute("ALTER TABLE users ADD COLUMN api_key TEXT")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key)")
            except sqlite3.OperationalError:
                try: conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key)")
                except: pass
            try:
                conn.execute("ALTER TABLE users ADD COLUMN quota_daily INTEGER DEFAULT 0")
            except sqlite3.OperationalError: pass
            try:
                conn.execute("ALTER TABLE users ADD COLUMN disabled INTEGER DEFAULT 0")
            except sqlite3.OperationalError: pass

            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    created_at REAL,
                    role TEXT DEFAULT 'user',
                    api_key TEXT UNIQUE,
                    quota_daily INTEGER DEFAULT 0,
                    disabled INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tokens (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    expires_at REAL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """)
            conn.commit()

    async def _execute(self, query: str, params: tuple = (), fetch_one=False, fetch_all=False) -> Any:
        try:
            if self._is_mysql:
                # Convert ? to %s for MySQL
                final_query = query.replace('?', '%s')
                async with self._pool.acquire() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(final_query, params)
                        if fetch_one:
                            return await cursor.fetchone()
                        if fetch_all:
                            return await cursor.fetchall()
                        return cursor.lastrowid
            else:
                # SQLite via ThreadPool
                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, self._sqlite_worker, query, params, fetch_one, fetch_all)

        except Exception as e:
            log.error(f"Async DB Error ({'MySQL' if self._is_mysql else 'SQLite'}): {e} | Query: {query}")
            raise e

    def _hash_password(self, password: str) -> str:
        salt = "gcli_static_salt" 
        return hashlib.sha256((password + salt).encode()).hexdigest()

    async def register(self, username, password, role="user") -> bool:
        try:
            user_id = secrets.token_hex(8)
            pwd_hash = self._hash_password(password)
            api_key = f"sk-gcli-{secrets.token_urlsafe(24)}"
            
            await self._execute(
                "INSERT INTO users (id, username, password_hash, created_at, role, api_key) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, username, pwd_hash, time.time(), role, api_key)
            )
            log.info(f"新用户注册: {username} (Role: {role})")
            return True
        except Exception as e:
            if "Duplicate entry" in str(e) or "UNIQUE constraint" in str(e):
                 return False
            log.error(f"注册失败: {e}")
            return False

    async def create_admin_if_not_exists(self):
        """Create default admin user if not exists, or ensure admin role"""
        row = await self._execute("SELECT id, role FROM users WHERE username=?", ('admin',), fetch_one=True)
        if not row:
            log.info("Creating default admin user...")
            await self.register("admin", "admin", role="admin")
        elif row['role'] != 'admin':
            log.warning("Admin user exists but has wrong role. Fixing...")
            await self._execute("UPDATE users SET role='admin' WHERE username=?", ('admin',))

    async def login(self, username, password) -> Optional[dict]:
        pwd_hash = self._hash_password(password)
        row = await self._execute(
            "SELECT id, role, api_key, disabled FROM users WHERE username=? AND password_hash=?",
            (username, pwd_hash),
            fetch_one=True
        )
        
        if row:
            user_id = row['id']
            disabled = bool(row['disabled'])
            
            if disabled:
                log.warning(f"Disabled user attempted login: {username}")
                return None

            token = secrets.token_urlsafe(32)
            expires_at = time.time() + (30 * 24 * 3600)
            
            await self._execute(
                "INSERT INTO tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
                (token, user_id, expires_at)
            )
            
            log.info(f"用户登录成功: {username}")
            return {
                "token": token, 
                "user_id": user_id,
                "role": row['role'],
                "api_key": row['api_key']
            }
        return None

    async def change_password(self, user_id, new_password) -> bool:
        pwd_hash = self._hash_password(new_password)
        try:
            await self._execute("UPDATE users SET password_hash=? WHERE id=?", (pwd_hash, user_id))
            return True
        except Exception as e:
            log.error(f"修改密码失败: {e}")
            return False

    async def delete_user(self, user_id: str) -> bool:
        admin_id = await self.get_user_by_username("admin")
        if user_id == admin_id:
             pass

        try:
             await self._execute("DELETE FROM tokens WHERE user_id=?", (user_id,))
             await self._execute("DELETE FROM users WHERE id=?", (user_id,))
             return True
        except Exception as e:
             log.error(f"Failed to delete user {user_id}: {e}")
             return False

    async def get_user_by_username(self, username: str) -> Optional[str]:
        row = await self._execute("SELECT id FROM users WHERE username=?", (username,), fetch_one=True)
        return row['id'] if row else None

    async def verify_token(self, token: str) -> Optional[str]:
        if not token:
            return None
        
        query = "SELECT t.user_id, t.expires_at, u.disabled FROM tokens t JOIN users u ON t.user_id = u.id WHERE t.token=?"
        row = await self._execute(query, (token,), fetch_one=True)
        
        if row:
            if bool(row['disabled']):
                log.warning(f"Disabled user attempted token auth: {row['user_id']}")
                return None
            if time.time() < row['expires_at']:
                return row['user_id']
            else:
                await self._execute("DELETE FROM tokens WHERE token=?", (token,))
        return None

    async def logout(self, token: str):
        await self._execute("DELETE FROM tokens WHERE token=?", (token,))

    async def get_user_role(self, user_id: str) -> str:
        row = await self._execute("SELECT role FROM users WHERE id=?", (user_id,), fetch_one=True)
        return row['role'] if row else "user"

    async def get_user_by_api_key(self, api_key: str) -> Optional[str]:
        row = await self._execute("SELECT id, disabled FROM users WHERE api_key=?", (api_key,), fetch_one=True)
        if row:
            if bool(row['disabled']):
                return None
            return row['id']
        return None

    async def get_user(self, user_id: str) -> Optional[dict]:
        row = await self._execute(
            "SELECT id, username, role, created_at, api_key, quota_daily, disabled FROM users WHERE id=?", 
            (user_id,), 
            fetch_one=True
        )
        if row:
            return {
                "id": row['id'], 
                "username": row['username'], 
                "role": row['role'], 
                "created_at": row['created_at'], 
                "api_key": row['api_key'],
                "quota_daily": row['quota_daily'] or 0, 
                "disabled": bool(row['disabled'])
            }
        return None

    async def list_users(self) -> list:
        rows = await self._execute(
            "SELECT id, username, role, created_at, quota_daily, disabled FROM users",
            fetch_all=True
        )
        users = []
        for row in rows:
            users.append({
                "id": row['id'],
                "username": row['username'],
                "role": row['role'],
                "created_at": row['created_at'],
                "quota_daily": row['quota_daily'] or 0,
                "disabled": bool(row['disabled'])
            })
        return users

    async def update_user_status(self, user_id: str, disabled: bool = None, quota_daily: int = None):
        updates = []
        params = []
        if disabled is not None:
            updates.append("disabled=?")
            params.append(1 if disabled else 0)
        if quota_daily is not None:
            updates.append("quota_daily=?")
            params.append(quota_daily)
        
        if updates:
            params.append(user_id)
            query = f"UPDATE users SET {', '.join(updates)} WHERE id=?"
            await self._execute(query, tuple(params))

    async def impersonate_user(self, user_id: str) -> Optional[dict]:
        row = await self._execute(
            "SELECT id, role, username, api_key FROM users WHERE id=?", 
            (user_id,), 
            fetch_one=True
        )
        if not row:
            return None
        
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + 3600
        
        await self._execute(
            "INSERT INTO tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at)
        )
        
        return {
            "token": token,
            "user_id": user_id,
            "role": row['role'], 
            "username": row['username'],
            "api_key": row['api_key']
        }

    async def create_user_token(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + (30 * 24 * 3600)
        await self._execute(
            "INSERT INTO tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
            (token, user_id, expires_at)
        )
        return token

    async def get_all_users(self) -> list:
        return await self.list_users()

    async def regenerate_api_key(self, user_id: str) -> str:
        new_key = f"sk-gcli-{secrets.token_urlsafe(24)}"
        await self._execute("UPDATE users SET api_key=? WHERE id=?", (new_key, user_id))
        return new_key

user_manager = UserManager()
