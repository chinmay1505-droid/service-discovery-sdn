"""Thread-safe event bus that bridges sync registry threads → async WebSocket handlers.

Any thread calls emit(). The asyncio loop delivers events to all subscribed WebSocket queues.
"""
import asyncio
import datetime
import threading
import time
import uuid
from collections import deque

# Set by app.py at startup — the running asyncio event loop.
_loop: asyncio.AbstractEventLoop | None = None

# All active WebSocket subscriber queues.
_subscribers: set[asyncio.Queue] = set()
_lock = threading.Lock()

# Circular buffer of recent events (survives WS reconnects).
_history: deque = deque(maxlen=500)


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def emit(category: str, event: str, data: dict | None = None) -> dict:
    """
    Post one event from any thread.
    Returns the event dict (useful for tests / logging).
    """
    now = time.time()
    msg = {
        "id": uuid.uuid4().hex[:8],
        "ts": datetime.datetime.fromtimestamp(now).strftime("%H:%M:%S"),
        "ts_epoch": now,
        "category": category,   # REGISTRY | SDN | CLIENT | SERVER | SYSTEM | ERROR | PERF
        "event": event,          # e.g. REGISTERED, EXPIRED, ALLOW_FLOW_INSTALLED, ...
        "data": data or {},
    }
    _history.append(msg)
    if _loop is None or _loop.is_closed():
        return msg
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            _loop.call_soon_threadsafe(q.put_nowait, msg)
        except Exception:
            pass
    return msg


def subscribe() -> asyncio.Queue:
    """Return a new asyncio.Queue that will receive all future events."""
    q: asyncio.Queue = asyncio.Queue()
    with _lock:
        _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    with _lock:
        _subscribers.discard(q)


def get_history(n: int = 200) -> list[dict]:
    """Return up to n most-recent events (newest last)."""
    return list(_history)[-n:]

