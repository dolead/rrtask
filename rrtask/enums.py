from enum import Enum


class State(Enum):
    STARTING = "STARTING"
    THROTTLED = "THROTTLED"
    ERRORED = "ERRORED"
    SKIPPED = "SKIPPED"
    FINISHED = "FINISHED"
