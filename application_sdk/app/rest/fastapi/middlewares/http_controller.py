from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


def http_controller(fn):
    async def decorator(self, request: Request):
        try:
            result = await fn(self, request)
            return JSONResponse(status_code=200, content=result.dict())
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content={"error": e.detail})
        except Exception as e:
            # Log
            return JSONResponse(status_code=500, content={"error": str(e)})

    return decorator
