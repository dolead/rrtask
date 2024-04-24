from enum import Enum


class State(Enum):
    UNKNOWN = "UNKNOWN"
    STARTING = "STARTING"
    THROTTLED = "THROTTLED"
    ERRORED = "ERRORED"
    SKIPPED = "SKIPPED"
    FINISHED = "FINISHED"
