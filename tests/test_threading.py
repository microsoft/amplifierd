"""Tests for ThreadedToolWrapper and wrap_tools_for_threading."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from amplifierd.threading import ThreadedToolWrapper, wrap_tools_for_threading


class TestThreadedToolWrapper:
    async def test_execute_returns_tool_result(self):
        class FakeTool:
            name = "fake"
            description = "A fake tool"
            async def execute(self, input: dict[str, Any]) -> str:
                return f"result:{input['x']}"

        wrapper = ThreadedToolWrapper(FakeTool())
        result = await wrapper.execute({"x": 42})
        assert result == "result:42"

    async def test_execute_runs_off_event_loop(self):
        captured_loop_ids: list[int] = []

        class LoopSniffingTool:
            name = "sniffer"
            async def execute(self, input: dict[str, Any]) -> str:
                loop = asyncio.get_running_loop()
                captured_loop_ids.append(id(loop))
                return "done"

        main_loop = asyncio.get_running_loop()
        wrapper = ThreadedToolWrapper(LoopSniffingTool())
        await wrapper.execute({})
        assert len(captured_loop_ids) == 1
        assert captured_loop_ids[0] != id(main_loop)

    async def test_execute_propagates_exceptions(self):
        class ExplodingTool:
            name = "exploder"
            async def execute(self, input: dict[str, Any]) -> None:
                raise ValueError("kaboom")

        wrapper = ThreadedToolWrapper(ExplodingTool())
        with pytest.raises(ValueError, match="kaboom"):
            await wrapper.execute({})

    def test_getattr_proxies_tool_attributes(self):
        class RichTool:
            name = "rich-tool"
            description = "A tool with many attributes"
            input_schema = {"type": "object"}
            async def execute(self, input: dict[str, Any]) -> str:
                return ""

        wrapper = ThreadedToolWrapper(RichTool())
        assert wrapper.name == "rich-tool"
        assert wrapper.description == "A tool with many attributes"
        assert wrapper.input_schema == {"type": "object"}

    def test_repr(self):
        tool = MagicMock()
        tool.__repr__ = lambda self: "MockTool()"
        wrapper = ThreadedToolWrapper(tool)
        assert "ThreadedToolWrapper" in repr(wrapper)
        assert "MockTool()" in repr(wrapper)

    async def test_does_not_block_event_loop(self):
        class SlowTool:
            name = "slow"
            async def execute(self, input: dict[str, Any]) -> str:
                time.sleep(0.1)
                return "slow-done"

        wrapper = ThreadedToolWrapper(SlowTool())
        fast_completed = False

        async def fast_coro():
            nonlocal fast_completed
            await asyncio.sleep(0.01)
            fast_completed = True

        results = await asyncio.gather(wrapper.execute({}), fast_coro())
        assert results[0] == "slow-done"
        assert fast_completed is True


class TestWrapToolsForThreading:
    def test_wraps_all_tools(self):
        tool_a = MagicMock()
        tool_a.name = "tool-a"
        tool_b = MagicMock()
        tool_b.name = "tool-b"
        tools_dict = {"tool-a": tool_a, "tool-b": tool_b}
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=tools_dict)
        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        assert isinstance(tools_dict["tool-a"], ThreadedToolWrapper)
        assert isinstance(tools_dict["tool-b"], ThreadedToolWrapper)
        assert tools_dict["tool-a"]._tool is tool_a
        assert tools_dict["tool-b"]._tool is tool_b

    def test_noop_when_no_tools(self):
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=None)
        session = MagicMock()
        session.coordinator = coordinator
        wrap_tools_for_threading(session)  # Should not raise

    def test_noop_when_no_coordinator(self):
        session = MagicMock(spec=[])
        wrap_tools_for_threading(session)  # Should not raise

    def test_noop_when_tools_empty(self):
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value={})
        session = MagicMock()
        session.coordinator = coordinator
        wrap_tools_for_threading(session)  # Should not raise

    def test_noop_when_coordinator_has_no_get_method(self):
        """Coordinator may be a SimpleNamespace without a .get() method."""
        from types import SimpleNamespace

        coordinator = SimpleNamespace(hooks=None)
        session = MagicMock()
        session.coordinator = coordinator
        wrap_tools_for_threading(session)  # Should not raise
