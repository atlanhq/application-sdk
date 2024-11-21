from typing import Any, Callable

from fastapi import HTTPException, Request
from pydantic import BaseModel


def validation(
    request_model: BaseModel,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def validation(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
        async def decorator(self: Any, request: Request) -> Any:
            try:
                json_data = await request.json()
                request_body = request_model.model_validate(json_data)
                return await fn(
                    self,
                    body=request_body,
                    query_params=request.query_params,
                    headers=request.headers,
                    request=request,
                )
            except HTTPException as e:
                raise e
            except Exception as e:
                raise HTTPException(status_code=400, detail=str(e))

        return decorator

    return validation
