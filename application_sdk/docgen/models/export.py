from typing import List

import pydantic


class Section(pydantic.BaseModel):
    id: str
    name: str
    content: str


class Page(pydantic.BaseModel):
    id: str
    title: str
    description: str
    sections: List[Section]
