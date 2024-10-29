import logging
import os
from typing import Any, List

import orjson

from application_sdk.paas.readers import ChunkedObjectStoreReaderInterface

logger = logging.getLogger(__name__)


class JSONChunkedObjectStoreReader(ChunkedObjectStoreReaderInterface):
    async def read_chunk(self, chunk: int) -> List[Any]:
        data: List[Any] = []
        async with self.lock:
            filename = os.path.join(
                self.local_file_path, f"{self.typename}-{chunk}.json"
            )
            with open(filename) as f:
                while True:
                    line = f.readline()
                    if line == "":
                        break

                    data.append(orjson.loads(line))
        return data
