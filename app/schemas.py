from pydantic import BaseModel
from typing import Optional, List, Any

class UploadResponse(BaseModel):
    records_ingested: int
    issues: List[str]

class SummaryRow(BaseModel):
    scope: str
    category: str
    co2e: float

class SummaryResponse(BaseModel):
    total_co2e: float
    by_scope: List[SummaryRow]
    by_category: List[SummaryRow]
    notes: Optional[str] = None
