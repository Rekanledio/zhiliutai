from dataclasses import dataclass
from datetime import datetime


@dataclass
class WatcherState:
    running: bool = False
    last_heartbeat_at: datetime | None = None
    last_error: str | None = None


watcher_state = WatcherState()
