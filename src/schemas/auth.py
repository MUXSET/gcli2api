from pydantic import BaseModel
from typing import Optional

class LoginRequest(BaseModel):
    username: Optional[str] = "admin"
    password: str

class RegisterRequest(BaseModel):
    username: str
    password: str

class AuthStartRequest(BaseModel):
    project_id: Optional[str] = None
    get_all_projects: Optional[bool] = False

class AuthCallbackRequest(BaseModel):
    project_id: Optional[str] = None
    get_all_projects: Optional[bool] = False

class AuthCallbackUrlRequest(BaseModel):
    callback_url: str
    project_id: Optional[str] = None
    get_all_projects: Optional[bool] = False
