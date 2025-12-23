from pydantic import BaseModel
from typing import List

class CredFileActionRequest(BaseModel):
    filename: str
    action: str

class CredFileBatchActionRequest(BaseModel):
    action: str
    filenames: List[str]
