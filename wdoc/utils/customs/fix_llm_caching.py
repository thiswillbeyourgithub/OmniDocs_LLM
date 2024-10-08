"""
source : https://api.python.langchain.com/en/latest/_modules/langchain_community/cache.html#InMemoryCache

This workaround is to solve this: https://github.com/langchain-ai/langchain/issues/22389
Create a caching class that looks like it's just in memory but actually saves to sql

"""


import json
from pathlib import Path
from typing import Union, Any, Optional, Generator

from langchain_core.caches import BaseCache

from .sql_dict import SQLiteDict

class SQLiteCacheFixed(BaseCache):
    """Cache that stores things in memory using SQLiteDict."""

    def __init__(
        self,
        database_path: Union[str, Path],
        expiration_days: Optional[int] = 0,
        ) -> None:
        self.sd = SQLiteDict(
            database_path=database_path,
            expiration_days=expiration_days,
        )


    def lookup(self, prompt: str, llm_string: str) -> Any:
        """Look up based on prompt and llm_string."""
        key = json.dumps((prompt, llm_string))
        try:
            val = self.sd[key]
        except KeyError:
            return None
        return val


    def update(self, prompt: str, llm_string: str, return_val: Any) -> None:
        """Update cache based on prompt and llm_string."""
        key = json.dumps((prompt, llm_string))
        self.sd[key] = return_val

    def clear(self) -> None:
        raise NotImplementedError()
        # self.sd.clear()

    async def alookup(self, prompt: str, llm_string: str) -> Any:
        """Look up based on prompt and llm_string."""
        return self.lookup(prompt, llm_string)

    async def aupdate(
        self, prompt: str, llm_string: str, return_val: Any
    ) -> None:
        """Update cache based on prompt and llm_string."""
        self.update(prompt, llm_string, return_val)

    async def aclear(self) -> None:
        """Clear cache."""
        self.clear()

    def __get_keys__(self) -> Generator[str, None, None]:
        "get the list of keys present in the db"
        for k in self.sd.keys():
            yield k
