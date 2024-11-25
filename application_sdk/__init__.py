from functools import wraps
from typing import Dict, Optional

import pandas as pd

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
            typename = None
            for name, arg in kwargs.items():
                arg = arg(self, *args)
                if isinstance(arg, Input):
                    fn_kwargs[name] = await arg.get_df(*args)
                elif isinstance(arg, Output):
                    fn_kwargs[name] = arg
                    outputs[name] = arg
                    typename = arg.typename
                else:
                    fn_kwargs[name] = arg

            async def process_ret(ret: Optional[Dict[str, pd.DataFrame]]):
                if not ret or not isinstance(ret, dict):
                    logger.info("Function did not return any data")
                    return

                for ret_name, ret_df in ret.items():
                    if ret_name not in outputs:
                        logger.warning(
                            f"Output {ret_name} not found but function returned data"
                        )
                        continue
                    await outputs[ret_name].write_df(ret_df)

            async def process_metadata():
                for out in outputs.values():
                    await out.write_metadata()

            # if the outputs are not passed, we'll let the main method decide the return value
            if not outputs:
                fn_kwargs["batch_input"] = batch_input(self, *args).get_df()
                fn_kwargs.update(*args)
                return await f(self, **fn_kwargs)

            # We'll get the data in chunks and write it to the outputs provided
            chunk_count = 0
            for df_batch in batch_input(self, *args).get_batched_df():
                fn_kwargs["batch_input"] = df_batch
                await process_ret(await f(self, **fn_kwargs))
                del fn_kwargs["batch_input"]
                chunk_count += 1

            # In the end, we'll write the metadata and return the chunk count to the caller method
            await process_metadata()
            return {"typename": typename, "chunk_count": chunk_count}

        return new_fn

    return decorator
