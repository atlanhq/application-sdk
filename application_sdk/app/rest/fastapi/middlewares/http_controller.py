from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse


def http_controller(T):
    def request_handler(fn):
        async def wrapped_controller(self, request_dto: T, request_obj: Request):
            try:
                result = await fn(
                    self,
                    body=request_dto,
                    query_params=request_obj.query_params,
                    headers=request_obj.headers,
                    request=request_obj,
                )
                return JSONResponse(
                    status_code=status.HTTP_200_OK, content=result.dict()
                )
            except HTTPException as e:
                return JSONResponse(
                    status_code=e.status_code, content={"error": e.detail}
                )
            except Exception as e:
                # Log
                return JSONResponse(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    content={"error": str(e)},
                )

        return wrapped_controller

    return request_handler
