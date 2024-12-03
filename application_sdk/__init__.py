from functools import wraps
from typing import Optional

from application_sdk import logging
from application_sdk.inputs import Input
from application_sdk.outputs import Output

logger = logging.get_logger(__name__)


def activity_pd(batch_input: Optional[Input] = None, **kwargs):
    def decorator(f):
        @wraps(f)
        async def new_fn(self, *args):
            fn_kwargs = {}
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

            async def process_metadata():
                for out in outputs.values():
                    await out.write_metadata()

            batch_input_obj = batch_input(self, *args)
            # We'll decide whether to read the data in chunks or not based on the chunk_size attribute
            # If chunk_size is None, we'll read the data in one go
            if (
                not hasattr(batch_input_obj, "chunk_size")
                or not batch_input_obj.chunk_size
            ):
                fn_kwargs["batch_input"] = await batch_input_obj.get_dataframe()
                fn_kwargs.update(*args)
                return await f(self, **fn_kwargs)

            # if chunk_size is set, we'll get the data in chunks and write it to the outputs provided
            rets = []
            for df_batch in await batch_input_obj.get_batched_dataframe():
                fn_kwargs["batch_input"] = df_batch
                rets.append(await f(self, **fn_kwargs))
                del fn_kwargs["batch_input"]

            # In the end, we'll write the metadata and return the df to the caller method
            await process_metadata()
            return rets

        return new_fn

    return decorator
