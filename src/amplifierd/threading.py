"""ThreadedToolWrapper — run tool.execute() off the main event loop.

Uses the double-loop pattern:
  1. Create the coroutine eagerly on the calling (main event-loop) thread.
  2. Hand the coroutine to ``asyncio.run()`` executed inside a thread-pool
     worker via ``asyncio.to_thread()``.

This means each tool call gets its own tiny event loop spun up in a worker
thread, completely isolated from the main loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class ThreadedToolWrapper:
    """Transparent proxy that runs ``tool.execute()`` off the event loop."""

    def __init__(self, tool: Any) -> None:
        # Store under a mangled-style name to avoid clashing with the
        # proxied tool's own attributes.
        object.__setattr__(self, "_tool", tool)

    async def execute(self, input: Any) -> Any:  # noqa: A002
        """Run tool.execute(input) in a thread-pool worker with its own loop.

        The coroutine is created *eagerly* here on the calling thread so that
        any synchronous pre-flight code in the coroutine function runs on the
        correct thread, then the coroutine object is passed to ``asyncio.run``
        inside a worker thread.
        """
        tool = object.__getattribute__(self, "_tool")
        coro = tool.execute(input)
        log.debug("ThreadedToolWrapper: dispatching %s.execute to thread pool", tool)
        return await asyncio.to_thread(asyncio.run, coro)

    def __getattr__(self, name: str) -> Any:
        tool = object.__getattribute__(self, "_tool")
        return getattr(tool, name)

    def __repr__(self) -> str:
        tool = object.__getattribute__(self, "_tool")
        return f"ThreadedToolWrapper({tool!r})"


def wrap_tools_for_threading(session: Any) -> None:
    """Replace tools in *session*'s coordinator with :class:`ThreadedToolWrapper` instances.

    Safe to call even when the session has no coordinator or no tools.

    Typical usage::

        await session.initialize()
        wrap_tools_for_threading(session)

    or::

        session = await prepared.create_session()
        wrap_tools_for_threading(session)
    """
    coordinator = getattr(session, "coordinator", None)
    if coordinator is None:
        log.debug("wrap_tools_for_threading: session has no coordinator, skipping")
        return

    tools = coordinator.get("tools")
    if not tools:
        log.debug("wrap_tools_for_threading: no tools found in coordinator, skipping")
        return

    wrapped = [ThreadedToolWrapper(tool) for tool in tools]
    coordinator["tools"] = wrapped
    log.debug("wrap_tools_for_threading: wrapped %d tool(s)", len(wrapped))
