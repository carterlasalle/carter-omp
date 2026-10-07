"""Resume-aware behavior of `worker._run_rpc_blocking`.

These tests swap `carter_omp.worker.RpcClient` for a recording fake so we can
observe the `extra_args` and `set_todos` decisions the driver takes based on
whether the workspace's omp session directory already holds a JSONL transcript.
"""

from __future__ import annotations

import asyncio
import stat
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from carter_omp import worker
from carter_omp.config import Settings
from carter_omp.git_ops import DirtyState


class _FakeRpcClient:
    """Recording stand-in for `RpcClient`, installed as `worker.RpcClient`.

    Listeners the driver registers are kept per instance so a prompt hook can
    replay tool events; inject the hook with `_install_prompt_hook`.
    """

    instances: list[_FakeRpcClient] = []

    def __init__(self, *, on_prompt: Callable[[_FakeRpcClient, str], None] | None = None, **kwargs):
        self.kwargs = kwargs
        self.on_prompt = on_prompt
        self.prompts: list[str] = []
        self.event_listeners: list[Callable[[Any], None]] = []
        self.tool_end_listeners: list[Callable[[Any], None]] = []
        self.host_tool_completed_listeners: list[Callable[[Any], None]] = []
        self.set_todos_calls: list[list[dict]] = []
        self.get_todos_calls = 0
        self.stop_calls = 0
        self.mark_closed_calls: list[BaseException] = []
        _FakeRpcClient.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def install_headless_ui(self) -> None:
        pass

    def on_event(self, cb) -> None:
        self.event_listeners.append(cb)

    def on_tool_execution_end(self, cb) -> None:
        self.tool_end_listeners.append(cb)

    def on_host_tool_completed(self, cb) -> None:
        self.host_tool_completed_listeners.append(cb)

    def on_message_update(self, _cb) -> None:
        pass

    def on_message_end(self, _cb) -> None:
        pass

    def on_retry_fallback_applied(self, _cb) -> None:
        pass

    def on_retry_fallback_succeeded(self, _cb) -> None:
        pass

    def stop(self) -> None:
        self.stop_calls += 1

    def _mark_closed(self, error: BaseException) -> None:
        self.mark_closed_calls.append(error)

    def set_todos(self, phases):
        self.set_todos_calls.append(phases)

    def get_todos(self):
        self.get_todos_calls += 1
        return ()

    def emit_event(self, event_type: str) -> None:
        event = SimpleNamespace(type=event_type)
        for cb in self.event_listeners:
            cb(event)

    def emit_tool_end(self, tool_name: str, *, result: Any = None, is_error: bool | None = None) -> None:
        event = SimpleNamespace(tool_name=tool_name, result={} if result is None else result, is_error=is_error)
        for cb in self.tool_end_listeners:
            cb(event)

    def emit_host_tool_completed(self, tool_name: str) -> None:
        event = SimpleNamespace(tool_name=tool_name, tool_call_id=f"js-{tool_name}-1")
        for cb in self.host_tool_completed_listeners:
            cb(event)

    def prompt_and_wait(self, prompt, timeout):
        self.prompts.append(prompt)
        if self.on_prompt is not None:
            self.on_prompt(self, prompt)

        class _Turn:
            messages: list = []
            events: list = []
            assistant_text: str = "ok"
            assistant_message: dict | None = None

        return _Turn()


def _install_prompt_hook(monkeypatch: pytest.MonkeyPatch, on_prompt: Callable[[_FakeRpcClient, str], None]) -> None:
    """Make the driver's next `RpcClient` a fake that runs `on_prompt` after each prompt."""
    monkeypatch.setattr("carter_omp.worker.RpcClient", partial(_FakeRpcClient, on_prompt=on_prompt))


_SEEDED_PHASES = [
    {
        "id": "p1",
        "name": "Reproduce",
        "tasks": [
            {
                "id": "t1",
                "content": "do it",
                "status": "pending",
                "notes": "",
                "details": "",
            }
        ],
    }
]


def _make_inputs(
    tmp_path: Path, settings: Settings, *, session_has_jsonl: bool, slot_uid: int | None = None
) -> tuple[worker.TaskInputs, SimpleNamespace]:
    root = tmp_path / "workspace"
    root.mkdir()
    session_dir = root / "session"
    session_dir.mkdir()
    if session_has_jsonl:
        (session_dir / "foo.jsonl").write_text("{}\n", encoding="utf-8")
    repo_dir = root / "repo"
    repo_dir.mkdir()

    workspace = SimpleNamespace(
        root=root,
        session_dir=session_dir,
        repo_dir=repo_dir,
        branch="carter_omp/issue-1",
    )
    repo = SimpleNamespace(full_name="acme/widgets", owner="acme", name="widgets")
    issue = SimpleNamespace(repo="acme/widgets", number=1, title="bug")

    db = SimpleNamespace(set_event_model=lambda _did, _model: None, get_issue=lambda _key: None)
    github = SimpleNamespace()

    inputs = worker.TaskInputs(
        settings=settings,
        db=db,  # type: ignore[arg-type]
        github=github,  # type: ignore[arg-type]
        git_transport=SimpleNamespace(),  # type: ignore[arg-type]
        repo=repo,  # type: ignore[arg-type]
        issue=issue,  # type: ignore[arg-type]
        workspace=workspace,  # type: ignore[arg-type]
        delivery_id="d-test",
        attempts=0,
        slot_uid=slot_uid,
    )
    bindings = SimpleNamespace(
        workspace=workspace,
        repo=repo,
        issue=issue,
        issue_key=f"{repo.full_name}#{issue.number}",
        abort=None,
    )
    return inputs, bindings


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    _FakeRpcClient.instances.clear()


@pytest.fixture(autouse=True)
def _patch_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("carter_omp.worker.RpcClient", _FakeRpcClient)
    monkeypatch.setattr("carter_omp.worker._AGENT_HOME_STAGE", tmp_path / "missing-agent-home-stage")
    monkeypatch.setattr("carter_omp.worker.host_tools.build", lambda _b: ())
    monkeypatch.setattr(
        "carter_omp.worker.persona.system_append",
        lambda *, repo, issue, workspace, bot_login: "SYS",
    )
    monkeypatch.setattr(
        "carter_omp.worker.persona.seed_phases",
        lambda _kind: [dict(p) for p in _SEEDED_PHASES],
    )


@pytest.mark.asyncio
async def test_run_task_sets_impl_authorized_from_directive(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    captured: dict[str, bool] = {}

    monkeypatch.setattr(worker, "_build_prompt", lambda *args, **kwargs: "prompt")

    def fake_run_rpc_blocking(
        _inputs: worker.TaskInputs,
        *,
        task_kind: str,
        prompt: str,
        bindings: worker.ToolBindings,
        directive: worker.DirectiveInfo | None = None,
    ) -> str:
        del task_kind, prompt, directive
        captured["impl_authorized"] = bindings.impl_authorized
        return "ok"

    monkeypatch.setattr(worker, "_run_rpc_blocking", fake_run_rpc_blocking)

    result = await worker.run_task(
        task_kind="triage_issue",
        inputs=inputs,
        directive=worker.DirectiveInfo(body="go ahead", author="can1357", authorizes_impl=True),
    )

    assert result == "ok"
    assert captured == {"impl_authorized": True}


@pytest.mark.asyncio
async def test_run_task_preserves_impl_authorized_when_resuming(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, _bindings = _make_inputs(tmp_path, settings, session_has_jsonl=True)
    captured: dict[str, bool] = {}

    monkeypatch.setattr(worker, "_build_prompt", lambda *args, **kwargs: "prompt")

    def capture_build(bindings: worker.ToolBindings) -> tuple:
        captured["impl_authorized"] = bindings.impl_authorized
        return ()

    monkeypatch.setattr(worker.host_tools, "build", capture_build)

    result = await worker.run_task(
        task_kind="handle_comment",
        inputs=inputs,
        directive=worker.DirectiveInfo(body="go ahead", author="can1357", authorizes_impl=True),
    )

    assert result == "ok"
    assert captured == {"impl_authorized": True}
    assert _FakeRpcClient.instances[0].kwargs["extra_args"] == ("--continue", "--no-extensions")


def _trigger(*, capabilities) -> Any:
    from datetime import UTC, datetime

    from carter_omp.github_events import TriggerContext

    return TriggerContext(
        run_id="run-1",
        delivery_id="d-test",
        repository_id=1,
        repository_full_name="acme/widgets",
        installation_id=1,
        actor_id=1,
        actor_login="carterlasalle",
        actor_type="User",
        event_type="issue_comment",
        action="created",
        trigger_kind="mention",
        trigger_object_id=555,
        trigger_value="/allow-skip-checks",
        issue_number=1,
        pull_request_number=None,
        capabilities=capabilities,
        policy_version="carter-omp/v1",
        authorized_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_run_task_mirrors_trigger_capabilities_into_bindings(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator-granted extras on the trigger reach the host-tool gates.

    `/allow-skip-checks` adds SKIP_CHECKS to the trigger only; if bindings were
    rebuilt from `capabilities_for(task_kind)` the pre-publish gates would
    never see it and the documented bypass stays unreachable.
    """
    from carter_omp.capabilities import ISSUE_RUN_CAPABILITIES, Capability

    inputs, _bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    granted = ISSUE_RUN_CAPABILITIES | {Capability.SKIP_CHECKS}
    inputs.trigger = _trigger(capabilities=granted)
    captured: dict[str, object] = {}

    monkeypatch.setattr(worker, "_build_prompt", lambda *args, **kwargs: "prompt")

    def fake_run_rpc_blocking(
        _inputs: worker.TaskInputs,
        *,
        task_kind: str,
        prompt: str,
        bindings: worker.ToolBindings,
        directive: worker.DirectiveInfo | None = None,
    ) -> str:
        del task_kind, prompt, directive
        captured["capabilities"] = bindings.capabilities
        return "ok"

    monkeypatch.setattr(worker, "_run_rpc_blocking", fake_run_rpc_blocking)

    result = await worker.run_task(task_kind="handle_comment", inputs=inputs)

    assert result == "ok"
    assert captured["capabilities"] == granted


@pytest.mark.asyncio
async def test_run_task_falls_back_to_task_profile_without_trigger(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy/manual paths with no TriggerContext keep the task-kind profile."""
    from carter_omp.capabilities import PR_REVIEW_CAPABILITIES

    inputs, _bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    inputs.workspace.branch = "review/pr-7"
    captured: dict[str, object] = {}

    monkeypatch.setattr(worker, "_build_prompt", lambda *args, **kwargs: "prompt")

    def fake_run_rpc_blocking(
        _inputs: worker.TaskInputs,
        *,
        task_kind: str,
        prompt: str,
        bindings: worker.ToolBindings,
        directive: worker.DirectiveInfo | None = None,
    ) -> str:
        del task_kind, prompt, directive
        captured["capabilities"] = bindings.capabilities
        return "ok"

    monkeypatch.setattr(worker, "_run_rpc_blocking", fake_run_rpc_blocking)

    result = await worker.run_task(task_kind="review_pr", inputs=inputs)

    assert result == "ok"
    assert captured["capabilities"] == PR_REVIEW_CAPABILITIES


@pytest.mark.asyncio
async def test_run_rpc_passes_continue_when_session_jsonl_present(tmp_path: Path, settings: Settings) -> None:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=True)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )
    assert _FakeRpcClient.instances[0].kwargs["extra_args"] == ("--continue", "--no-extensions")


@pytest.mark.asyncio
async def test_run_rpc_omits_continue_when_session_empty(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_home = tmp_path / "agent-home"
    agent_home.mkdir()
    monkeypatch.setattr(worker, "_AGENT_HOME", agent_home)

    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )
    assert _FakeRpcClient.instances[0].kwargs["extra_args"] == ("--no-extensions",)
    client_kwargs = _FakeRpcClient.instances[0].kwargs
    assert client_kwargs["env"]["HOME"] == str(agent_home)
    assert client_kwargs["env"]["GITHUB_TOKEN"] == ""
    assert client_kwargs["env"]["GITHUB_WEBHOOK_SECRET"] == ""
    assert client_kwargs["env"]["CARTER_OMP_REPLAY_TOKEN"] == ""
    assert client_kwargs["env"]["CARTER_OMP_GH_PROXY_HMAC_KEY"] == ""
    assert client_kwargs["user"] is None
    assert client_kwargs["group"] is None
    assert client_kwargs["extra_groups"] is None


def test_build_extra_env_stages_agent_home(tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    stage_home = tmp_path / "agent-home-stage"
    agent_home = tmp_path / "agent-home"
    monkeypatch.setattr(worker, "_AGENT_HOME_STAGE", stage_home)
    monkeypatch.setattr(worker, "_AGENT_HOME", agent_home)

    agent_dir = stage_home / ".agent"
    agent_rules_dir = agent_dir / "rules"
    omp_agent_dir = stage_home / ".omp" / "agent"
    agent_rules_dir.mkdir(parents=True)
    omp_agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text("agent instructions\n", encoding="utf-8")
    (agent_rules_dir / "rule.md").write_text("rule\n", encoding="utf-8")
    (omp_agent_dir / "models.yml").write_text("models: []\n", encoding="utf-8")

    env = worker._build_extra_env(settings)

    assert env["HOME"] == str(agent_home)
    assert (agent_home / ".agent" / "AGENTS.md").is_file()
    assert (agent_home / ".agent" / "rules" / "rule.md").is_file()
    assert (agent_home / ".omp" / "agent" / "models.yml").is_file()
    assert (agent_home / ".agent").stat().st_mode & 0o777 == 0o755
    assert (agent_home / ".agent" / "AGENTS.md").stat().st_mode & 0o777 == 0o644
    assert (agent_home / ".agent" / "rules").stat().st_mode & 0o777 == 0o755
    assert (agent_home / ".agent" / "rules" / "rule.md").stat().st_mode & 0o777 == 0o644
    assert (agent_home / ".omp" / "agent").stat().st_mode & 0o777 == 0o755
    assert (agent_home / ".omp" / "agent" / "models.yml").stat().st_mode & 0o777 == 0o644


@pytest.mark.asyncio
async def test_run_rpc_omits_home_when_agent_home_absent(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "_AGENT_HOME", tmp_path / "missing-agent-home")

    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )
    client_kwargs = _FakeRpcClient.instances[0].kwargs
    assert "HOME" not in client_kwargs["env"]
    assert client_kwargs["env"]["GITHUB_TOKEN"] == ""
    assert client_kwargs["env"]["GITHUB_WEBHOOK_SECRET"] == ""
    assert client_kwargs["env"]["CARTER_OMP_REPLAY_TOKEN"] == ""
    assert client_kwargs["env"]["CARTER_OMP_GH_PROXY_HMAC_KEY"] == ""


@pytest.mark.asyncio
async def test_run_rpc_uses_workspace_xdg_dirs_without_slot(tmp_path: Path, settings: Settings) -> None:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False, slot_uid=None)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )

    env = _FakeRpcClient.instances[0].kwargs["env"]
    xdg_root = inputs.workspace.root / ".omp-xdg"
    for key in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        path = Path(env[key])
        assert path.is_relative_to(xdg_root)
        assert (path / "omp").is_dir()
    tmpdir = inputs.workspace.root / ".omp-tmp"
    assert env["TMPDIR"] == str(tmpdir)
    assert env["TMP"] == str(tmpdir)
    assert env["TEMP"] == str(tmpdir)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == str(inputs.workspace.repo_dir)
    assert env["GIT_AUTHOR_NAME"] == settings.resolved_author_name
    assert env["GIT_AUTHOR_EMAIL"] == settings.git_author_email
    assert env["GIT_COMMITTER_NAME"] == settings.resolved_author_name
    assert env["GIT_COMMITTER_EMAIL"] == settings.git_author_email
    assert tmpdir.is_dir()
    assert stat.S_IMODE(tmpdir.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_run_rpc_uses_workspace_xdg_dirs_for_slot_without_chown(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    chown_calls: list[tuple[Path, int, int]] = []
    monkeypatch.setattr("carter_omp.sandbox.platform.system", lambda: "Linux")
    monkeypatch.setattr("carter_omp.sandbox.os.geteuid", lambda: 0)
    monkeypatch.setattr(
        "carter_omp.sandbox.os.chown", lambda path, uid, gid: chown_calls.append((Path(path), uid, gid))
    )

    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False, slot_uid=2001)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )

    env = _FakeRpcClient.instances[0].kwargs["env"]
    for key in ("XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        base = Path(env[key])
        assert base.is_dir()
        assert (base / "omp").is_dir()
    assert Path(env["BUN_INSTALL_CACHE_DIR"]).is_dir()
    assert chown_calls == []


@pytest.mark.asyncio
async def test_run_rpc_skips_set_todos_on_resumed_triage(tmp_path: Path, settings: Settings) -> None:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=True)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )
    assert _FakeRpcClient.instances[0].set_todos_calls == []


@pytest.mark.asyncio
async def test_run_rpc_seeds_todos_on_fresh_triage(tmp_path: Path, settings: Settings) -> None:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )
    calls = _FakeRpcClient.instances[0].set_todos_calls
    assert len(calls) == 1
    assert calls[0] == _SEEDED_PHASES


@pytest.mark.asyncio
async def test_run_rpc_merges_todos_on_followup_with_resume(tmp_path: Path, settings: Settings) -> None:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=True)
    worker._run_rpc_blocking(
        inputs,
        task_kind="handle_comment",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )
    client = _FakeRpcClient.instances[0]
    assert client.get_todos_calls == 1
    assert len(client.set_todos_calls) == 1
    assert len(client.set_todos_calls[0]) == len(_SEEDED_PHASES)


@pytest.mark.asyncio
async def test_run_rpc_passes_slot_uid_user_slot_group_and_omp_extra_group(tmp_path: Path, settings: Settings) -> None:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False, slot_uid=2001)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )
    client_kwargs = _FakeRpcClient.instances[0].kwargs
    assert client_kwargs["user"] == 2001
    assert client_kwargs["group"] == 2001
    assert client_kwargs["extra_groups"] == ["omp"]


@pytest.mark.asyncio
async def test_run_rpc_arms_hard_timeout_timer(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    timers = []

    class FakeTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False
            self.started = False
            self.cancelled = False
            timers.append(self)

        def start(self) -> None:
            self.started = True

        def cancel(self) -> None:
            self.cancelled = True

    monkeypatch.setattr("carter_omp.worker.threading.Timer", FakeTimer)
    settings.task_timeout_seconds = 3.0
    settings.task_timeout_hard_grace_seconds = 7.0
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )

    assert len(timers) == 1
    timer = timers[0]
    assert timer.interval == 10.0
    assert timer.daemon is True
    assert timer.started is True
    assert timer.cancelled is True


@pytest.mark.asyncio
async def test_run_rpc_hard_timeout_stops_client_and_fails(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FiringTimer:
        def __init__(self, interval, function):
            self.interval = interval
            self.function = function
            self.daemon = False
            self.cancelled = False

        def start(self) -> None:
            self.function()

        def cancel(self) -> None:
            self.cancelled = True

    monkeypatch.setattr("carter_omp.worker.threading.Timer", FiringTimer)
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    with pytest.raises(TimeoutError, match="hard timeout"):
        worker._run_rpc_blocking(
            inputs,
            task_kind="triage_issue",
            prompt="x",
            bindings=bindings,  # type: ignore[arg-type]
        )

    fake = _FakeRpcClient.instances[0]
    assert fake.stop_calls == 1
    # `_cancel_hook` (used by both manual cancel and hard timeout) MUST also call
    # `_mark_closed` to unblock `_wait_for_agent_end` — `stop()` alone leaves
    # `_closed_error` unset (omp_rpc bug), so the worker would hang otherwise.
    assert len(fake.mark_closed_calls) == 1
    from omp_rpc import RpcProcessExitError

    assert isinstance(fake.mark_closed_calls[0], RpcProcessExitError)


def test_agent_activity_watch_fires_on_silence_and_rearms() -> None:
    """The watch is the *silence* deadline `prompt_and_wait` never had."""
    now = {"t": 1000.0}
    stalls: list[str] = []
    watch = worker._AgentActivityWatch(seconds=10.0, on_stall=lambda: stalls.append("x"), clock=lambda: now["t"])
    assert watch.enabled is True
    assert watch.fired is False

    now["t"] += 30
    assert watch.check() is True
    assert watch.fired is True
    assert stalls == ["x"]
    assert watch.reason() == "no agent activity for 30s (last event: turn start)"

    # A second watch re-armed by steady events never fires (4s < 10s each time).
    quiet = worker._AgentActivityWatch(seconds=10.0, on_stall=lambda: stalls.append("y"), clock=lambda: now["t"])
    for _ in range(5):
        now["t"] += 4
        quiet.touch("message_update")
        assert quiet.check() is False
    now["t"] += 7  # 7s since the last event, still inside the budget
    assert quiet.check() is False
    assert stalls == ["x"]
    assert quiet.reason() == "no agent activity for 7s (last event: message_update)"


def test_agent_activity_watch_is_disabled_at_zero() -> None:
    watch = worker._AgentActivityWatch(seconds=0.0, on_stall=lambda: pytest.fail("disabled watch must never fire"))
    watch.start()
    assert watch.enabled is False
    assert watch.check() is False


def test_run_rpc_fails_fast_when_the_agent_goes_silent(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent child fails on the silence budget, not on the 40-minute turn budget."""
    settings.task_stall_seconds = 0.3

    def on_prompt(_client, _prompt: str) -> None:
        time.sleep(1.2)  # agent alive but emitting nothing (the scc#21 hang)

    _install_prompt_hook(monkeypatch, on_prompt)
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    started = time.monotonic()
    with pytest.raises(worker.AgentStalledError, match=r"no agent activity for \d+s"):
        worker._run_rpc_blocking(
            inputs,
            task_kind="triage_issue",
            prompt="x",
            bindings=bindings,  # type: ignore[arg-type]
        )
    assert time.monotonic() - started < 5.0

    fake = _FakeRpcClient.instances[0]
    assert fake.stop_calls == 1
    from omp_rpc import RpcProcessExitError

    assert len(fake.mark_closed_calls) == 1
    assert isinstance(fake.mark_closed_calls[0], RpcProcessExitError)


def test_run_rpc_tolerates_a_slow_but_talking_agent(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steady events re-arm the watch: a long turn is not a stalled one."""
    settings.task_stall_seconds = 0.5

    def on_prompt(client, _prompt: str) -> None:
        for _ in range(20):  # ~1.0s of work, events every 50ms
            client.emit_event("message_update")
            time.sleep(0.05)

    _install_prompt_hook(monkeypatch, on_prompt)
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    result = worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )

    assert result == "ok"
    assert _FakeRpcClient.instances[0].stop_calls == 0


@pytest.mark.asyncio
async def test_run_rpc_cancel_hook_stops_and_marks_closed(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cancel hook registered with `register_cancel_hook` must call both
    `client.stop()` AND `client._mark_closed()`. The latter is the workaround for
    an upstream omp_rpc bug where `stop()` does not set `_closed_error`, leaving
    `_wait_for_agent_end` blocked until timeout."""
    captured: list = []
    monkeypatch.setattr("carter_omp.worker.register_cancel_hook", lambda hook: captured.append(hook))
    monkeypatch.setattr("carter_omp.worker.unregister_cancel_hook", lambda: None)

    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )

    assert len(captured) == 1
    hook = captured[0]
    fake = _FakeRpcClient.instances[0]
    pre_stop = fake.stop_calls
    hook()  # Simulate the API/worker firing the cancel
    assert fake.stop_calls == pre_stop + 1
    assert len(fake.mark_closed_calls) == 1
    from omp_rpc import RpcProcessExitError

    assert isinstance(fake.mark_closed_calls[0], RpcProcessExitError)
    assert "cancelled by operator" in str(fake.mark_closed_calls[0])


class _ClassifiedRow:
    """Stand-in for `db.IssueRow` carrying just `.classification`."""

    def __init__(self, classification: str | None) -> None:
        self.classification = classification


def _make_inputs_with_classification(
    tmp_path: Path,
    settings: Settings,
    *,
    classification: str | None,
) -> tuple[worker.TaskInputs, SimpleNamespace]:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=True)
    row = _ClassifiedRow(classification) if classification else None
    inputs.db.get_issue = lambda _key: row  # type: ignore[attr-defined]
    bindings.db = inputs.db  # tools_called check uses inputs.db.get_issue
    return inputs, bindings


@pytest.mark.asyncio
async def test_run_rpc_sends_reminder_when_pr_class_quits_early(tmp_path: Path, settings: Settings) -> None:
    """`bug` classified turn that never calls a terminal tool gets a reminder."""
    inputs, bindings = _make_inputs_with_classification(tmp_path, settings, classification="bug")
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )
    fake = _FakeRpcClient.instances[0]
    # kickoff + 2 reminders (default CARTER_OMP_TASK_COMPLETION_MAX_REMINDERS=2)
    assert len(fake.prompts) == 1 + settings.task_completion_max_reminders
    assert fake.prompts[0] == "kickoff"
    terminal_tools = {"gh_open_pr", "mark_unable_to_reproduce", "abort_task"}
    assert all(terminal_tools <= set(prompt.split("`")) for prompt in fake.prompts[1:])


@pytest.mark.asyncio
async def test_run_rpc_stops_reminding_after_terminal_tool(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reminder turn that fires `gh_open_pr` halts the loop."""
    inputs, bindings = _make_inputs_with_classification(tmp_path, settings, classification="bug")

    # The first turn ends without a terminal tool; the agent calls
    # `gh_open_pr` during the first reminder turn.
    def _on_prompt(client: _FakeRpcClient, _prompt: str) -> None:
        if len(client.prompts) == 2:
            client.emit_tool_end("gh_open_pr")

    _install_prompt_hook(monkeypatch, _on_prompt)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )

    fake = _FakeRpcClient.instances[0]
    # kickoff + 1 reminder; second reminder NOT sent because gh_open_pr fired.
    assert len(fake.prompts) == 2, fake.prompts


@pytest.mark.asyncio
async def test_run_rpc_skips_reminder_for_non_pr_classification(tmp_path: Path, settings: Settings) -> None:
    """`question` classified turns are not enforced — no reminder."""
    inputs, bindings = _make_inputs_with_classification(tmp_path, settings, classification="question")
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )
    fake = _FakeRpcClient.instances[0]
    assert len(fake.prompts) == 1


@pytest.mark.asyncio
async def test_run_rpc_skips_reminder_when_unclassified(tmp_path: Path, settings: Settings) -> None:
    """No classification (agent quit before classify_issue) → no reminder."""
    inputs, bindings = _make_inputs_with_classification(tmp_path, settings, classification=None)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )
    fake = _FakeRpcClient.instances[0]
    assert len(fake.prompts) == 1


@pytest.mark.asyncio
async def test_run_rpc_comment_turn_reminds_without_a_reply(tmp_path: Path, settings: Settings) -> None:
    """A mention that ends without posting leaves the human waiting.

    The run on personal_website#64 (2026-10-07) called todo/bash and stopped:
    the delivery went `done` in 84s and the thread got nothing. `handle_comment`
    now has a terminal tool like the review and triage kinds.
    """
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="handle_comment",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )
    fake = _FakeRpcClient.instances[0]
    assert len(fake.prompts) == 1 + settings.task_completion_max_reminders
    assert fake.prompts[0] == "kickoff"
    assert all("gh_post_comment" in p for p in fake.prompts[1:])


@pytest.mark.asyncio
async def test_run_rpc_comment_turn_stops_after_the_reply(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    prompts: list[str] = []

    def on_prompt(client, prompt: str) -> None:
        prompts.append(prompt)
        client.emit_tool_end("gh_post_comment")

    _install_prompt_hook(monkeypatch, on_prompt)
    worker._run_rpc_blocking(
        inputs,
        task_kind="handle_comment",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )
    assert prompts == ["kickoff"]


@pytest.mark.asyncio
async def test_run_rpc_review_pr_reminds_until_submit_pr_review(tmp_path: Path, settings: Settings) -> None:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="review_pr",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )
    fake = _FakeRpcClient.instances[0]
    assert len(fake.prompts) == 1 + settings.task_completion_max_reminders
    assert fake.prompts[0] == "kickoff"
    assert all("submit_pr_review" in p for p in fake.prompts[1:])
    assert all("gh_open_pr" not in p for p in fake.prompts[1:])


@pytest.mark.asyncio
async def test_run_rpc_review_pr_stops_after_submit_without_dirty_probe(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)

    def _probe(_workspace, _slot_uid):  # type: ignore[no-untyped-def]
        raise AssertionError("review_pr must not run dirty-state probes")

    monkeypatch.setattr(worker, "_probe_workspace_dirty", _probe)
    _install_prompt_hook(monkeypatch, lambda client, _prompt: client.emit_tool_end("submit_pr_review"))
    worker._run_rpc_blocking(
        inputs,
        task_kind="review_pr",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )

    fake = _FakeRpcClient.instances[0]
    assert fake.prompts == ["kickoff"]


@pytest.mark.asyncio
async def test_run_rpc_review_pr_still_reminds_when_submit_fails(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An errored terminal-tool end event does not count as the terminal action.

    omp_rpc normalizes xd:// device dispatches to the host-tool name, so a
    rejected `submit_pr_review` surfaces as an end event with `is_error=True`
    under its real name. Counting it would end the review task silently with
    no review submitted — the completion reminder must still fire.
    """
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)

    def _on_prompt(client: _FakeRpcClient, _prompt: str) -> None:
        client.emit_tool_end(
            "submit_pr_review",
            result={"content": [{"type": "text", "text": "GitHub rejected PR review: 422"}]},
            is_error=True,
        )

    _install_prompt_hook(monkeypatch, _on_prompt)
    worker._run_rpc_blocking(
        inputs,
        task_kind="review_pr",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )

    fake = _FakeRpcClient.instances[0]
    assert len(fake.prompts) == 1 + settings.task_completion_max_reminders
    assert all("submit_pr_review" in p for p in fake.prompts[1:])


@pytest.mark.asyncio
async def test_run_rpc_review_pr_stops_after_eval_bridged_submit(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `submit_pr_review` reached through the eval bridge ends the review task.

    The only transport end event is the enclosing `eval`; the host-tool
    completion signal must still satisfy the terminal-action gate, otherwise
    the completion reminders make the agent submit the review again (#13583).
    """
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)

    def _on_prompt(client: _FakeRpcClient, _prompt: str) -> None:
        client.emit_host_tool_completed("submit_pr_review")
        client.emit_tool_end("eval")

    _install_prompt_hook(monkeypatch, _on_prompt)
    worker._run_rpc_blocking(
        inputs,
        task_kind="review_pr",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )

    fake = _FakeRpcClient.instances[0]
    assert fake.prompts == ["kickoff"]


# ---------------------------------------------------------------------------
# Dirty-state watchdog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_rpc_sends_dirty_state_reminder_when_worktree_has_unpushed_work(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent ended its turn with unpushed commits → reminder fires; clean → loop exits."""
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    dirty = DirtyState(uncommitted=2, unpushed=1, summary="Unpushed commits (1):\nabc1234 wip")
    clean = DirtyState(uncommitted=0, unpushed=0, summary="")
    states = iter([dirty, clean])
    monkeypatch.setattr(worker, "_probe_workspace_dirty", lambda _ws, _slot: next(states, clean))

    # A mention turn must reach its terminal action (the reply) before the
    # dirty-state check applies — that is what makes this a "replied but left
    # work unpushed" scenario.
    _install_prompt_hook(monkeypatch, lambda client, _prompt: client.emit_tool_end("gh_post_comment"))
    worker._run_rpc_blocking(
        inputs,
        task_kind="handle_comment",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )

    fake = _FakeRpcClient.instances[0]
    assert len(fake.prompts) == 2, fake.prompts
    reminder = fake.prompts[1]
    assert "Unpushed commits" in reminder
    assert "abc1234" in reminder
    assert "{{" not in reminder, "template placeholder leaked"


@pytest.mark.asyncio
async def test_run_rpc_skips_dirty_state_reminder_when_worktree_is_clean(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean worktree at end of turn → no extra prompts."""
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    monkeypatch.setattr(
        worker,
        "_probe_workspace_dirty",
        lambda _ws, _slot: DirtyState(uncommitted=0, unpushed=0, summary=""),
    )

    # A mention turn must reach its terminal action (the reply) before the
    # dirty-state check applies — that is what makes this a "replied but left
    # work unpushed" scenario.
    _install_prompt_hook(monkeypatch, lambda client, _prompt: client.emit_tool_end("gh_post_comment"))
    worker._run_rpc_blocking(
        inputs,
        task_kind="handle_comment",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )

    fake = _FakeRpcClient.instances[0]
    assert len(fake.prompts) == 1


@pytest.mark.asyncio
async def test_run_rpc_caps_dirty_state_reminders_at_budget(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistently dirty workspace must not loop past the reminder budget."""
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    dirty = DirtyState(uncommitted=1, unpushed=0, summary="Uncommitted changes (1):\n?? oops.txt")
    monkeypatch.setattr(worker, "_probe_workspace_dirty", lambda _ws, _slot: dirty)

    worker._run_rpc_blocking(
        inputs,
        task_kind="handle_comment",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )

    fake = _FakeRpcClient.instances[0]
    # kickoff + N reminders, capped at the configured budget (default 2).
    assert len(fake.prompts) == 1 + settings.task_completion_max_reminders


# ---------------------------------------------------------------------------
# Natives-cache capture-on-success
# ---------------------------------------------------------------------------


class _RecordingNativesCache:
    """Test double for `NativesCache`: records `capture` calls, optionally
    raises so we can verify exception swallowing."""

    def __init__(self, *, raise_on_capture: bool = False) -> None:
        self.capture_calls: list[tuple[str, str, Path]] = []
        self.raise_on_capture = raise_on_capture

    def capture(self, repo: str, key: str, native_dir: Path, **_kwargs) -> Path | None:
        self.capture_calls.append((repo, key, native_dir))
        if self.raise_on_capture:
            raise RuntimeError("simulated cache failure")
        return native_dir


def _make_capture_inputs(
    tmp_path: Path,
    settings: Settings,
    *,
    cache: _RecordingNativesCache | None,
    with_native_artifacts: bool,
) -> worker.TaskInputs:
    """Build a `TaskInputs` whose workspace optionally has built natives."""
    inputs, _ = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    # Replace the SimpleNamespace workspace with one carrying the fields
    # `_capture_natives_cache` needs (workspace_key + repo_full_name).
    ws = SimpleNamespace(
        root=inputs.workspace.root,
        session_dir=inputs.workspace.session_dir,
        repo_dir=inputs.workspace.repo_dir,
        branch=inputs.workspace.branch,
        workspace_key="acme__widgets__1",
        repo_full_name="acme/widgets",
    )
    if with_native_artifacts:
        native_dir = ws.repo_dir / "packages" / "natives" / "native"
        native_dir.mkdir(parents=True)
        (native_dir / "pi_natives.linux-arm64.node").write_bytes(b"ELFx")
        (native_dir / "index.d.ts").write_text("")
        (native_dir / "index.js").write_text("")
        (native_dir / "embedded-addon.js").write_text("")
    return worker.TaskInputs(
        settings=settings,
        db=inputs.db,
        github=inputs.github,
        git_transport=inputs.git_transport,
        repo=inputs.repo,
        issue=inputs.issue,
        workspace=ws,  # type: ignore[arg-type]
        delivery_id=inputs.delivery_id,
        attempts=inputs.attempts,
        slot_uid=inputs.slot_uid,
        natives_cache=cache,  # type: ignore[arg-type]
    )


def test_capture_natives_cache_no_op_without_cache(tmp_path: Path, settings: Settings) -> None:
    inputs = _make_capture_inputs(tmp_path, settings, cache=None, with_native_artifacts=True)
    # Just must not raise.
    worker._capture_natives_cache(inputs)


def test_capture_natives_cache_skips_without_artifacts(tmp_path: Path, settings: Settings) -> None:
    cache = _RecordingNativesCache()
    inputs = _make_capture_inputs(tmp_path, settings, cache=cache, with_native_artifacts=False)
    worker._capture_natives_cache(inputs)
    # No artifacts → no key compute, no capture.
    assert cache.capture_calls == []


def test_capture_natives_cache_swallows_key_compute_failure(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _RecordingNativesCache()
    inputs = _make_capture_inputs(tmp_path, settings, cache=cache, with_native_artifacts=True)
    # Repo dir is not a git repo → natives_compute_key raises.
    # Already true for the SimpleNamespace workspace (repo_dir is plain tmp dir).
    worker._capture_natives_cache(inputs)
    assert cache.capture_calls == []


def test_capture_natives_cache_swallows_capture_exception(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _RecordingNativesCache(raise_on_capture=True)
    inputs = _make_capture_inputs(tmp_path, settings, cache=cache, with_native_artifacts=True)
    # Bypass git: stub the key compute so capture is reached.
    monkeypatch.setattr(worker, "natives_compute_key", lambda _repo_dir: "deadbeef")
    # Must not propagate the RuntimeError.
    worker._capture_natives_cache(inputs)
    assert len(cache.capture_calls) == 1


def test_capture_natives_cache_records_on_success(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = _RecordingNativesCache()
    inputs = _make_capture_inputs(tmp_path, settings, cache=cache, with_native_artifacts=True)
    monkeypatch.setattr(worker, "natives_compute_key", lambda _repo_dir: "cafef00d")
    worker._capture_natives_cache(inputs)
    assert len(cache.capture_calls) == 1
    repo, key, native_dir = cache.capture_calls[0]
    assert repo == "acme/widgets"
    assert key == "cafef00d"
    assert native_dir == inputs.workspace.repo_dir / "packages" / "natives" / "native"


def _release_inputs(
    tmp_path: Path,
    settings: Settings,
    *,
    session_has_jsonl: bool,
) -> tuple[worker.TaskInputs, SimpleNamespace]:
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=session_has_jsonl)
    inputs.issue = None
    inputs.release = worker.ReleaseTaskContext(
        tag="v17.2.8",
        version="17.2.8",
        round=2,
        max_rounds=5,
        head_sha="abc",
        default_branch="main",
        failures_text="tests failed",
        run_urls=("https://example/run",),
    )
    bindings.issue = None
    bindings.issue_key = "acme/widgets#v17.2.8"
    return inputs, bindings


def test_release_prompt_routes_fresh_and_resumed_sessions(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, _bindings = _release_inputs(tmp_path, settings, session_has_jsonl=False)
    monkeypatch.setattr(worker.persona, "kickoff_release", lambda **_kwargs: "fresh", raising=False)
    monkeypatch.setattr(worker.persona, "followup_release", lambda **_kwargs: "resumed", raising=False)
    kwargs = {
        "comment": None,
        "pr_number": None,
        "review_payload": None,
    }
    assert worker._build_prompt("handle_release_ci", inputs, resuming=False, **kwargs) == "fresh"
    assert worker._build_prompt("handle_release_ci", inputs, resuming=True, **kwargs) == "resumed"


@pytest.mark.asyncio
async def test_release_task_reminds_until_terminal_tool_runs(
    tmp_path: Path,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs, bindings = _release_inputs(tmp_path, settings, session_has_jsonl=False)
    monkeypatch.setattr(worker.persona, "system_append_release", lambda **_kwargs: "SYS RELEASE", raising=False)
    monkeypatch.setattr(worker.persona, "followup_release", lambda **_kwargs: "retag or abort", raising=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="handle_release_ci",
        prompt="kickoff",
        bindings=bindings,  # type: ignore[arg-type]
    )
    fake = _FakeRpcClient.instances[0]
    assert fake.kwargs["append_system_prompt"] == "SYS RELEASE"
    assert fake.kwargs["model"] in settings.release_model_pool
    assert fake.prompts == ["kickoff", *(["retag or abort"] * settings.task_completion_max_reminders)]


def test_rpc_pins_minimal_builtin_tools(tmp_path: Path, settings: Settings) -> None:
    """RpcClient must receive the explicit minimal built-in set (S22)."""
    from carter_omp.capabilities import OMP_BUILTIN_TOOLS, OMP_FORBIDDEN_BUILTINS

    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )
    tools = _FakeRpcClient.instances[0].kwargs["tools"]
    assert sorted(tools) == sorted(OMP_BUILTIN_TOOLS)
    assert not (set(tools) & set(OMP_FORBIDDEN_BUILTINS))


def test_rpc_tool_registry_excludes_forbidden_builtins(tmp_path: Path, settings: Settings) -> None:
    """Runtime registry check: forbidden built-ins absent from the pinned set."""
    from carter_omp.capabilities import OMP_BUILTIN_TOOLS, OMP_FORBIDDEN_BUILTINS

    for forbidden in OMP_FORBIDDEN_BUILTINS:
        assert forbidden not in OMP_BUILTIN_TOOLS
    assert "read" in OMP_BUILTIN_TOOLS and "bash" in OMP_BUILTIN_TOOLS
    assert "edit" in OMP_BUILTIN_TOOLS and "lsp" in OMP_BUILTIN_TOOLS


def test_rpc_env_scrubs_app_and_cloud_secrets(tmp_path: Path, settings: Settings) -> None:
    """Agent env must not carry App keys, proxy HMAC, or cloud credentials."""
    import os

    os.environ["AWS_SECRET_ACCESS_KEY"] = "bogus"
    os.environ["CARTER_OMP_GITHUB_APP_PRIVATE_KEY"] = "bogus"
    try:
        inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
        loop = asyncio.new_event_loop()
        try:
            worker._run_rpc_blocking(
                inputs,
                task_kind="triage_issue",
                prompt="x",
                bindings=bindings,  # type: ignore[arg-type]
            )
        finally:
            loop.close()
        env = _FakeRpcClient.instances[0].kwargs["env"]
        for key in (
            "GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_APP_PRIVATE_KEY",
            "CARTER_OMP_GITHUB_APP_PRIVATE_KEY",
            "CARTER_OMP_GH_PROXY_HMAC_KEY",
            "GITHUB_WEBHOOK_SECRET",
            "CARTER_OMP_REPLAY_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
        ):
            assert env.get(key, "") == "", key
    finally:
        del os.environ["AWS_SECRET_ACCESS_KEY"]
        del os.environ["CARTER_OMP_GITHUB_APP_PRIVATE_KEY"]


def test_write_fallback_chains_renders_default_overlay(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_home = tmp_path / "agent-home"
    monkeypatch.setattr(worker, "_AGENT_HOME", agent_home)
    cfg = settings.model_copy(update={"fallback_model": "openrouter/deepseek/deepseek-v4.1-flash, opencode-zen/x"})

    path = worker._write_fallback_chains(cfg)

    assert path == agent_home / ".omp" / "agent" / "carter-omp-fallback.yml"
    body = path.read_text(encoding="utf-8")
    assert "fallbackChains:" in body
    assert body.rstrip().endswith('default: ["openrouter/deepseek/deepseek-v4.1-flash", "opencode-zen/x"]')
    assert path.stat().st_mode & 0o777 == 0o644
    # Idempotent: a second call is a no-op rewrite, still one file.
    assert worker._write_fallback_chains(cfg) == path


def test_write_fallback_chains_unset_writes_nothing(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_home = tmp_path / "agent-home"
    monkeypatch.setattr(worker, "_AGENT_HOME", agent_home)

    assert worker._write_fallback_chains(settings) is None
    assert not agent_home.exists()


@pytest.mark.asyncio
async def test_run_rpc_passes_fallback_overlay_config(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_home = tmp_path / "agent-home"
    monkeypatch.setattr(worker, "_AGENT_HOME", agent_home)
    cfg = settings.model_copy(update={"fallback_model": "openrouter/deepseek/deepseek-v4.1-flash"})

    inputs, bindings = _make_inputs(tmp_path, cfg, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )

    extra_args = _FakeRpcClient.instances[0].kwargs["extra_args"]
    assert extra_args == ("--no-extensions", "--config", str(agent_home / ".omp" / "agent" / "carter-omp-fallback.yml"))


@pytest.mark.asyncio
async def test_run_rpc_omits_config_without_fallback(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker, "_AGENT_HOME", tmp_path / "agent-home")

    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    worker._run_rpc_blocking(
        inputs,
        task_kind="triage_issue",
        prompt="x",
        bindings=bindings,  # type: ignore[arg-type]
    )

    assert _FakeRpcClient.instances[0].kwargs["extra_args"] == ("--no-extensions",)


def test_run_token_ttl_outlives_the_task_budget(settings: Settings) -> None:
    """One token covers a whole run: a 10-minute TTL expired mid-run and every
    later mutation 401'd, leaving work committed but unpublished."""
    cfg = settings.model_copy(
        update={
            "task_timeout_seconds": 2400.0,
            "release_task_timeout_seconds": 3600.0,
            "task_timeout_hard_grace_seconds": 60.0,
        }
    )

    ttl = worker._run_token_ttl(cfg)

    assert ttl > 3600.0  # the longest task budget the run can be given
    assert ttl >= int(3600.0 + 60.0)


def test_attach_run_token_mints_with_the_run_budget(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    from carter_omp import run_token as run_token_module
    from carter_omp.capabilities import Capability
    from carter_omp.github_events import TriggerContext

    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    inputs.trigger = TriggerContext(
        run_id="run-1",
        delivery_id="d1",
        repository_id=1,
        repository_full_name="acme/widgets",
        installation_id=2,
        actor_id=3,
        actor_login="carterlasalle",
        actor_type="User",
        event_type="issues",
        action="labeled",
        trigger_kind="label",
        trigger_object_id=None,
        trigger_value="carter-omp",
        issue_number=1,
        pull_request_number=None,
        capabilities=frozenset({Capability.COMMENT}),
        policy_version="v1",
        authorized_at=datetime(2026, 10, 6, tzinfo=UTC),
    )
    seen: dict[str, object] = {}

    def _fake_mint(**kwargs: object) -> str:
        seen.update(kwargs)
        return "token"

    monkeypatch.setattr(run_token_module, "mint_run_token", _fake_mint)

    worker._attach_run_token(inputs, bindings)

    assert seen["ttl_seconds"] == worker._run_token_ttl(settings)
    assert seen["issue"] == 1


def test_record_agent_abort_marks_the_delivery_failed(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent abort must not look green: a run that published nothing used to
    end as `done` with the reason only in a log line."""
    recorded: dict[str, object] = {}

    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    inputs.db = SimpleNamespace(  # type: ignore[assignment]
        mark_event=lambda delivery, state, error=None: recorded.update(
            {"delivery": delivery, "state": state, "error": error}
        )
    )
    bindings.abort = worker.host_tools.AbortController()
    bindings.abort.signal("Harness credential fault: gh_push_branch rejected 401")

    worker._record_agent_abort(inputs, bindings)

    assert recorded["delivery"] == inputs.delivery_id
    assert recorded["state"] == "failed"
    assert "401" in str(recorded["error"])


def test_record_agent_abort_is_a_noop_without_an_abort(tmp_path: Path, settings: Settings) -> None:
    calls: list[object] = []
    inputs, bindings = _make_inputs(tmp_path, settings, session_has_jsonl=False)
    inputs.db = SimpleNamespace(mark_event=lambda *a, **k: calls.append((a, k)))  # type: ignore[assignment]
    bindings.abort = worker.host_tools.AbortController()

    worker._record_agent_abort(inputs, bindings)

    assert calls == []


def test_agent_env_disables_husky(tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repo lifecycle scripts must not rewrite SHARED git metadata: husky's
    `prepare` runs `git config core.hooksPath`, which rewrote the pool's
    `.git/config` as the invoking slot's uid:gid and locked other slots out."""
    monkeypatch.setattr(worker, "_AGENT_HOME_STAGE", tmp_path / "missing-stage")
    monkeypatch.setattr(worker, "_AGENT_HOME", tmp_path / "agent-home")

    env = worker._build_extra_env(settings)

    assert env["HUSKY"] == "0"
    assert env["HUSKY_SKIP_INSTALL"] == "1"


def test_agent_env_disables_pty(tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The child died mid-turn with `EPIPE: broken pipe, write` (Bun stream
    teardown) on runs that use bash heavily; nothing here is interactive."""
    monkeypatch.setattr(worker, "_AGENT_HOME_STAGE", tmp_path / "missing-stage")
    monkeypatch.setattr(worker, "_AGENT_HOME", tmp_path / "agent-home")

    assert worker._build_extra_env(settings)["PI_NO_PTY"] == "1"


@pytest.mark.asyncio
async def test_consume_trigger_label_uses_the_run_scoped_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #15: label consumption called `inputs.github` (the shared HMAC-only
    client), which the proxy's label endpoints reject with 401, so the trigger
    label was never removed and `:running` was never applied."""
    from datetime import UTC, datetime

    import httpx

    from carter_omp.capabilities import Capability
    from carter_omp.github_events import TriggerContext
    from carter_omp.proxy_client import GitHubProxyClient
    from tests.test_proxy_server import _HMAC, _build_app, _build_settings

    proxy_settings = _build_settings(tmp_path)
    gh_calls: list[str] = []

    def gh(req: httpx.Request) -> httpx.Response:
        gh_calls.append(f"{req.method} {req.url.path}")
        return httpx.Response(200, json=[])

    client = GitHubProxyClient(
        base_url="http://proxy.test",
        hmac_key=_HMAC,
        transport=httpx.ASGITransport(app=_build_app(proxy_settings, gh)),
    )

    inputs, _bindings = _make_inputs(tmp_path, proxy_settings, session_has_jsonl=False)
    inputs.github = client
    inputs.trigger = TriggerContext(
        run_id="run-1",
        delivery_id="d-1",
        repository_id=1,
        repository_full_name="octo/widget",
        installation_id=2,
        actor_id=3,
        actor_login="carterlasalle",
        actor_type="User",
        event_type="issues",
        action="labeled",
        trigger_kind="label",
        trigger_object_id=None,
        trigger_value="carter-omp",
        issue_number=1,
        pull_request_number=None,
        capabilities=frozenset({Capability.COMMENT, Capability.LABEL}),
        policy_version="v1",
        authorized_at=datetime(2026, 10, 6, tzinfo=UTC),
    )

    monkeypatch.setattr(worker, "_build_prompt", lambda *args, **kwargs: "prompt")

    def fake_run_rpc_blocking(_inputs, *, task_kind, prompt, bindings, directive=None):
        del task_kind, prompt, directive
        return "ok"

    monkeypatch.setattr(worker, "_run_rpc_blocking", fake_run_rpc_blocking)

    await worker.run_task(task_kind="triage_issue", inputs=inputs)

    assert "DELETE /repos/octo/widget/issues/1/labels/carter-omp" in gh_calls
    assert "POST /repos/octo/widget/issues/1/labels" in gh_calls
