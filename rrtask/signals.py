import logging
from blinker import signal  # type: ignore
from rrtask.enums import State

logger = logging.getLogger(__name__)

task = signal("rrtask.task")


@task.connect
def log_task(
    current_task, task_name: str, queue_name: str, status: State, **kwargs
):
    logger.info(
        "[%s] %s %s",
        queue_name,
        task_name.split(".")[-1],
        status.value.lower(),
    )
