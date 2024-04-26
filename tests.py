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

    def test_reschedule_lock(self):
        assert self.task.can_reschedule()
        assert self.task.can_reschedule(force=True)
        self.task.mark_for_scheduling("task id")
        assert not self.task.can_reschedule()
        assert self.task.can_reschedule(force=True)
        assert not self.task.can_reschedule()
        with mock.patch("rrtask.rrtask.current_task") as current_task:
            current_task.request.id = "task id"
            assert self.task.can_reschedule()
        self.redis.set("testing.FavoriteColor.lock", 1)
        assert not self.task.can_reschedule()
        assert self.task.can_reschedule(force=True)
