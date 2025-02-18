from pydantic import BaseModel


class Page(BaseModel):
    """
    A page of documentation.
    """

    id: str
    title: str
    content: str
    last_updated: str
    path: str
