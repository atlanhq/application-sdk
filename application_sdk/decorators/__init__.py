import asyncio
import inspect
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, Optional, Tuple, Union

import pandas as pd

from application_sdk.activities import ActivitiesState
from application_sdk.common.logger_adaptors import get_logger
from application_sdk.inputs import Input

logger = get_logger(__name__)


executor = ThreadPoolExecutor()

if TYPE_CHECKING:
    import daft


async def to_async(
    func: Callable[..., Any], *args: Dict[str, Any], **kwargs: Dict[str, Any]
) -> Iterator[Union[pd.DataFrame, "daft.DataFrame"]]:  # noqa: F821
    """Convert a synchronous method to an asynchronous one.

    Args:
        func: The function to convert to async.
        *args: Positional arguments to pass to the function.
        **kwargs: Keyword arguments to pass to the function.

    Returns:
        An iterator containing either a pandas DataFrame or a daft DataFrame.
    """
    if inspect.iscoroutinefunction(func):
        return await func(*args, **kwargs)
    else:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(executor, func, *args, **kwargs)


async def _get_dataframe(
    input_obj: Input, get_dataframe_fn: Callable[..., Any]
) -> Union[pd.DataFrame, "daft.DataFrame"]:  # noqa: F821
    """Call the get_dataframe method of the input object.

    Args:
        input_obj: The Input object to get the dataframe from.
        get_dataframe_fn: The function to get the dataframe.

    Returns:
        Either a pandas DataFrame or a daft DataFrame.
    """
    get_dataframe_fn = getattr(input_obj, get_dataframe_fn.__name__)
    return await to_async(get_dataframe_fn)


async def prepare_fn_kwargs(
    self: Any,
    state: Optional[ActivitiesState],
    get_dataframe_fn: Callable[..., Any],
    get_batched_dataframe_fn: Callable[..., Any],
    kwargs: Dict[str, Any],
    fn_args: Tuple[Any, ...],
    fn_kwargs: Dict[str, Any],
) -> Dict[str, Any]:
    """Prepare the keyword arguments for the function.

    Args:
        self: The instance of the class.
        state: Optional state for activities.
        get_dataframe_fn: Function to get a single dataframe.
        get_batched_dataframe_fn: Function to get batched dataframes.
        kwargs: Keyword arguments to prepare.
        fn_args: Function arguments as a tuple.
        fn_kwargs: Function keyword arguments.

    Returns:
        Prepared keyword arguments for the function.
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
    """Run a function in a thread pool executor.

    Args:
        func: The function to run in thread pool.

    Returns:
        An async wrapper function that runs the input function in a thread pool.
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
    args: Tuple[Any, ...],
    kwargs: Dict[str, Any],
    fn_args: Tuple[Any, ...],
    fn_kwargs: Dict[str, Any],
) -> Any:
    """Process input data through a function.

    This method handles the execution of a function with input data processing.
    It supports both batch and non-batch processing modes, depending on the presence
    and configuration of batch_input.

    Args:
        fn: The workflow function to execute.
        get_dataframe_fn: Function to get a single dataframe.
        get_batched_dataframe_fn: Function to get batched dataframes.
        args: Positional arguments for the workflow function.
        kwargs: Additional keyword arguments for the workflow function.
        fn_args: Function arguments as a tuple.
        fn_kwargs: Function keyword arguments.

    Returns:
        The result of the workflow function execution. For batch processing,
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
        kwargs=kwargs,
        fn_args=fn_args,
        fn_kwargs=fn_kwargs,
    )
    if fn_self:
        return await fn(fn_self, **fn_kwargs)
    else:
        return await fn(**fn_kwargs)


def transform(*args: Any, **kwargs: Any):
    """Decorator that reads data from an Input source and returns data as a pandas DataFrame.

    This decorator handles reading data from an Input source and writing it to an Output.
    It supports both single and batched processing modes.

    Args:
        *args: Positional arguments to pass to the decorated function.
        **kwargs: Keyword arguments to pass to the decorated function.

    Returns:
        A decorator that wraps the function with the necessary processing.

    Examples:
        Read from SQL and write to JSON:
            >>> engine = sqlalchemy.create_engine("sqlite:///:memory:")
            >>> workflow_id = "322342798"
            >>> output_prefix = "/tmp/output/"
            >>> output_path = f"{output_prefix}{workflow_id}"
            >>> @transform(
            ...     batch_input=SQLQueryInput(engine=engine, query="SELECT * from table_a"),
            ...     output=JsonOutput(
            ...         output_prefix=output_prefix,
            ...         output_path=output_path,
            ...         output_suffix="/raw/table",
            ...         chunk_size=10,
            ...     )
            ... )
            ... async def fetch_table(batch_input: pd.DataFrame, output: JsonOutput, **kwargs):
            ...     # Write any custom logic to process the Dataframe here if required
            ...     custom_process_logic(batch_input)
            ...     # Write the processed Dataframe to the output
            ...     await output.write_batched_dataframe(batch_input)
            >>>
            >>> await fetch_table()
            >>>
            >>> # In this case the data extracted from the sql source will be written to below path in a json file
            >>> # /tmp/output/322342798/raw/table/1.json
            >>> # If in case the total records fetched from query is 30 then data will be written in 3 chunks with 10 records each
            >>> # as we have specified chunk_size=10. Data will be structured as below
            >>> # /tmp/output/322342798/raw/table
            >>> #   - 1.json
            >>> #   - 2.json
            >>> #   - 3.json

        Class-based transformation:
            >>> # Read the data from a SQL source and write the raw and transformed data into a JSON file using classes.
            >>> # This showcases how to use the decorator if the method is part of a class.
            >>>
            >>> # Notice the use of the `sql_query` attribute in the class to input the query into the `SQLQueryInput`.
            >>> # In the case of a class attribute, the name of the attribute should be passed as a string.
            >>>
            >>> # When the decorator runs, it'll re-initialize the `SQLQueryInput` with the correct value of the `sql_query` attribute.
            >>> # Also, when the arguments are not static and are dynamic, the arguments can be passed to the method as kwargs.
            >>>
            >>> class DemoActivity:
            ...     sql_query = "SELECT * from table_a"
            ...
            ...     async def run(self):
            ...         workflow_id = "322342798"
            ...         output_prefix = "/tmp/output/"
            ...         args = {
            ...             "output_prefix": output_prefix,
            ...             "output_path": f"{output_prefix}{workflow_id}"
            ...         }
            ...         await self.transform_table(args)
            ...
            ...     @transform(
            ...         batch_input=SQLQueryInput(engine=sqlalchemy.create_engine("sqlite:///:memory:"), query="sql_query"),
            ...         raw_output=JsonOutput(output_suffix="/raw/"),
            ...         transformed_output=JsonOutput(output_suffix="/transformed/")
            ...     )
            ...     async def transform_table(self, batch_input: pd.DataFrame, raw_output: JsonOutput, transformed_output: JsonOutput, **kwargs):
            ...         await raw_output.write_batched_dataframe(batch_input)
            ...         transformer(batch_input)
            ...         await transformed_output.write_batched_dataframe(batch_input)
    """

    def wrapper(fn: Callable[..., Any]):
        @wraps(fn)
        async def inner(*fn_args: Any, **fn_kwargs: Any):
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


def transform_daft(*args: Any, **kwargs: Any):
    """Decorator that reads data from an Input source and returns data as a daft DataFrame.

    This decorator handles reading data from an Input source and writing it to an Output.
    It supports both single and batched processing modes.

    Args:
        *args: Positional arguments to pass to the decorated function.
        **kwargs: Keyword arguments to pass to the decorated function.

    Returns:
        A decorator that wraps the function with the necessary processing.

    Examples:
        Read from SQL and write to JSON:
            >>> engine = sqlalchemy.create_engine("sqlite:///:memory:")
            >>> workflow_id = "322342798"
            >>> output_prefix = "/tmp/output/"
            >>> output_path = f"{output_prefix}{workflow_id}"
            >>> @transform_daft(
            ...     batch_input=SQLQueryInput(engine=engine, query="SELECT * from table_a"),
            ...     output=JsonOutput(
            ...         output_prefix=output_prefix,
            ...         output_path=output_path,
            ...         output_suffix="/raw/table",
            ...         chunk_size=10,
            ...     )
            ... )
            ... async def fetch_table(batch_input: daft.DataFrame, output: JsonOutput, **kwargs):
            ...     # Write any custom logic to process the Dataframe here if required
            ...     custom_process_logic(batch_input)
            ...     # Write the processed Dataframe to the output
            ...     await output.write_batched_dataframe(batch_input)
            >>>
            >>> await fetch_table()
            >>>
            >>> # In this case the data extracted from the sql source will be written to below path in a json file
            >>> # /tmp/output/322342798/raw/table/1.json
            >>>
            >>> # If in case the total records fetched from query is 30 then data will be written in 3 chunks with 10 records each
            >>> # as we have specified chunk_size=10. Data will be structured as below
            >>> # /tmp/output/322342798/raw/table
            >>> #   - 1.json
            >>> #   - 2.json
            >>> #   - 3.json

        Class-based transformation:
            >>> # Read the data from a SQL source and write the raw and transformed data into a JSON file using classes.
            >>> # This showcases how to use the decorator if the method is part of a class.
            >>>
            >>> # Notice the use of the `sql_query` attribute in the class to input the query into the `SQLQueryInput`.
            >>> # In the case of a class attribute, the name of the attribute should be passed as a string.
            >>>
            >>> # When the decorator runs, it'll re-initialize the `SQLQueryInput` with the correct value of the `sql_query` attribute.
            >>> # Also, when the arguments are not static and are dynamic, the arguments can be passed to the method as kwargs.
            >>>
            >>> class DemoActivity:
            ...     sql_query = "SELECT * from table_a"
            ...
            ...     async def run(self):
            ...         workflow_id = "322342798"
            ...         output_prefix = "/tmp/output/"
            ...         args = {
            ...             "output_prefix": output_prefix,
            ...             "output_path": f"{output_prefix}{workflow_id}"
            ...         }
            ...         await self.transform_table(args)
            ...
            ...     @transform_daft(
            ...         batch_input=SQLQueryInput(engine=sqlalchemy.create_engine("sqlite:///:memory:"), query="sql_query"),
            ...         raw_output=JsonOutput(output_suffix="/raw/"),
            ...         transformed_output=JsonOutput(output_suffix="/transformed/")
            ...     )
            ...     async def transform_table(self, batch_input: daft.DataFrame, raw_output: JsonOutput, transformed_output: JsonOutput, **kwargs):
            ...         await raw_output.write_batched_daft_dataframe(batch_input)
            ...         transformer(batch_input)
            ...         await transformed_output.write_batched_daft_dataframe(batch_input)
    """

    def wrapper(fn):
        @wraps(fn)
        async def inner(*fn_args: Any, **fn_kwargs: Any):
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
