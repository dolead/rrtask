import logging
from typing import Generator, Optional, Union

from redis import Redis
from celery import Celery, current_task  # type: ignore

from rrtask import signals
from rrtask.enums import State
from rrtask.utils import get_rabbitmq_client

logger = logging.getLogger(__name__)


class RoundRobinTask:
    shall_loop_in: Optional[Union[float, int]] = None
    _lock_expire = 10 * 60

    def __init__(
        self,
        celery: Celery,
        redis: Redis,
        queue_prefix: str = "",
    ):
        self._celery = celery
        self._redis = redis
        self._queue_prefix = queue_prefix

        logger.info("Registering %s", self.queue_name)
        self._recurring_task = self.__set_recuring_task()
        self._scheduler_task = self.__set_scheduling_task()

    def recurring_task(self, **kwd_params) -> Union[bool, State]:
        """This is the true task that will be executed by the semaphore.
        The task executing this method will be stored in self._recurring_task.
        """
        raise NotImplementedError("should be overridden")

    def reschedule_params(self) -> Generator[dict, None, None]:
        """This method should return an iterable. Each element of this iterable
        is a valid argument for the true task (aka self.recurring_task).
        """
        raise NotImplementedError("should be overridden")

    @property
    def queue_name(self):
        if self._queue_prefix:
            return f"{self._queue_prefix}.{self.__class__.__name__}"
        return self.__class__.__name__

    @property
    def is_queue_empty(self) -> int:
        rabbitmq_conn = self._celery.broker_connection()
        rabbitmq_client = get_rabbitmq_client(
            rabbitmq_conn.host, rabbitmq_conn.userid, rabbitmq_conn.password
        )
        queue_depth = rabbitmq_client.get_queue_depth(
            rabbitmq_conn.virtual_host, self.queue_name
        )
        return queue_depth == 0

    def is_rescheduling_allowed(self, force: bool = False) -> bool:
        lock_key = f"{self.queue_name}.lock"
        if not self._redis.setnx(lock_key, 1):
            return False
        self._redis.expire(lock_key, self._lock_expire)
        try:
            uuid = current_task.request.id
        except AttributeError:
            uuid = None
        if uuid and self._redis.delete(f"{self.queue_name}.{uuid}"):
            self._redis.delete(lock_key)
            return True
        if force:
            self._redis.delete(lock_key)
            return True
        if self.is_queue_empty:
            self._redis.delete(lock_key)
            return True
        return False

    def mark_for_scheduling(self, schedule_id: str):
        self._redis.set(f"{self.queue_name}.{schedule_id}", 1)

    def __set_recuring_task(self):
        task_name = f"{self.queue_name}.recurring_task"

        @self._celery.task(
            queue=self.queue_name, ignore_result=True, name=task_name
        )
        def __recurring_task(**kwd_params):
            sigload = {
                "task_name": task_name,
                "queue_name": self.queue_name,
                "task_kwargs": kwd_params,
            }
            signals.task.send(current_task, status=State.STARTING, **sigload)
            status = State.SKIPPED
            try:
                result = self.recurring_task(**kwd_params)
                if isinstance(result, State):
                    status = result
                elif result is True:
                    status = State.FINISHED
                else:
                    status = State.UNKNOWN
            except Exception:
                status = State.ERRORED
                raise
            signals.task.send(current_task, status=status, **sigload)
            return status.value

        return __recurring_task

    def __set_scheduling_task(self):
        task_name = f"{self.queue_name}.scheduler_task"

        @self._celery.task(
            queue=self.queue_name, ignore_result=True, name=task_name
        )
        def __scheduler_task(force: bool = False):
            sigload = {
                "task_name": task_name,
                "queue_name": self.queue_name,
                "force": force,
            }
            signals.task.send(current_task, status=State.STARTING, **sigload)
            if not self.is_rescheduling_allowed(force):
                status = State.SKIPPED
                signals.task.send(current_task, status=status, **sigload)
                return status

            # Push all other stuff in queue
            params_list = list(self.reschedule_params())
            task_count = len(params_list)
            delay_between_task = 0.0
            if self.shall_loop_in and params_list:
                delay_between_task = self.shall_loop_in / task_count
            logger.info(
                "Scheduling %s for %d params",
                self.queue_name,
                task_count,
            )
            for i, params in enumerate(params_list):
                self._recurring_task.apply_async(
                    kwargs=params,
                    countdown=int(i * delay_between_task) or None,
                )

            # push yourself
            logger.info("Rescheduling %s", self.queue_name)
            async_res = self._scheduler_task.apply_async(
                countdown=self.shall_loop_in or None
            )
            self.mark_for_scheduling(async_res.id)
            signals.task.send(current_task, status=State.FINISHED, **sigload)
            return State.FINISHED

        return __scheduler_task

    def start(self):
        self._scheduler_task()
