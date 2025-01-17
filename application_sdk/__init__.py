import asyncio
import inspect
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Any, Callable, Dict, Iterator, List, Optional, Union

import daft
import pandas as pd

from application_sdk.activities import ActivitiesInterface
from application_sdk.common.logger_adaptors import AtlanLoggerAdapter
from application_sdk.inputs import Input

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


executor = ThreadPoolExecutor()


def is_empty_dataframe(df: Union[pd.DataFrame, daft.DataFrame]) -> bool:
    """
    Helper method to check if the dataframe has any rows
    """
    if isinstance(df, pd.DataFrame):
        return df.empty
    if isinstance(df, daft.DataFrame):
        return df.count_rows() == 0
    return True


def is_lambda_function(obj: Any) -> bool:
    """
    Helper method to check if a method is a lambda function
    """
    return inspect.isfunction(obj) and obj.__name__ == "<lambda>"


async def to_async(
    func: Callable, *args: Dict[str, Any], **kwargs: Dict[str, Any]
) -> Iterator[Union[pd.DataFrame, daft.DataFrame]]:
    """
    Wrapper method to convert a sync method to async
    Used to convert the input method that are sync to async and keep the logic consistent
    """
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    else:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(executor, func, *args, **kwargs)


async def _get_dataframe(
    input_obj: Input, get_dataframe_fn: Callable
) -> Union[pd.DataFrame, daft.DataFrame]:
    """
    Helper method to call the get_dataframe method of the input object
    """
    get_dataframe_fn = getattr(input_obj, get_dataframe_fn.__name__)
    return await to_async(get_dataframe_fn)


async def prepare_fn_kwargs(
    self,
    get_dataframe_fn: Callable,
    args: Dict[str, Any],
    inner_kwargs: Dict[str, Any],
    kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Helper method to prepare the kwargs for the function
    """
    fn_kwargs = dict(*args)
    fn_kwargs.update(inner_kwargs)
    for name, arg in kwargs.items():
        if is_lambda_function(arg):
            arg = arg(self, *args)
        if isinstance(arg, Input):
            fn_kwargs[name] = await _get_dataframe(arg, get_dataframe_fn)
        else:
            fn_kwargs[name] = arg
    return fn_kwargs


async def process_batch(
    self,
    f: Callable,
    df_batch: Union[pd.DataFrame, daft.DataFrame],
    fn_kwargs: Dict[str, Any],
) -> Optional[Dict]:
    """
    Helper method to process each batch and return the result
    """
    try:
        if not is_empty_dataframe(df_batch):
            fn_kwargs["batch_input"] = df_batch
            result = await f(self, **fn_kwargs)
            del fn_kwargs["batch_input"]
            return result
    except Exception as e:
        logger.error(f"Error processing batch: {str(e)}")
    return None


async def process_batches(
    self,
    batch_input_obj: Input,
    f: Callable,
    get_batch_dataframe_fn: Callable,
    fn_kwargs: Dict[str, Any],
) -> List[Dict]:
    """
    Method to loop over each batch of the dataframe and process it
    """
    try:
        rets = []
        df_batches = await _get_dataframe(batch_input_obj, get_batch_dataframe_fn)

        if inspect.isasyncgen(df_batches):
            async for df_batch in df_batches:
                result = await process_batch(
                    self=self, df_batch=df_batch, f=f, fn_kwargs=fn_kwargs
                )
                if result:
                    rets.append(result)
        else:
            for df_batch in df_batches:
                result = await process_batch(
                    self=self, df_batch=df_batch, f=f, fn_kwargs=fn_kwargs
                )
                if result:
                    rets.append(result)

        return rets
    except Exception as e:
        logger.error(f"Error processing batched data: {str(e)}")


async def run_process(
    self,
    f: Callable,
    get_dataframe_fn: Callable,
    get_batched_dataframe_fn: Callable,
    batch_input: Optional[Input],
    args: Dict[str, Any],
    inner_kwargs: Dict[str, Any],
    kwargs: Dict[str, Any],
    state: Dict[str, Any],
):            
    state = None
    if hasattr(self, "_get_state"):
        state = await self._get_state(args[0])

    fn_kwargs = await prepare_fn_kwargs(
        self=self,
        get_dataframe_fn=get_dataframe_fn,
        args=args,
        inner_kwargs=inner_kwargs,
        kwargs=kwargs,
        state=state,
    )

    # If batch_input is not provided, we'll call the function directly
    # since there is nothing to be read as the inputs
    if batch_input is None:
        return await f(self, **fn_kwargs)

    batch_input_obj = batch_input(self, *args, **fn_kwargs)
    # We'll decide whether to read the data in chunks or not based on the chunk_size attribute
    # If chunk_size is None, we'll read the data in one go
    if not hasattr(batch_input_obj, "chunk_size") or not batch_input_obj.chunk_size:
        fn_kwargs["batch_input"] = await _get_dataframe(
            input_obj=batch_input_obj, get_dataframe_fn=get_dataframe_fn
        )
        return await f(self, **fn_kwargs)

    # if chunk_size is set, we'll get the data in chunks and write it to the outputs provided
    return await process_batches(
        self=self,
        batch_input_obj=batch_input_obj,
        f=f,
        get_batch_dataframe_fn=get_batched_dataframe_fn,
        fn_kwargs=fn_kwargs,
    )


def activity_pd(batch_input: Optional[Input] = None, **kwargs):
    """
    Decorator to be used for activity functions that read data from a source and write to a sink
    It uses pandas dataframes as input and output
    """

    def decorator(f):
        @wraps(f)
        async def new_fn(self: ActivitiesInterface, *args, **inner_kwargs):
            return await run_process(
                self=self,
                f=f,
                batch_input=batch_input,
                get_dataframe_fn=Input.get_dataframe,
                get_batched_dataframe_fn=Input.get_batched_dataframe,
                args=args,
                inner_kwargs=inner_kwargs,
                kwargs=kwargs,
            )

        return new_fn

    return decorator


def activity_daft(batch_input: Optional[Input] = None, **kwargs):
    """
    Decorator to be used for activity functions that read data from a source and write to a sink
    It uses daft dataframes as input and output
    """

    def decorator(f):
        @wraps(f)
        async def new_fn(self, *args, **inner_kwargs):
            return await run_process(
                self=self,
                f=f,
                batch_input=batch_input,
                get_dataframe_fn=Input.get_daft_dataframe,
                get_batched_dataframe_fn=Input.get_batched_daft_dataframe,
                args=args,
                inner_kwargs=inner_kwargs,
                kwargs=kwargs,
            )

        return new_fn

    return decorator
