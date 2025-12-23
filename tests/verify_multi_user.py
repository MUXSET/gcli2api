
import asyncio
import os
import sys
import json
import shutil
from unittest.mock import MagicMock, patch

# Mock missing modules BEFORE importing anything else
sys.modules["jwt"] = MagicMock()
sys.modules["oauthlib"] = MagicMock()
sys.modules["oauthlib.oauth2"] = MagicMock()
sys.modules["oauthlib.oauth2.rfc6749"] = MagicMock()
sys.modules["oauthlib.oauth2.rfc6749.parameters"] = MagicMock()
sys.modules["pypinyin"] = MagicMock()
sys.modules["toml"] = MagicMock()
sys.modules["redis"] = MagicMock()
sys.modules["asyncpg"] = MagicMock()
sys.modules["motor"] = MagicMock()
sys.modules["motor.motor_asyncio"] = MagicMock()
sys.modules["hypercorn"] = MagicMock()
sys.modules["hypercorn.asyncio"] = MagicMock()
sys.modules["hypercorn.config"] = MagicMock()

# Add root to path (remove src from path to avoid relative import issues)
sys.path.append(os.getcwd())

from fastapi.testclient import TestClient

# Mock config to use a temp directory for credentials
TEST_DIR = os.path.join(os.getcwd(), "test_data")
os.makedirs(TEST_DIR, exist_ok=True)

# Mock environment variables
os.environ["CREDENTIALS_DIR"] = TEST_DIR
os.environ["USERS_DB_PATH"] = os.path.join(TEST_DIR, "users.db")

# Import app after setting env vars
from web import app
from src.user_manager import user_manager

from src.storage_adapter import get_storage_adapter

client = TestClient(app)

def setup_module():
    # Clean up test dir
    if os.path.exists(TEST_DIR):
        try:
            shutil.rmtree(TEST_DIR)
        except Exception as e:
            print(f"Warning: Failed to clean up TEST_DIR in setup: {e}")
    os.makedirs(TEST_DIR, exist_ok=True)
    
    # Initialize user manager db
    import sqlite3
    conn = sqlite3.connect(os.environ["USERS_DB_PATH"])
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            created_at REAL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            expires_at REAL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
    """)
    conn.commit()
    conn.close()

def teardown_module():
    if os.path.exists(TEST_DIR):
        try:
            shutil.rmtree(TEST_DIR)
        except Exception as e:
            print(f"Warning: Failed to clean up TEST_DIR in teardown: {e}")

def test_multi_user_flow():
    print("Starting Multi-User Verification...")
    
    # 1. Register User 1
    print("1. Registering User 1...")
    resp = client.post("/auth/register", json={"username": "user1", "password": "password123"})
    assert resp.status_code == 200, f"Register failed: {resp.text}"
    
    # 2. Login User 1
    print("2. Logging in User 1...")
    resp = client.post("/auth/login", json={"username": "user1", "password": "password123"})
    assert resp.status_code == 200
    token1 = resp.json()["token"]
    print(f"   User 1 Token: {token1[:10]}...")

    # 3. Register & Login User 2
    print("3. Registering & Logging in User 2...")
    client.post("/auth/register", json={"username": "user2", "password": "password123"})
    resp = client.post("/auth/login", json={"username": "user2", "password": "password123"})
    token2 = resp.json()["token"]
    print(f"   User 2 Token: {token2[:10]}...")

    # 4. User 1 Uploads Credential
    print("4. User 1 Uploads Credential...")
    dummy_cred = {
        "client_id": "test_client_id",
        "client_secret": "test_client_secret",
        "refresh_token": "test_refresh_token",
        "token_uri": "https://oauth2.googleapis.com/token",
        "project_id": "project1"
    }
    files = {
        "files": ("cred1.json", json.dumps(dummy_cred), "application/json")
    }
    resp = client.post("/auth/upload", files=files, headers={"Authorization": f"Bearer {token1}"})
    assert resp.status_code == 200, f"Upload failed: {resp.text}"
    
    # 5. Verify User 1 sees the credential
    print("5. Verifying User 1 sees credential...")
    resp = client.get("/creds/status", headers={"Authorization": f"Bearer {token1}"})
    assert resp.status_code == 200
    # creds is a dict {filename: info}, not a list
    creds1 = resp.json().get("creds", {})
    filenames1 = list(creds1.keys())
    assert "cred1.json" in filenames1 or any("cred1" in f for f in filenames1), f"User 1 should see cred1.json, got {filenames1}"
    print("   User 1 sees credential.")

    # 6. Verify User 2 does NOT see the credential
    print("6. Verifying User 2 does NOT see credential...")
    resp = client.get("/creds/status", headers={"Authorization": f"Bearer {token2}"})
    creds2 = resp.json().get("creds", {})
    filenames2 = list(creds2.keys())
    assert not any("cred1" in f for f in filenames2), f"User 2 should NOT see cred1.json, got {filenames2}"
    print("   User 2 does not see credential.")

    # 7. User 2 Uploads Credential
    print("7. User 2 Uploads Credential...")
    dummy_cred2 = dummy_cred.copy()
    dummy_cred2["project_id"] = "project2"
    files = {
        "files": ("cred2.json", json.dumps(dummy_cred2), "application/json")
    }
    resp = client.post("/auth/upload", files=files, headers={"Authorization": f"Bearer {token2}"})
    assert resp.status_code == 200

    # 8. Verify User 1 does NOT see User 2's credential
    print("8. Verifying User 1 does NOT see User 2's credential...")
    resp = client.get("/creds/status", headers={"Authorization": f"Bearer {token1}"})
    creds1 = resp.json().get("creds", {})
    filenames1 = list(creds1.keys())
    assert not any("cred2" in f for f in filenames1), f"User 1 should NOT see cred2.json, got {filenames1}"
    print("   User 1 does not see User 2's credential.")

    # 9. Test Load Env Creds Isolation
    print("9. Testing Load Env Creds Isolation...")
    # Mock environment variables
    with patch.dict(os.environ, {"GCLI_CREDS_envtest": json.dumps(dummy_cred)}):
        # User 1 loads env creds
        resp = client.post("/auth/load-env-creds", headers={"Authorization": f"Bearer {token1}"})
        assert resp.status_code == 200
        
        # Verify User 1 sees it
        resp = client.get("/creds/status", headers={"Authorization": f"Bearer {token1}"})
        creds1 = resp.json().get("creds", {})
        filenames1 = list(creds1.keys())
        assert any("env-" in f for f in filenames1), f"User 1 should see env cred, got {filenames1}"
        
        # Verify User 2 does NOT see it
        resp = client.get("/creds/status", headers={"Authorization": f"Bearer {token2}"})
        creds2 = resp.json().get("creds", {})
        filenames2 = list(creds2.keys())
        assert not any("env-" in f for f in filenames2), f"User 2 should NOT see env cred, got {filenames2}"
    
    print("   Env creds isolation verified.")

    print("\nAll verification steps passed!")

if __name__ == "__main__":
    try:
        setup_module()
        test_multi_user_flow()
    finally:
        teardown_module()
