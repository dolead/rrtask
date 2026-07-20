import logging
from typing import Generator, Optional, Union

from celery import Celery, current_task  # type: ignore
from redis import Redis

from rrtask import signals
from rrtask.enums import Routing, State

logger = logging.getLogger(__name__)


class RoundRobinTask:
    shall_loop_in: Optional[Union[float, int]] = None
    # The lock only guards the (tiny) generation read-modify-write, so a
    # short expiry is enough and bounds how long a crashed holder can
    # stall rescheduling.
    _lock_expire = 60
    _encoding = "utf8"
    # Liveness heartbeat: refreshed every time a task of the chain runs.
    # Its TTL tracks the chain's own cadence -- roughly the spacing between
    # two consecutive recurring tasks (shall_loop_in / nb_of_task), padded
    # by a margin, and never below a floor. So a busy chain gets a tight
    # window regardless of how long shall_loop_in is, and a sparse one a
    # wider one; the window is short when tasks flow often and only grows
    # when they genuinely don't.
    _heartbeat_margin = 1.1
    _heartbeat_min_ttl = 60

    def __init__(
        self,
        celery: Celery,
        redis: Redis,
        queue_prefix: Optional[str] = None,
        routing_via=Routing.QUEUE_NAME,
    ):
        self._celery = celery
        self._redis = redis
        self._queue_prefix = queue_prefix
        self._routing_via = routing_via

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
    def is_queue_empty(self) -> bool:
        """Whether the chain looks dead: no task has refreshed the
        heartbeat within its liveness window.

        Named for the watchdog that consumes it. It no longer inspects the
        broker queue -- that signal was structurally wrong here: chains run
        with ROUTING_KEY and large countdowns, so their tasks are held in
        worker memory as ETA jobs and the broker queue reads empty even
        while the chain is perfectly alive.
        """
        return not self._redis.get(self._heartbeat_key)

    @property
    def _heartbeat_key(self):
        return f"rrtask.{self.queue_name}.heartbeat"

    @property
    def _heartbeat_ttl_key(self):
        return f"rrtask.{self.queue_name}.heartbeat_ttl"

    def _compute_heartbeat_ttl(self, task_count: int) -> int:
        """Size the liveness window to the chain's cadence and persist it.

        Recurring tasks refresh the heartbeat but don't know the batch
        size, so the scheduler stores the window it computed for them to
        reuse.
        """
        ttl = self._heartbeat_min_ttl
        if self.shall_loop_in:
            spacing = self.shall_loop_in / max(task_count, 1)
            ttl = int(spacing * self._heartbeat_margin)
        ttl = max(ttl, self._heartbeat_min_ttl)
        self._redis.set(self._heartbeat_ttl_key, ttl)
        return ttl

    def _beat(self, ttl: Optional[int] = None):
        """Refresh the liveness heartbeat. Falls back to the window the
        scheduler last stored (or the floor if the chain never ran)."""
        if ttl is None:
            stored = self._redis.get(self._heartbeat_ttl_key)
            ttl = int(stored) if stored else self._heartbeat_min_ttl
        self._redis.set(self._heartbeat_key, 1, ex=int(ttl))

    @property
    def _generation_key(self):
        return f"rrtask.{self.queue_name}.generation"

    @property
    def _lock_key(self):
        return f"rrtask.{self.queue_name}.lock"

    def _claim_next_generation(
        self, current_generation: Optional[int], force: bool
    ) -> Optional[int]:
        """Decide whether the calling scheduler may reschedule and, if so,
        reserve the next generation token.

        Deduplication does NOT depend on queue emptiness. Every scheduler
        is stamped with the generation it was scheduled under; only the
        holder of the currently live generation (or a forced/bootstrap
        start) may advance the counter. Stale duplicates fail the compare
        and skip, so forks collapse deterministically instead of being
        allowed to survive and multiply whenever the broker reports an
        (apparently) empty queue -- which it always does while the chain's
        tasks are held by workers as countdown/ETA jobs.

        Returns the freshly reserved generation, or None when the caller
        is a stale duplicate and must skip.
        """
        # Serialize the read-modify-write so two concurrent schedulers
        # carrying the same generation cannot both claim and fork.
        if not self._redis.setnx(self._lock_key, 1):
            logger.debug("[%s] claim skipped: busy", self.queue_name)
            return None
        self._redis.expire(self._lock_key, self._lock_expire)
        try:
            registered = self._redis.get(self._generation_key)
            if registered is not None:
                registered = int(registered)
            if force:
                reason = "forcing"
            elif registered is None:
                reason = "no live chain"
            elif (
                current_generation is not None
                and current_generation == registered
            ):
                reason = "live chain"
            else:
                logger.warning(
                    "[%s] CANNOT reschedule: stale generation %r (live %r)",
                    self.queue_name,
                    current_generation,
                    registered,
                )
                return None
            next_generation = self._redis.incr(self._generation_key)
            logger.debug(
                "[%s] claimed generation %d (%s)",
                self.queue_name,
                next_generation,
                reason,
            )
            return next_generation
        finally:
            self._redis.delete(self._lock_key)

    def __set_recuring_task(self):
        task_name = f"{self.queue_name}.recurring_task"
        task_kwargs = {"ignore_result": True, "name": task_name}
        if self._routing_via is Routing.QUEUE_NAME:
            task_kwargs["queue"] = self.queue_name

        @self._celery.task(**task_kwargs)
        def __recurring_task(**kwd_params):
            sigload = {
                "task_name": task_name,
                "queue_name": self.queue_name,
                "task_kwargs": kwd_params,
            }
            signals.task.send(current_task, status=State.STARTING, **sigload)
            self._beat()
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

        task_kwargs = {"ignore_result": True, "name": task_name}
        apply_kwargs = {}
        if self._routing_via is Routing.QUEUE_NAME:
            task_kwargs["queue"] = self.queue_name
        elif self._routing_via is Routing.ROUTING_KEY:
            apply_kwargs["declare"] = []
            apply_kwargs["exchange"] = self._celery.conf.task_default_exchange
            apply_kwargs["routing_key"] = self.queue_name

        @self._celery.task(**task_kwargs)
        def __scheduler_task(
            generation: Optional[int] = None, force: bool = False
        ):
            sigload = {
                "task_name": task_name,
                "queue_name": self.queue_name,
                "force": force,
            }
            signals.task.send(current_task, status=State.STARTING, **sigload)
            next_generation = self._claim_next_generation(generation, force)
            if next_generation is None:
                status = State.SKIPPED
                signals.task.send(current_task, status=status, **sigload)
                return status

            # Secure chain continuity FIRST: enqueue our successor stamped
            # with the freshly claimed generation before the (potentially
            # large) fan-out, so a failure while building/queuing the batch
            # cannot leave the chain without a successor and kill it.
            logger.info("[%s] Enqueuing scheduler", self.queue_name)
            self._scheduler_task.apply_async(
                kwargs={"generation": next_generation},
                countdown=self.shall_loop_in or None,
                **apply_kwargs,
            )

            # Push all other stuff in queue
            params_list = list(self.reschedule_params())
            task_count = len(params_list)
            # Refresh liveness and (re)size the window now that we know how
            # many tasks this loop carries.
            self._beat(self._compute_heartbeat_ttl(task_count))
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
                    **apply_kwargs,
                )

            signals.task.send(current_task, status=State.FINISHED, **sigload)
            return State.FINISHED

        return __scheduler_task

    def start(self, force: bool = False, delay: bool = False):
        apply_kwargs = {}
        if self._routing_via is Routing.QUEUE_NAME:
            apply_kwargs["queue"] = self.queue_name
        elif self._routing_via is Routing.ROUTING_KEY:
            apply_kwargs["declare"] = []
            apply_kwargs["exchange"] = self._celery.conf.task_default_exchange
            apply_kwargs["routing_key"] = self.queue_name
        if delay:
            self._scheduler_task.apply_async(
                kwargs={"force": force}, **apply_kwargs
            )
        else:
            self._scheduler_task(force=force)
