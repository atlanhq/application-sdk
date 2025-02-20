from pydantic import BaseModel


class Page(BaseModel):
    """A page of documentation.

    Attributes:
        id (str): The unique identifier for the page.
        title (str): The title of the page.
        content (str): The content of the page.
        last_updated (str): The last updated date of the page.
        path (str): The path to the page.
    """

    id: str
    title: str
    content: str
    last_updated: str
    path: str
