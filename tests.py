from unittest import TestCase, mock

import rrtask


class Redis:
    """Fake Redis class for local testing only"""

    def __init__(self):
        self._bdd = {}

    def get(self, key):
        return self._bdd.get(key)

    def set(self, key, value):
        self._bdd[key] = str(value).encode("utf8")

    def setnx(self, key, value):
        if key not in self._bdd:
            self.set(key, value)
            return True
        return False

    def incr(self, key):
        value = int(self._bdd.get(key, b"0")) + 1
        self.set(key, value)
        return value

    def delete(self, key):
        if key not in self._bdd:
            return 0
        del self._bdd[key]
        return 1

    def expire(self, key, expire):
        pass


class FavoriteColor(rrtask.RoundRobinTask):
    def recurring_task(self, color):
        if color in {"red", "blue"}:
            raise Exception("wrong")
        return True

    def reschedule_params(self):
        yield {"color": "red"}
        yield {"color": "blue"}
        yield {"color": "what's your favorite color"}


class RRTaskTest(TestCase):
    def setUp(self):
        rabbitmq_client_patch = mock.patch("rrtask.rrtask.get_rabbitmq_client")
        self.rabbitmq_client = rabbitmq_client_patch.start()
        self.rabbitmq_client.get_queue_depth.return_value = 0
        self.redis = Redis()
        self.celery = mock.Mock()
        self.task = FavoriteColor(self.celery, self.redis, "testing")

    def tearDown(self):
        mock.patch.stopall()

    def test_generation_dedup(self):
        task = self.task
        # Bootstrap: no live chain yet, a plain start adopts generation 1.
        assert task._claim_next_generation(None, force=False) == 1
        # The live scheduler (stamped with gen 1) advances to gen 2.
        assert task._claim_next_generation(1, force=False) == 2
        # A stale duplicate still carrying gen 1 is refused -> it skips.
        assert task._claim_next_generation(1, force=False) is None
        # The current live scheduler (gen 2) keeps the chain going.
        assert task._claim_next_generation(2, force=False) == 3
        # A force start always supersedes, whatever generation it carries.
        assert task._claim_next_generation(None, force=True) == 4
        # The chain that was live before the force (gen 3) is now stale.
        assert task._claim_next_generation(3, force=False) is None
        # ...while the forced chain (gen 4) continues normally.
        assert task._claim_next_generation(4, force=False) == 5

    def test_claim_serialized_by_lock(self):
        task = self.task
        # A concurrent claim holding the lock blocks any other claim,
        # even a forced one, so two schedulers cannot fork the chain.
        self.redis.set(task._lock_key, 1)
        assert task._claim_next_generation(None, force=False) is None
        assert task._claim_next_generation(None, force=True) is None
