import os

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


def transform_query_results(engine: Engine, query: str, output_path: str, chunk_size: int = 10000):
    def decorator(f):
        os.makedirs(f"{output_path}/raw", exist_ok=True)
        os.makedirs(f"{output_path}/transformed", exist_ok=True)

        @query_batch(engine, query, chunk_size)
        async def new_fn(chunk_number, chunk_df, *args, **kwargs):
            df: pd.DataFrame = await f(chunk_number, chunk_df, *args, **kwargs)
            if df is None:
                raise ValueError("Function must return a DataFrame")

            chunk_df.to_json(f"{output_path}/raw/{chunk_number}.json", orient="records", lines=True)
            df.to_json(f"{output_path}/transformed/{chunk_number}.json", orient="records", lines=True)
        return new_fn
    return decorator


def incremental_query_batch(
        engine: Engine,
        query: str,
        marker_hash: str,
        chunk_size: int = 10000
):
    def decorator(f):
        # TODO: Fetch last saved marker using {marker_hash} from PaaS storage
        hashes = {}
        last_marker = hashes.get(marker_hash, 0)
        new_query = query.replace("{MARKER_VALUE}", str(last_marker))

        @query_batch(engine, new_query, chunk_size)
        async def new_fn(chunk_number, chunk_df, *args, **kwargs):
            await f(chunk_number, chunk_df, *args, **kwargs)

        return new_fn
    return decorator
