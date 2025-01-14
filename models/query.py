from typing import List, Optional

from pydantic import BaseModel

from models.ingest import EncoderEnum
from models.vector_database import VectorDatabase


class RequestPayload(BaseModel):
    input: str
    vector_database: VectorDatabase
    index_name: str
    encoder: EncoderEnum = EncoderEnum.openai


class ResponseData(BaseModel):
    content: str
    doc_url: str
    page_number: Optional[int]


class ResponsePayload(BaseModel):
    success: bool
    data: List[ResponseData]
