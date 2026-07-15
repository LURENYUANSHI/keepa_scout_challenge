"""Pydantic request model for app/routers/chat.py."""
from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    message: str
