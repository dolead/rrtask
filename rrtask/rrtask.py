import logging
from typing import Generator, Optional, Union

from redis import Redis
from celery import Celery, current_task  # type: ignore
from pyrabbit.http import HTTPError  # type: ignore

from rrtask import signals
from rrtask.enums import State
from rrtask.utils import get_rabbitmq_client

logger = logging.getLogger(__name__)


class RoundRobinTask:
    shall_loop_in: Optional[Union[float, int]] = None
    _lock_expire = 10 * 60
    _encoding = "utf8"

    def __init__(
        self,
        celery: Celery,
        redis: Redis,
        queue_prefix: Optional[str] = None,
        celery_http_api_port: int = 15672,
    ):
        self._celery = celery
        self._redis = redis
        self._queue_prefix = queue_prefix
        self._celery_port = celery_http_api_port

        logger.info("[%s] Initializing celery tasks", self.queue_name)
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
        if self._queue_prefix is not None:
            return f"{self._queue_prefix}.{self.__class__.__name__}"
        return self.__class__.__name__

    @property
    def is_queue_empty(self) -> int:
        broker = self._celery.broker_connection()
        rabbitmq_client = get_rabbitmq_client(
            f"{broker.hostname}:{self._celery_port}",
            broker.userid,
            broker.password,
        )
        try:
            queue_depth = rabbitmq_client.get_queue_depth(
                broker.virtual_host, self.queue_name
            )
        except HTTPError as error:
            if getattr(error, "reason", "") == "Object Not Found":
                return True
            queue_depth = 1
        return queue_depth == 0

    @property
    def scheduler_key(self):
        return f"rrtask.{self.queue_name}.scheduler_id"

    def can_reschedule(self, force: bool = False) -> bool:
        lock_key = f"rrtask.{self.queue_name}.lock"
        if not self._redis.setnx(lock_key, 1) and not force:
            logger.debug("[%s] scheduling forbidden: locked")
            return False
        self._redis.expire(lock_key, self._lock_expire)
        try:
            scheduler_id = current_task.request.id.encode(self._encoding)
        except AttributeError:
            scheduler_id = None
        allowed = True
        existing_scheduler_id = self._redis.get(self.scheduler_key)
        if scheduler_id and existing_scheduler_id == scheduler_id:
            logger.debug("[%s] can reschedule: matching id", self.queue_name)
        elif existing_scheduler_id is None:
            logger.debug(
                "[%s] can reschedule: no registered scheduler",
                self.queue_name,
            )
        elif force:
            logger.debug("[%s] can reschedule: forcing", self.queue_name)
        elif self.is_queue_empty:
            logger.debug("[%s] can reschedule: empty queue", self.queue_name)
        else:
            logger.warning(
                "[%s] CANNOT reschedule: locked on %r",
                self.queue_name,
                existing_scheduler_id,
            )
            allowed = False
        self._redis.delete(lock_key)
        return allowed

    def mark_for_scheduling(self, schedule_id: str):
        self._redis.set(f"rrtask.{self.queue_name}.scheduler_id", schedule_id)

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
            if not self.can_reschedule(force):
                status = State.SKIPPED
                signals.task.send(current_task, status=status, **sigload)
                return status
            self._redis.delete(self.scheduler_key)

            # Push all other stuff in queue
            params_list = list(self.reschedule_params())
            task_count = len(params_list)
            delay_between_task = 0.0
            if self.shall_loop_in and params_list:
                delay_between_task = self.shall_loop_in / task_count
            logger.info(
                "[%s] Scheduling %d tasks", self.queue_name, task_count
            )
            for i, params in enumerate(params_list):
                self._recurring_task.apply_async(
                    kwargs=params,
                    countdown=int(i * delay_between_task) or None,
                )

            # push yourself
            logger.info("[%s] Enqueuing scheduler", self.queue_name)
            async_res = self._scheduler_task.apply_async(
                countdown=self.shall_loop_in or None
            )
            self.mark_for_scheduling(async_res.id)
            signals.task.send(current_task, status=State.FINISHED, **sigload)
            return State.FINISHED

        return __scheduler_task

    def start(self, force: bool = False):
        self._scheduler_task(force=force)
