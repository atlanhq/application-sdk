from typing import Optional, Dict
from application_sdk.inputs import Input
from application_sdk.outputs import Output
import pandas as pd

from application_sdk import logging

logger = logging.get_logger(__name__)

def activity(batch_input: Optional[Input]=None, **kwargs):
    def decorator(f):
        async def new_fn():
            fn_kwargs = {}
            outs = {}
            for name, arg in kwargs.items():
                if isinstance(arg, Input):
                    fn_kwargs[name] = arg.get_df()
                elif isinstance(arg, Output):
                    fn_kwargs[name] = arg
                    outs[name] = arg
                else:
                    fn_kwargs[name] = arg

            def process_ret(ret: Optional[Dict[str, pd.DataFrame]]):
                if not ret or type(ret) != dict:
                    logger.info("Function did not return any data")
                    return

                for ret_name, ret_df in ret.items():
                    if ret_name not in outs:
                        logger.warning(f"Output {ret_name} not found but function returned data")
                        continue
                    outs[ret_name].write_df(ret_df)

            if not batch_input:
                process_ret(await f(**fn_kwargs))
                return

            for df in batch_input.get_batched_df():
                fn_kwargs['batch_input'] = df
                process_ret(await f(**fn_kwargs))
                del fn_kwargs['batch_input']

        return new_fn
    return decorator