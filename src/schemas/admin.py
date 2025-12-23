from pydantic import BaseModel
from typing import List, Optional

class ConfigSaveRequest(BaseModel):
    config: dict

class AnnouncementRequest(BaseModel):
    content: str
    level: str = "info"
    enabled: bool = True

class MigrateRequest(BaseModel):
    target_user_id: str

class ExportRequest(BaseModel):
    filenames: List[str]
    password: str

class BatchActionRequest(BaseModel): 
    action: str
    filenames: List[str]
