from pydantic import BaseModel


class BasicCredential(BaseModel):
    host: str
    port: int
    user: str
    password: str
    database: str
