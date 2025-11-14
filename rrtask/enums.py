from enum import Enum


class State(Enum):
    UNKNOWN = "UNKNOWN"
    STARTING = "STARTING"
    THROTTLED = "THROTTLED"
    ERRORED = "ERRORED"
    SKIPPED = "SKIPPED"
    FINISHED = "FINISHED"


class Routing:
    QUEUE_NAME = "QUEUE_NAME"
    ROUTING_KEY = "ROUTING_KEY"
