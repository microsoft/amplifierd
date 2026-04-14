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

# Tools whose execute() does blocking synchronous I/O.
# Session-spawning tools (delegate, task, recipes) and other async-safe tools
# (bash, web_search, todo, mode) are deliberately NOT listed — they must run
# on the caller's event loop to avoid cross-loop crashes with PyO3 Rust types.
_NEEDS_THREADING = frozenset(
    {
        "grep",
        "glob",
        "python_check",
        "read_file",
        "write_file",
        "edit_file",
        "apply_patch",
        "load_skill",
        "web_fetch",
    }
)


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
    """Wrap known-blocking tools with :class:`ThreadedToolWrapper`.

    Only tools whose names appear in ``_NEEDS_THREADING`` are wrapped.
    Tools that spawn child sessions (delegate, task, recipes) or are already
    async-safe (bash, web_search, todo, mode) run directly on the caller's
    event loop.

    Safe to call even when the session has no coordinator or no tools, and
    when the coordinator does not expose a ``.get()`` method (e.g. it is a
    ``types.SimpleNamespace``).

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

    get_fn = getattr(coordinator, "get", None)
    if get_fn is None or not callable(get_fn):
        log.debug("wrap_tools_for_threading: coordinator has no .get() method, skipping")
        return

    tools: Any = get_fn("tools")
    if not tools:
        log.debug("wrap_tools_for_threading: no tools found in coordinator, skipping")
        return

    if isinstance(tools, dict):
        wrapped_count = 0
        for key in list(tools):
            tool = tools[key]
            if isinstance(tool, ThreadedToolWrapper):
                continue  # idempotency guard — prevents double-wrapping
            tool_name = getattr(tool, "name", key)
            if tool_name in _NEEDS_THREADING:
                tools[key] = ThreadedToolWrapper(tool)
                wrapped_count += 1
        log.debug(
            "wrap_tools_for_threading: wrapped %d of %d tool(s)",
            wrapped_count,
            len(tools),
        )
    else:
        wrapped = []
        wrapped_count = 0
        for tool in tools:
            if isinstance(tool, ThreadedToolWrapper):
                wrapped.append(tool)
            elif getattr(tool, "name", "") in _NEEDS_THREADING:
                wrapped.append(ThreadedToolWrapper(tool))
                wrapped_count += 1
            else:
                wrapped.append(tool)
        coordinator["tools"] = wrapped
        log.debug(
            "wrap_tools_for_threading: wrapped %d of %d tool(s)",
            wrapped_count,
            len(wrapped),
        )
