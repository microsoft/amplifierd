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
        class ReprTool:
            def __repr__(self) -> str:
                return "MockTool()"

        wrapper = ThreadedToolWrapper(ReprTool())
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
    def _make_tool(self, name: str) -> MagicMock:
        """Create a MagicMock tool with a .name attribute."""
        tool = MagicMock()
        tool.name = name
        return tool

    def test_wraps_only_blocking_tools_dict_path(self):
        """Only tools in _NEEDS_THREADING get wrapped (dict-keyed tools)."""
        grep_tool = self._make_tool("grep")
        delegate_tool = self._make_tool("delegate")
        bash_tool = self._make_tool("bash")
        read_file_tool = self._make_tool("read_file")

        tools_dict = {
            "grep": grep_tool,
            "delegate": delegate_tool,
            "bash": bash_tool,
            "read_file": read_file_tool,
        }
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=tools_dict)
        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        # Blocking tools: wrapped
        assert isinstance(tools_dict["grep"], ThreadedToolWrapper)
        assert tools_dict["grep"]._tool is grep_tool
        assert isinstance(tools_dict["read_file"], ThreadedToolWrapper)
        assert tools_dict["read_file"]._tool is read_file_tool

        # Async-safe tools: NOT wrapped
        assert not isinstance(tools_dict["delegate"], ThreadedToolWrapper)
        assert tools_dict["delegate"] is delegate_tool
        assert not isinstance(tools_dict["bash"], ThreadedToolWrapper)
        assert tools_dict["bash"] is bash_tool

    def test_wraps_only_blocking_tools_list_path(self):
        """Only tools in _NEEDS_THREADING get wrapped (list-based tools)."""
        glob_tool = self._make_tool("glob")
        task_tool = self._make_tool("task")
        recipes_tool = self._make_tool("recipes")
        python_check_tool = self._make_tool("python_check")

        tools_list = [glob_tool, task_tool, recipes_tool, python_check_tool]
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=tools_list)
        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        # coordinator["tools"] = wrapped_list was called
        coordinator.__setitem__.assert_called_once()
        key, wrapped = coordinator.__setitem__.call_args[0]
        assert key == "tools"
        assert len(wrapped) == 4

        # Blocking tools: wrapped
        assert isinstance(wrapped[0], ThreadedToolWrapper)  # glob
        assert wrapped[0]._tool is glob_tool
        assert isinstance(wrapped[3], ThreadedToolWrapper)  # python_check
        assert wrapped[3]._tool is python_check_tool

        # Async-safe tools: NOT wrapped
        assert not isinstance(wrapped[1], ThreadedToolWrapper)  # task
        assert wrapped[1] is task_tool
        assert not isinstance(wrapped[2], ThreadedToolWrapper)  # recipes
        assert wrapped[2] is recipes_tool

    def test_idempotency_dict_path(self):
        """Already-wrapped tools are not double-wrapped."""
        inner_tool = self._make_tool("grep")
        already_wrapped = ThreadedToolWrapper(inner_tool)
        tools_dict = {"grep": already_wrapped}
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=tools_dict)
        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        # Should still be the same wrapper, not double-wrapped
        assert tools_dict["grep"] is already_wrapped
        assert tools_dict["grep"]._tool is inner_tool

    def test_idempotency_list_path(self):
        """Already-wrapped tools in a list are not double-wrapped."""
        inner_tool = self._make_tool("glob")
        already_wrapped = ThreadedToolWrapper(inner_tool)
        tools_list = [already_wrapped]
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=tools_list)
        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        coordinator.__setitem__.assert_called_once()
        _, wrapped = coordinator.__setitem__.call_args[0]
        assert len(wrapped) == 1
        assert wrapped[0] is already_wrapped
        assert wrapped[0]._tool is inner_tool

    def test_all_needs_threading_tools_get_wrapped(self):
        """Every tool name in _NEEDS_THREADING is wrapped when present."""
        from amplifierd.threading import _NEEDS_THREADING

        tools_dict = {}
        raw_tools = {}
        for name in _NEEDS_THREADING:
            tool = self._make_tool(name)
            tools_dict[name] = tool
            raw_tools[name] = tool

        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=tools_dict)
        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        for name in _NEEDS_THREADING:
            assert isinstance(tools_dict[name], ThreadedToolWrapper), (
                f"Expected {name} to be wrapped"
            )
            assert tools_dict[name]._tool is raw_tools[name]

    def test_session_spawning_tools_never_wrapped(self):
        """delegate, task, recipes, bash, web_search, todo, mode stay unwrapped."""
        safe_names = ["delegate", "task", "recipes", "bash", "web_search", "todo", "mode"]
        tools_dict = {}
        raw_tools = {}
        for name in safe_names:
            tool = self._make_tool(name)
            tools_dict[name] = tool
            raw_tools[name] = tool

        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=tools_dict)
        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        for name in safe_names:
            assert not isinstance(tools_dict[name], ThreadedToolWrapper), (
                f"Expected {name} to NOT be wrapped"
            )
            assert tools_dict[name] is raw_tools[name]

    def test_tool_name_from_key_fallback(self):
        """If tool has no .name attribute, the dict key is used for matching."""
        tool_no_name = MagicMock(spec=[])  # spec=[] means no attributes
        tools_dict = {"grep": tool_no_name}
        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=tools_dict)
        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        # "grep" matches via dict key fallback
        assert isinstance(tools_dict["grep"], ThreadedToolWrapper)

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


class TestNeedsThreadingFrozenset:
    """Verify _NEEDS_THREADING contains exactly the 9 specified blocking tools."""

    def test_needs_threading_has_exactly_9_tools(self):
        """_NEEDS_THREADING must contain exactly 9 tool names per spec."""
        from amplifierd.threading import _NEEDS_THREADING

        assert len(_NEEDS_THREADING) == 9, (
            f"Expected exactly 9 tools in _NEEDS_THREADING, got {len(_NEEDS_THREADING)}: "
            f"{sorted(_NEEDS_THREADING)}"
        )

    def test_needs_threading_contains_exact_set_of_9_tools(self):
        """_NEEDS_THREADING must be exactly the 9 blocking I/O tools from the spec."""
        from amplifierd.threading import _NEEDS_THREADING

        expected = frozenset(
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
        assert _NEEDS_THREADING == expected, (
            f"_NEEDS_THREADING mismatch.\n"
            f"  Extra (should be removed): {sorted(_NEEDS_THREADING - expected)}\n"
            f"  Missing (should be added): {sorted(expected - _NEEDS_THREADING)}"
        )

    def test_async_heavy_tools_excluded_from_needs_threading(self):
        """LSP, nano-banana, team_knowledge, dot_graph must NOT be in _NEEDS_THREADING.

        These tools were removed because they either spawn child sessions, use
        async-native I/O, or have other reasons to run on the caller's event loop.
        """
        from amplifierd.threading import _NEEDS_THREADING

        excluded_tools = {"LSP", "nano-banana", "team_knowledge", "dot_graph"}
        for tool_name in excluded_tools:
            assert tool_name not in _NEEDS_THREADING, (
                f"'{tool_name}' must NOT be in _NEEDS_THREADING (should have been removed)"
            )

    def test_session_spawning_tools_not_in_needs_threading(self):
        """delegate, task, recipes and async-safe tools must not be in _NEEDS_THREADING."""
        from amplifierd.threading import _NEEDS_THREADING

        must_be_absent = {"delegate", "task", "recipes", "bash", "web_search", "todo", "mode"}
        for tool_name in must_be_absent:
            assert tool_name not in _NEEDS_THREADING, (
                f"'{tool_name}' must NOT be in _NEEDS_THREADING"
            )

    def test_previously_excluded_tools_not_wrapped_in_dict_path(self):
        """LSP, nano-banana, team_knowledge, dot_graph are not wrapped (not in frozenset)."""
        tools_dict = {}
        raw_tools = {}
        for name in ("LSP", "nano-banana", "team_knowledge", "dot_graph"):
            tool = MagicMock()
            tool.name = name
            tools_dict[name] = tool
            raw_tools[name] = tool

        coordinator = MagicMock()
        coordinator.get = MagicMock(return_value=tools_dict)
        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        for name in ("LSP", "nano-banana", "team_knowledge", "dot_graph"):
            assert not isinstance(tools_dict[name], ThreadedToolWrapper), (
                f"'{name}' should NOT be wrapped"
            )
            assert tools_dict[name] is raw_tools[name]
