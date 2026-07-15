"""Pydantic request model for app/routers/ask.py."""
from pydantic import BaseModel


class AskRequest(BaseModel):
    question: str
