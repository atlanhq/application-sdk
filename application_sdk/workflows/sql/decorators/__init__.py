from sqlalchemy.engine import Engine
import pandas as pd



def query_batch(engine: Engine, query: str, chunk_size: int = 10000):
    def decorator(f):
        with engine.connect() as conn:
            data_iter = pd.read_sql_query(
                query, conn, chunksize=chunk_size
            )

            async def new_f(*args, **kwargs):
                chunk_number = 0
                for chunk_df in data_iter:
                    await f(chunk_number, chunk_df, *args, **kwargs)
                    chunk_number += 1
                    del chunk_df
            return new_f
    return decorator


def query_write(engine: Engine, query: str, output_path: str, chunk_size: int = 10000):
    def decorator(f):
        @query_batch(engine, query, chunk_size)
        async def new_fn(chunk_number, chunk_df, *args, **kwargs):
            df: pd.DataFrame = await f(chunk_number, chunk_df, *args, **kwargs)
            if df is None:
                raise ValueError("Function must return a DataFrame")

            df.to_json(f"{output_path}/{chunk_number}.json", orient="records", lines=True)
        return new_fn
    return decorator
