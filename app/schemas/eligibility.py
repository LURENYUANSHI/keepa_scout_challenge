"""Pydantic request model for app/routers/eligibility.py.

Response bodies are deliberately plain `dict`s, not a Pydantic
`response_model` -- `/eligibility/batch` mixes two different shapes per
item (a full eligibility record, or a `{"asin", "error"}` not-found marker;
see HARNESS.md §3), which doesn't map cleanly onto one static model without
a lossy union. The request side only needs the one shape below.
"""
from pydantic import BaseModel


class BatchEligibilityRequest(BaseModel):
    asins: list[str]
