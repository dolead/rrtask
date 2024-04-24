from functools import lru_cache

from pyrabbit.api import Client  # type: ignore
from redis import Redis


@lru_cache()
def get_rabbitmq_client(host, user, passwd):
    return Client(host, user, passwd)


@lru_cache()
def get_redis_conn(host, database=10):
    return Redis(host=host, db=database)
