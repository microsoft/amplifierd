# Fix Distro Delegation Threading — Implementation Plan

> **Execution:** Use the subagent-driven-development workflow to implement this plan.

**Goal:** Fix the cross-event-loop crash that breaks `tool-delegate`, `tool-task`, and `tool-recipes` in amp-distro web mode by inverting `wrap_tools_for_threading()` to only wrap known blocking tools.

**Architecture:** Currently `wrap_tools_for_threading()` wraps ALL tools in `ThreadedToolWrapper`, which creates a new event loop per tool call. This breaks session-spawning tools (delegate, recipes) because PyO3 Rust types capture event loop affinity at coroutine creation time. The fix inverts the logic: only tools with known synchronous blocking I/O (`grep`, `glob`, `python_check`, `read_file`, etc.) get wrapped. All other tools — especially session-spawners — run directly on the main uvicorn event loop.

**Tech Stack:** Python 3.12+, pytest with pytest-asyncio, asyncio, git worktrees

**Design doc:** `docs/plans/2026-04-14-fix-distro-delegation-threading-design.md`

---

## Workspace Layout

Both repos live in `/Users/samule/repo/distro-workspace/`:
- `amplifierd/` — main web server repo (on `main` branch)
- `amplifier-chat/` — chat plugin repo (on `main` branch, has diverged copy of `threading.py`)

ALL work is done in git worktrees at:
- `amplifierd-fix-threading/` — worktree for amplifierd changes
- `amplifier-chat-fix-threading/` — worktree for amplifier-chat changes

---

## Part A: amplifierd Repo

### Task 1: Create the amplifierd git worktree

**Files:** None (git operation)

**Step 1: Create the worktree**

```bash
cd /Users/samule/repo/distro-workspace/amplifierd
git worktree add ../amplifierd-fix-threading -b fix/invert-threading-wrapper
```

Expected: Worktree created at `/Users/samule/repo/distro-workspace/amplifierd-fix-threading/` on branch `fix/invert-threading-wrapper`.

**Step 2: Verify the worktree exists**

```bash
cd /Users/samule/repo/distro-workspace/amplifierd-fix-threading
git branch --show-current
```

Expected: `fix/invert-threading-wrapper`

**Step 3: Install dev dependencies in the worktree**

```bash
cd /Users/samule/repo/distro-workspace/amplifierd-fix-threading
uv sync --group dev
```

Expected: Dependencies installed successfully.

---

### Task 2: Write failing tests for selective wrapping

**Files:**
- Modify: `amplifierd-fix-threading/tests/test_threading.py`

The existing `TestWrapToolsForThreading` class (lines 98-142) tests the old "wrap everything" behavior. Replace it with tests for the new selective wrapping logic.

**Step 1: Replace the `TestWrapToolsForThreading` class**

In `amplifierd-fix-threading/tests/test_threading.py`, replace everything from line 98 (`class TestWrapToolsForThreading:`) through line 142 (end of file) with the following:

```python
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
```

---

### Task 3: Verify new tests fail against old code

**Files:** None (verification step)

**Step 1: Run the new selective-wrapping tests**

```bash
cd /Users/samule/repo/distro-workspace/amplifierd-fix-threading
uv run pytest tests/test_threading.py::TestWrapToolsForThreading -v 2>&1 | head -80
```

Expected: Multiple FAIL results. The key failures:
- `test_wraps_only_blocking_tools_dict_path` — FAIL because old code wraps delegate and bash too
- `test_wraps_only_blocking_tools_list_path` — FAIL because old code wraps task and recipes too
- `test_session_spawning_tools_never_wrapped` — FAIL because old code wraps everything
- `test_idempotency_dict_path` — FAIL because old code has no idempotency guard
- `test_all_needs_threading_tools_get_wrapped` — FAIL because `_NEEDS_THREADING` doesn't exist yet (ImportError)

The existing `ThreadedToolWrapper` tests (`TestThreadedToolWrapper`) should still pass — we didn't change those:

```bash
cd /Users/samule/repo/distro-workspace/amplifierd-fix-threading
uv run pytest tests/test_threading.py::TestThreadedToolWrapper -v
```

Expected: All PASS (wrapper class is unchanged).

---

### Task 4: Implement inverted `wrap_tools_for_threading()`

**Files:**
- Modify: `amplifierd-fix-threading/src/amplifierd/threading.py`

**Step 1: Replace `wrap_tools_for_threading()` and add `_NEEDS_THREADING`**

In `amplifierd-fix-threading/src/amplifierd/threading.py`, replace everything from line 51 (`def wrap_tools_for_threading`) through line 91 (end of file) with the following:

```python
# Tools whose execute() does blocking synchronous I/O.
# Session-spawning tools (delegate, task, recipes) and other async-safe tools
# (bash, web_search, todo, mode) are deliberately NOT listed — they must run
# on the caller's event loop to avoid cross-loop crashes with PyO3 Rust types.
_NEEDS_THREADING = frozenset({
    "grep",
    "glob",
    "python_check",
    "read_file",
    "write_file",
    "edit_file",
    "apply_patch",
    "load_skill",
    "web_fetch",
})


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
```

---

### Task 5: Verify all threading tests pass

**Files:** None (verification step)

**Step 1: Run all threading tests**

```bash
cd /Users/samule/repo/distro-workspace/amplifierd-fix-threading
uv run pytest tests/test_threading.py -v
```

Expected: ALL PASS. Both `TestThreadedToolWrapper` (unchanged) and `TestWrapToolsForThreading` (new selective tests).

---

### Task 6: Run the full test suite

**Files:** None (verification step)

**Step 1: Run all tests**

```bash
cd /Users/samule/repo/distro-workspace/amplifierd-fix-threading
uv run pytest tests/ -v --timeout=30 2>&1 | tail -40
```

Expected: All existing tests pass. The change to `wrap_tools_for_threading()` doesn't affect any other test — the function signature and call sites (`spawn.py:259-261`, `session_manager.py:273-275,425-427`) are unchanged. They still call `wrap_tools_for_threading(session)` the same way; it just wraps fewer tools now.

---

### Task 7: Lint check

**Files:** None (verification step)

**Step 1: Run linting on the changed files**

```bash
cd /Users/samule/repo/distro-workspace/amplifierd-fix-threading
uv run ruff check src/amplifierd/threading.py tests/test_threading.py
uv run ruff format --check src/amplifierd/threading.py tests/test_threading.py
```

Expected: No errors. If formatting issues, fix with:

```bash
uv run ruff format src/amplifierd/threading.py tests/test_threading.py
```

---

### Task 8: Commit amplifierd changes

**Files:** None (git operation)

**Step 1: Stage and commit**

```bash
cd /Users/samule/repo/distro-workspace/amplifierd-fix-threading
git add src/amplifierd/threading.py tests/test_threading.py
git commit -m "fix: invert ThreadedToolWrapper to only wrap blocking tools

ThreadedToolWrapper previously wrapped ALL tools unconditionally. This
broke session-spawning tools (delegate, task, recipes) in web mode
because PyO3 Rust types capture event loop affinity at coroutine
creation time, and ThreadedToolWrapper creates a separate event loop
per tool call via asyncio.to_thread(asyncio.run, coro).

The fix inverts the logic: only tools with known synchronous blocking
I/O (grep, glob, python_check, read_file, write_file, edit_file,
apply_patch, load_skill, web_fetch) are wrapped. All other tools run
directly on the main uvicorn event loop, avoiding cross-loop crashes.

Includes idempotency guard to prevent double-wrapping when
wrap_tools_for_threading() is called on child sessions.

Fixes: delegation broken in amp-distro web mode"
```

Expected: Clean commit on `fix/invert-threading-wrapper` branch.

---

## Part B: amplifier-chat Repo

### Task 9: Create the amplifier-chat git worktree

**Files:** None (git operation)

**Step 1: Create the worktree**

```bash
cd /Users/samule/repo/distro-workspace/amplifier-chat
git worktree add ../amplifier-chat-fix-threading -b fix/invert-threading-wrapper
```

Expected: Worktree created at `/Users/samule/repo/distro-workspace/amplifier-chat-fix-threading/` on branch `fix/invert-threading-wrapper`.

**Step 2: Verify the worktree exists**

```bash
cd /Users/samule/repo/distro-workspace/amplifier-chat-fix-threading
git branch --show-current
```

Expected: `fix/invert-threading-wrapper`

**Step 3: Install dev dependencies in the worktree**

```bash
cd /Users/samule/repo/distro-workspace/amplifier-chat-fix-threading
uv sync --group dev
```

Expected: Dependencies installed successfully.

---

### Task 10: Converge threading.py from amplifierd

**Files:**
- Modify: `amplifier-chat-fix-threading/src/amplifierd/threading.py`

The amplifier-chat version is a simpler, diverged copy (78 lines, list-path only, no `callable(get_fn)` guard). Replace it entirely with the updated amplifierd version.

**Step 1: Copy the updated threading.py from the amplifierd worktree**

```bash
cp /Users/samule/repo/distro-workspace/amplifierd-fix-threading/src/amplifierd/threading.py \
   /Users/samule/repo/distro-workspace/amplifier-chat-fix-threading/src/amplifierd/threading.py
```

**Step 2: Verify the file is identical**

```bash
diff /Users/samule/repo/distro-workspace/amplifierd-fix-threading/src/amplifierd/threading.py \
     /Users/samule/repo/distro-workspace/amplifier-chat-fix-threading/src/amplifierd/threading.py
```

Expected: No output (files are identical).

---

### Task 11: Update amplifier-chat test files

**Files:**
- Modify: `amplifier-chat-fix-threading/tests/test_threading.py`
- Modify: `amplifier-chat-fix-threading/tests/test_threaded_tool_wrapper.py`

amplifier-chat has TWO test files for threading. Both test the old "wrap everything" behavior and need updating.

**Important:** amplifier-chat does NOT have `asyncio_mode = "auto"` in its pytest config. All async tests need explicit `@pytest.mark.asyncio` decorators.

**Step 1: Replace `TestWrapToolsForThreading` in `test_threading.py`**

In `amplifier-chat-fix-threading/tests/test_threading.py`, replace everything from line 131 (`# ---------------------------------------------------------------------------`) through line 190 (end of file) with:

```python
# ---------------------------------------------------------------------------
# TestWrapToolsForThreading
# ---------------------------------------------------------------------------


class TestWrapToolsForThreading:
    """Unit tests for the wrap_tools_for_threading helper (selective wrapping)."""

    def _make_tool(self, name: str) -> MagicMock:
        """Create a MagicMock tool with a .name attribute."""
        tool = MagicMock()
        tool.name = name
        return tool

    def test_wraps_only_blocking_tools_list_path(self):
        """Only tools in _NEEDS_THREADING get wrapped."""
        grep_tool = self._make_tool("grep")
        delegate_tool = self._make_tool("delegate")
        bash_tool = self._make_tool("bash")

        coordinator = MagicMock()
        coordinator.get.return_value = [grep_tool, delegate_tool, bash_tool]

        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        coordinator.__setitem__.assert_called_once()
        key, wrapped = coordinator.__setitem__.call_args[0]
        assert key == "tools"
        assert len(wrapped) == 3

        # grep: wrapped (blocking)
        assert isinstance(wrapped[0], ThreadedToolWrapper)
        assert wrapped[0]._tool is grep_tool

        # delegate: NOT wrapped (session-spawning)
        assert not isinstance(wrapped[1], ThreadedToolWrapper)
        assert wrapped[1] is delegate_tool

        # bash: NOT wrapped (async-safe)
        assert not isinstance(wrapped[2], ThreadedToolWrapper)
        assert wrapped[2] is bash_tool

    def test_session_spawning_tools_never_wrapped(self):
        """delegate, task, recipes are never wrapped."""
        safe_names = ["delegate", "task", "recipes"]
        tools = [self._make_tool(name) for name in safe_names]

        coordinator = MagicMock()
        coordinator.get.return_value = tools

        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        coordinator.__setitem__.assert_called_once()
        _, wrapped = coordinator.__setitem__.call_args[0]
        for i, name in enumerate(safe_names):
            assert not isinstance(wrapped[i], ThreadedToolWrapper), (
                f"Expected {name} to NOT be wrapped"
            )

    def test_idempotency(self):
        """Already-wrapped tools are not double-wrapped."""
        inner_tool = self._make_tool("grep")
        already_wrapped = ThreadedToolWrapper(inner_tool)

        coordinator = MagicMock()
        coordinator.get.return_value = [already_wrapped]

        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)

        _, wrapped = coordinator.__setitem__.call_args[0]
        assert len(wrapped) == 1
        assert wrapped[0] is already_wrapped
        assert wrapped[0]._tool is inner_tool

    def test_noop_when_no_tools(self):
        """No error when coordinator.get('tools') returns None."""
        coordinator = MagicMock()
        coordinator.get.return_value = None

        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)
        coordinator.__setitem__.assert_not_called()

    def test_noop_when_no_coordinator(self):
        """No error when the session object has no attributes at all."""
        session = MagicMock(spec=[])
        wrap_tools_for_threading(session)  # Must not raise

    def test_noop_when_tools_empty(self):
        """No error when coordinator.get('tools') returns an empty dict."""
        coordinator = MagicMock()
        coordinator.get.return_value = {}

        session = MagicMock()
        session.coordinator = coordinator

        wrap_tools_for_threading(session)
        coordinator.__setitem__.assert_not_called()
```

**Step 2: Update `TestWrapToolsForThreading` in `test_threaded_tool_wrapper.py`**

In `amplifier-chat-fix-threading/tests/test_threaded_tool_wrapper.py`, replace everything from line 91 (`class TestWrapToolsForThreading:`) through line 155 (end of file) with:

```python
class TestWrapToolsForThreading:
    """Tests for wrap_tools_for_threading helper function (selective wrapping)."""

    def _make_session_with_tools(self, tools):
        """Build a minimal fake session with coordinator and tools."""

        class FakeCoordinator(dict):
            pass

        coordinator = FakeCoordinator()
        coordinator["tools"] = tools

        class FakeSession:
            coordinator: Any = None

        session = FakeSession()
        session.coordinator = coordinator
        return session

    def test_wraps_only_blocking_tools(self):
        class GrepTool:
            name = "grep"

            async def execute(self, input):
                return "ok"

        class DelegateTool:
            name = "delegate"

            async def execute(self, input):
                return "ok"

        tools = [GrepTool(), DelegateTool()]
        session = self._make_session_with_tools(tools)
        wrap_tools_for_threading(session)
        wrapped = session.coordinator["tools"]
        assert len(wrapped) == 2
        assert isinstance(wrapped[0], ThreadedToolWrapper)  # grep: wrapped
        assert not isinstance(wrapped[1], ThreadedToolWrapper)  # delegate: NOT wrapped

    def test_session_spawning_tools_untouched(self):
        class TaskTool:
            name = "task"

            async def execute(self, input):
                return "ok"

        class RecipesTool:
            name = "recipes"

            async def execute(self, input):
                return "ok"

        tools = [TaskTool(), RecipesTool()]
        session = self._make_session_with_tools(tools)
        wrap_tools_for_threading(session)
        wrapped = session.coordinator["tools"]
        for w in wrapped:
            assert not isinstance(w, ThreadedToolWrapper)

    def test_no_coordinator_is_safe(self):
        class FakeSession:
            pass  # No coordinator attribute

        session = FakeSession()
        wrap_tools_for_threading(session)

    def test_none_coordinator_is_safe(self):
        class FakeSession:
            coordinator = None

        session = FakeSession()
        wrap_tools_for_threading(session)

    def test_empty_tools_list(self):
        session = self._make_session_with_tools([])
        wrap_tools_for_threading(session)
        assert session.coordinator["tools"] == []

    def test_tools_key_missing_is_safe(self):
        class FakeCoordinator(dict):
            pass

        class FakeSession:
            coordinator = FakeCoordinator()  # no 'tools' key

        session = FakeSession()
        wrap_tools_for_threading(session)
```

---

### Task 12: Verify amplifier-chat tests pass

**Files:** None (verification step)

**Step 1: Run the threading tests**

```bash
cd /Users/samule/repo/distro-workspace/amplifier-chat-fix-threading
uv run pytest tests/test_threading.py tests/test_threaded_tool_wrapper.py -v
```

Expected: ALL PASS.

**Step 2: Run the full test suite**

```bash
cd /Users/samule/repo/distro-workspace/amplifier-chat-fix-threading
uv run pytest tests/ -v --timeout=30 2>&1 | tail -40
```

Expected: All existing tests pass. No regressions.

---

### Task 13: Lint check for amplifier-chat

**Files:** None (verification step)

**Step 1: Run linting on the changed files**

```bash
cd /Users/samule/repo/distro-workspace/amplifier-chat-fix-threading
uv run ruff check src/amplifierd/threading.py tests/test_threading.py tests/test_threaded_tool_wrapper.py 2>/dev/null || echo "ruff not available; skip"
uv run ruff format --check src/amplifierd/threading.py tests/test_threading.py tests/test_threaded_tool_wrapper.py 2>/dev/null || echo "ruff not available; skip"
```

Expected: No errors. If ruff is not installed in amplifier-chat's venv, this is non-blocking.

---

### Task 14: Commit amplifier-chat changes

**Files:** None (git operation)

**Step 1: Stage and commit**

```bash
cd /Users/samule/repo/distro-workspace/amplifier-chat-fix-threading
git add src/amplifierd/threading.py tests/test_threading.py tests/test_threaded_tool_wrapper.py
git commit -m "fix: converge threading.py from amplifierd — selective tool wrapping

Replace the diverged local copy of threading.py with the updated
version from amplifierd. The key change: wrap_tools_for_threading()
now only wraps tools with known synchronous blocking I/O (grep, glob,
python_check, etc.). Session-spawning tools (delegate, task, recipes)
and async-safe tools run directly on the caller's event loop.

This fixes cross-event-loop crashes when using delegation in web mode.

Also adds dict-path handling and callable(get_fn) guard that the
amplifier-chat copy was missing.

Converged from: amplifierd fix/invert-threading-wrapper"
```

Expected: Clean commit on `fix/invert-threading-wrapper` branch.

---

## Verification Checklist

After both repos are committed, verify:

1. **amplifierd worktree** (`amplifierd-fix-threading/`):
   - [ ] `git log --oneline -1` shows the fix commit
   - [ ] `uv run pytest tests/test_threading.py -v` — all pass
   - [ ] `uv run pytest tests/ --timeout=30` — full suite passes

2. **amplifier-chat worktree** (`amplifier-chat-fix-threading/`):
   - [ ] `git log --oneline -1` shows the converge commit
   - [ ] `diff` between both `threading.py` files shows no differences
   - [ ] `uv run pytest tests/test_threading.py tests/test_threaded_tool_wrapper.py -v` — all pass

3. **Code convergence**: The two `threading.py` files are byte-identical:
   ```bash
   diff /Users/samule/repo/distro-workspace/amplifierd-fix-threading/src/amplifierd/threading.py \
        /Users/samule/repo/distro-workspace/amplifier-chat-fix-threading/src/amplifierd/threading.py
   ```
   Expected: No output.

---

## What This Fix Does NOT Change

- **`spawn.py`** — No changes. It still calls `wrap_tools_for_threading(child_session)` at line 259-261 (amplifierd) / 262-264 (amplifier-chat). The inverted logic in `wrap_tools_for_threading()` handles child sessions correctly.
- **`session_manager.py`** — No changes. Same call pattern at lines 273-275 and 425-427.
- **`ThreadedToolWrapper` class** — No changes. The wrapper itself is unchanged; only the selection logic in `wrap_tools_for_threading()` changed.
- **Any tool module code** — No upstream tool changes. Phase 2 (fixing blocking tools at the source) is separate future work.