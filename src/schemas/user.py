from pydantic import BaseModel
from typing import Optional

class UserUpdateModel(BaseModel):
    quota_daily: Optional[int] = None
    disabled: Optional[bool] = None

class ChangePasswordRequest(BaseModel):
    new_password: str

class UsageLimitsUpdateRequest(BaseModel):
    filename: str
    gemini_2_5_pro_limit: Optional[int] = None
    total_limit: Optional[int] = None

class UsageResetRequest(BaseModel):
    filename: Optional[str] = None
