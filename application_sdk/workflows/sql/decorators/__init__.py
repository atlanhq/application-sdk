from sqlalchemy.engine import Engine
import pandas as pd



def stream_query_results(engine: Engine, query: str, chunk_size: int = 10000):
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
