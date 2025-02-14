import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Any, Callable, Dict, Iterator, Optional, Union

import pandas as pd

from application_sdk.activities import ActivitiesState
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs import Input

logger = get_logger(__name__)


executor = ThreadPoolExecutor()


async def to_async(
    func: Callable[..., Any], *args: Dict[str, Any], **kwargs: Dict[str, Any]
) -> Iterator[Union[pd.DataFrame, "daft.DataFrame"]]:  # noqa: F821
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
    input_obj: Input, get_dataframe_fn: Callable[..., Any]
) -> Union[pd.DataFrame, "daft.DataFrame"]:  # noqa: F821
    """
    Helper method to call the get_dataframe method of the input object
    """
    get_dataframe_fn = getattr(input_obj, get_dataframe_fn.__name__)
    return await to_async(get_dataframe_fn)


async def prepare_fn_kwargs(
    self: Any,
    state: Optional[ActivitiesState],
    get_dataframe_fn: Callable[..., Any],
    get_batched_dataframe_fn: Callable[..., Any],
    args: Dict[str, Any],
    kwargs: Dict[str, Any],
    fn_args: Dict[str, Any],
    fn_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Helper method to prepare the kwargs for the function
    """
    check_fn_args = fn_args and isinstance(fn_args[-1], Dict)
    for name, kwarg in kwargs.items():
        class_args: Dict[str, Any] = {}
        class_args["state"] = state
        class_args["parent_class"] = self
        class_args.update(kwarg.__dict__)
        if check_fn_args:
            class_args.update(fn_args[-1])
        class_instance = kwarg.re_init(**class_args)
        if isinstance(class_instance, Input) or issubclass(
            class_instance.__class__, Input
        ):
            # In case of Input classes, we'll return the dataframe from the get_dataframe method
            # we'll decide whether to read the data in chunks or not based on the chunk_size attribute
            # If chunk_size is None, we'll read the data in one go
            if (
                not hasattr(class_instance, "chunk_size")
                or not class_instance.chunk_size
            ):
                fn_kwargs[name] = await _get_dataframe(
                    input_obj=class_instance, get_dataframe_fn=get_dataframe_fn
                )
            else:
                # if chunk_size is set, we'll get the data in chunks and write it to the outputs provided
                fn_kwargs[name] = await _get_dataframe(
                    input_obj=class_instance, get_dataframe_fn=get_batched_dataframe_fn
                )

        else:
            # In case of output classes, we'll return the output class itself
            fn_kwargs[name] = class_instance
    if check_fn_args:
        fn_kwargs.update(fn_args[-1])
    return fn_kwargs


def run_sync(func):
    """Decorator to run a function in a thread pool executor.

    Args:
        func: The function to run in thread pool

    Returns:
        Wrapped async function that runs in thread pool
    """

    async def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor() as pool:
            return await loop.run_in_executor(pool, func, *args, **kwargs)

    return wrapper


async def run_process(
    fn: Callable[..., Any],
    get_dataframe_fn: Callable[..., Any],
    get_batched_dataframe_fn: Callable[..., Any],
    args: Dict[str, Any],
    kwargs: Dict[str, Any],
    fn_args: Dict[str, Any],
    fn_kwargs: Dict[str, Any],
) -> Any:
    """Process input data through a function.

    This method handles the execution of a function with input data processing.
    It supports both batch and non-batch processing modes, depending on the presence
    and configuration of batch_input.

    Args:
        self: The activity instance.
        f (Callable): The workflow function to execute.
        get_dataframe_fn (Callable): Function to get a single dataframe.
        get_batched_dataframe_fn (Callable): Function to get batched dataframes.
        batch_input (Optional[Input]): Input handler for batch processing. If None,
            the function will be called directly without batch processing.
        args (Dict[str, Any]): Positional arguments for the workflow function.
        inner_kwargs (Dict[str, Any]): Inner keyword arguments for the workflow function.
        kwargs (Dict[str, Any]): Additional keyword arguments for the workflow function.

    Returns:
        Any: The result of the workflow function execution. For batch processing,
            this will be a list of results from processing each batch.

    Note:
        If batch_input has a chunk_size attribute, the data will be processed in chunks.
        Otherwise, all data will be processed in a single operation.
    """
    state: Optional[ActivitiesState] = None
    fn_self = fn_args[0] if fn_args else None
    if fn_self and hasattr(fn_self, "_get_state"):
        state = await fn_self._get_state(fn_args[1])

    fn_kwargs = await prepare_fn_kwargs(
        self=fn_self,
        state=state,
        get_dataframe_fn=get_dataframe_fn,
        get_batched_dataframe_fn=get_batched_dataframe_fn,
        args=args,
        kwargs=kwargs,
        fn_args=fn_args,
        fn_kwargs=fn_kwargs,
    )
    if fn_self:
        return await fn(fn_self, **fn_kwargs)
    else:
        return await fn(**fn_kwargs)


def transform(*args: Dict[str, Any], **kwargs: Dict[str, Any]):
    """
    Decorator to be used for activity functions that read data from a source and write to a sink
    It uses pandas dataframes as input and output
    """

    def wrapper(fn: Callable[..., Any]):
        @wraps(fn)
        async def inner(*fn_args: Dict[str, Any], **fn_kwargs: Dict[str, Any]):
            return await run_process(
                fn=fn,
                get_dataframe_fn=Input.get_dataframe,
                get_batched_dataframe_fn=Input.get_batched_dataframe,
                args=args,
                kwargs=kwargs,
                fn_args=fn_args,
                fn_kwargs=fn_kwargs,
            )

        return inner

    return wrapper


def transform_daft(*args: Dict[str, Any], **kwargs: Dict[str, Any]):
    """
    Decorator to be used for activity functions that read data from a source and write to a sink
    It uses daft dataframes as input and output
    """

    def wrapper(fn):
        @wraps(fn)
        async def inner(*fn_args: Dict[str, Any], **fn_kwargs: Dict[str, Any]):
            return await run_process(
                fn=fn,
                get_dataframe_fn=Input.get_daft_dataframe,
                get_batched_dataframe_fn=Input.get_batched_daft_dataframe,
                args=args,
                kwargs=kwargs,
                fn_args=fn_args,
                fn_kwargs=fn_kwargs,
            )

        return inner

    return wrapper
