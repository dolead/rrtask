from functools import lru_cache

from pyrabbit.api import Client  # type: ignore


@lru_cache()
def get_rabbitmq_client(host, user, passwd):
    return Client(host, user, passwd)
