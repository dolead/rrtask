import logging
from datetime import datetime
from typing import Generator, Optional, Union
from uuid import uuid4

from celery import Celery, current_task  # type: ignore

from rrtask import signals
from rrtask.enums import State
from rrtask.utils import get_rabbitmq_client, get_redis_conn

logger = logging.getLogger(__name__)


class RoundRobinTask:
    shall_loop_in: Optional[Union[float, int]] = None

    def __init__(
        self,
        celery_app: Celery,
        redis_host: str,
        redis_db: int = 10,
        queue_prefix: str = "",
    ):
        self._celery_app = celery_app
        self._redis_conn = get_redis_conn(redis_host, redis_db)
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

    def is_queue_empty(self) -> int:
        rabbitmq_conn = self._celery_app.broker_connection()
        rabbitmq_client = get_rabbitmq_client(
            rabbitmq_conn.host, rabbitmq_conn.userid, rabbitmq_conn.password
        )
        queue_depth = rabbitmq_client.get_queue_depth(
            rabbitmq_conn.virtual_host, self.queue_name
        )
        return queue_depth == 0

    def __set_recuring_task(self):
        task_name = f"{self.queue_name}.recurring_task"

        @self._celery_app.task(
            queue=self.queue_name,
            ignore_result=True,
            name=task_name,
        )
        def __recurring_task(**kwd_params):
            sigload = {
                "task_name": task_name,
                "queue_name": self.queue_name,
                "task_kwargs": kwd_params,
            }
            signals.task.send(current_task, status=State.STARTING, **sigload)
            task_state = State.SKIPPED
            try:
                result = self.recurring_task(**kwd_params)
                if isinstance(result, State):
                    task_state = result
                elif result is True:
                    task_state = State.FINISHED
            except Exception:
                task_state = State.ERRORED
                raise
            signals.task.send(current_task, status=task_state, **sigload)
            return task_state.value

        return __recurring_task

    def is_rescheduling_allowed(
        self, task_name: str, uuid: Optional[str], force: bool
    ) -> bool:
        slot_freed = False
        if uuid:
            slot_freed = bool(self._redis_conn.delete(f"{task_name}.{uuid}"))
            if slot_freed:
                return True
        if force:
            return True
        if self.is_queue_empty():
            return True
        return False

    def mark_for_scheduling(self, task_name: str, uuid: str):
        self._redis_conn.set(
            f"{task_name}.{uuid}", datetime.utcnow().isoformat()
        )

    def __set_scheduling_task(self):
        task_name = f"{self.queue_name}.scheduler_task"

        @self._celery_app.task(
            queue=self.queue_name,
            ignore_result=True,
            name=task_name,
        )
        def __scheduler_task(
            schedule_id: Optional[str] = None, force: bool = False
        ):
            sigload = {
                "task_name": task_name,
                "queue_name": self.queue_name,
                "schedule_id": schedule_id,
                "force": force,
            }
            signals.task.send(current_task, status=State.STARTING, **sigload)
            if not self.is_rescheduling_allowed(task_name, schedule_id, force):
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
            reschedule_id = str(uuid4())
            self._scheduler_task.apply_async(
                args=[reschedule_id], countdown=self.shall_loop_in
            )
            self.mark_for_scheduling(task_name, reschedule_id)
            signals.task.send(current_task, status=State.FINISHED, **sigload)
            return State.FINISHED

        return __scheduler_task

    @property
    def queue_name(self):
        if self._queue_prefix:
            return f"{self._queue_prefix}.{self.__class__.__name__}"
        return self.__class__.__name__
