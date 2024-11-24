from typing import Any, Callable

from fastapi import HTTPException


def requires(*requirements: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def req(fn: Callable[..., Any]) -> Callable[..., Any]:
        async def decorator(self: Any, *args, **kwargs) -> Any:
            missing = [
                req
                for req in requirements
                if not hasattr(self, req) or getattr(self, req) is None
            ]
            if missing:
                raise HTTPException(
                    status_code=500,
                    detail=f"Missing required attributes in the application: {', '.join(missing)}",
                )
            return await fn(self, *args, **kwargs)

        return decorator

    return req
