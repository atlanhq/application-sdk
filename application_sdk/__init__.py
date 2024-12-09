import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Optional

from application_sdk import logging
from application_sdk.inputs import Input
from application_sdk.outputs import Output

logger = logging.get_logger(__name__)


executor = ThreadPoolExecutor()


async def to_async(func, *args, **kwargs):
    """
    Wrapper method to convert a sync method to async
    Used to convert the input method that are sync to async and keep the logic consistent
    """
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    else:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(executor, func, *args, **kwargs)


def activity_pd(batch_input: Optional[Input] = None, **kwargs):
    def decorator(f):
        @wraps(f)
        async def new_fn(self, *args, **inner_kwargs):
            fn_kwargs = dict(*args)
            fn_kwargs.update(inner_kwargs)
            outputs = {}
            for name, arg in kwargs.items():
                arg = arg(self, *args)
                if isinstance(arg, Input):
                    fn_kwargs[name] = await arg.get_dataframe()
                elif isinstance(arg, Output):
                    fn_kwargs[name] = arg
                    outputs[name] = arg
                else:
                    fn_kwargs[name] = arg

            # If batch_input is not provided, we'll call the function directly
            # since there is nothing to be read as the inputs
            if batch_input is None:
                return await f(self, **fn_kwargs)

            batch_input_obj = batch_input(self, *args)
            # We'll decide whether to read the data in chunks or not based on the chunk_size attribute
            # If chunk_size is None, we'll read the data in one go
            if (
                not hasattr(batch_input_obj, "chunk_size")
                or not batch_input_obj.chunk_size
            ):
                fn_kwargs["batch_input"] = await to_async(batch_input_obj.get_dataframe)
                return await f(self, **fn_kwargs)

            # if chunk_size is set, we'll get the data in chunks and write it to the outputs provided
            rets = []
            df_batches = await to_async(batch_input_obj.get_batched_dataframe)

            for df_batch in df_batches:
                if len(df_batch) > 0:
                    fn_kwargs["batch_input"] = df_batch
                    rets.append(await f(self, **fn_kwargs))
                    del fn_kwargs["batch_input"]

            # In the end, we'll return the df to the caller method
            return rets

        return new_fn

    return decorator
