from pydantic import BaseModel, Field


class BasicCredential(BaseModel):
    host: str
    port: int
    user: str
    password: str
    database: str


class CredentialPayload(BaseModel):
    host: str
    port: int
    user_name: str = Field(..., alias="userName")
    password: str
    database: str

    class Config:
        populate_by_name = True

    def get_credential_config(self) -> BasicCredential:
        return BasicCredential(
            host=self.host,
            port=self.port,
            database=self.database,
            user=self.user_name,
            password=self.password,
        )
