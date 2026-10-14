"""The IQB-14 runner.

Executes a task suite against one agent target and writes one JSON artifact per instance plus a
trajectory JSONL per instance. Everything downstream (scoring, reporting, comparing) is a pure
function of those artifacts, so results can be re-scored, re-analysed or disputed without spending
another token -- which is the property that separates a benchmark from a script.

Ordering guarantees:

* An instance is only marked `completed` if the agent returned normally. Budget exhaustion,
  harness errors, missing credentials and API failures all get their own status and are reported
  separately (see state.STATUSES).
* Results are appended to `instances.jsonl` as they finish, and a resumed run skips task/attempt
  pairs already present. Killing a suite halfway never loses completed work.
* The workspace is hashed before and after, so "the agent changed something" is provable rather
  than inferred from its own prose.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from .assertions import AssertionContext, run_assertions
from .guard import (
    MAX_ATTEMPT_RETRIES,
    RETRY_BACKOFF_SECONDS,
    Budget,
    BudgetExceeded,
    BudgetGuard,
    is_retryable,
)
from .parameters import STANDARD_ID, STANDARD_VERSION
from .prices import PRICE_TABLE_DATE, estimate_cost_usd
from .scoring import InstanceRecord
from .state import (
    NON_ATTRIBUTABLE,
    STATUS_AGENT_ERROR,
    STATUS_BUDGET_EXCEEDED,
    STATUS_COMPLETED,
    STATUS_ENV_ERROR,
    STATUS_HARNESS_ERROR,
    STATUS_TIMEOUT,
    RunState,
    TraceRecorder,
)
from .tasks import TaskSpec, materialize

DEFAULT_TIMEOUT_GRACE_S = 60.0


@dataclass
class Target:
    """The thing under test: provider + model + endpoint + sampling settings."""

    name: str
    provider: str
    model: str
    base_url: str = ""
    api_key: str = ""
    api_key_env: str = ""
    temperature: float | None = None
    seed: int | None = None
    max_tokens: int | None = None
    effort: str | None = None
    mode: str = "auto_edit"
    yolo: bool = True

    def resolve_key(self) -> str:
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "temperature": self.temperature,
            "seed": self.seed,
            "max_tokens": self.max_tokens,
            "effort": self.effort,
            "mode": self.mode,
            "yolo": self.yolo,
            "api_key_present": bool(self.resolve_key()),
        }


def build_agent(target: Target, workspace: Path, budget: Budget):
    """Construct one agent for one instance.

    Imported lazily so the bench package stays importable (and unit-testable) with no provider SDK
    installed and no API key present -- a scoring change must be testable in CI without network.
    """
    from rich.console import Console

    from ..constants import (
        DEFAULT_MAX_TOKENS,
        DEFAULT_MAX_TOKENS_OPENAI_COMPAT,
        PROVIDER_ANTHROPIC,
        PROVIDER_DEEPSEEK,
        PROVIDER_GEMINI,
        PROVIDER_OPENROUTER,
    )

    console = Console(quiet=True)
    key = target.resolve_key()
    max_tokens = target.max_tokens or (
        DEFAULT_MAX_TOKENS if target.provider == PROVIDER_ANTHROPIC else DEFAULT_MAX_TOKENS_OPENAI_COMPAT
    )
    tools = None
    if budget.max_subagent_depth <= 0:
        # Depth 0 means "no delegation at all" -- measured capability of the top-level agent only.
        # Built through the same registry so the tool set is otherwise identical.
        from .. import tool_registry

        if target.provider == PROVIDER_ANTHROPIC:
            tools = tool_registry.build_tool_defs(include_task=False)
        elif target.provider == PROVIDER_GEMINI:
            tools = tool_registry.build_tool_defs_gemini(include_task=False)
        else:
            tools = tool_registry.build_tool_defs_openai(include_task=False)

    if target.provider in (PROVIDER_DEEPSEEK, PROVIDER_OPENROUTER) or target.provider == "openai_compat":
        import openai

        from ..openai_agent import OpenAIAgent

        client = openai.OpenAI(api_key=key, base_url=target.base_url or None, max_retries=3, timeout=180.0)
        agent = OpenAIAgent(
            client,
            target.model,
            console,
            yolo=target.yolo,
            max_tokens=max_tokens,
            tools=tools,
            effort=target.effort,
            mode=target.mode,
            provider=target.provider,
        )
    elif target.provider == PROVIDER_GEMINI:
        import google.genai as genai

        from ..gemini_agent import GeminiAgent

        client = genai.Client(api_key=key, http_options={"base_url": target.base_url} if target.base_url else None)
        agent = GeminiAgent(client, target.model, console, yolo=target.yolo, tools=tools, mode=target.mode)
    elif target.provider == PROVIDER_ANTHROPIC:
        import anthropic

        from ..agent import Agent

        client = anthropic.Anthropic(api_key=key, base_url=target.base_url or None, max_retries=3, timeout=180.0)
        agent = Agent(client, target.model, console, yolo=target.yolo, max_tokens=max_tokens, tools=tools, effort=target.effort, mode=target.mode)
    else:
        raise ValueError(f"unsupported provider for benchmarking: {target.provider!r}")

    # Sampling knobs are attached post-construction so adding benchmark support never widened the
    # production constructor signature; providers that don't read them simply ignore them, and the
    # manifest records the request either way.
    agent.temperature = target.temperature
    agent.seed = target.seed
    if budget.max_tokens_per_request:
        agent.max_tokens = min(agent.max_tokens, budget.max_tokens_per_request)
        if hasattr(agent, "tool_context"):
            agent.tool_context.max_tokens = agent.max_tokens
    return agent


def workspace_digest(workspace: Path) -> dict[str, str]:
    """SHA-256 per file, excluding harness and session bookkeeping.

    The digest is what makes "the agent modified the repo" checkable by a third party, and what
    lets a re-scorer tell whether a verifier ran against the state the agent actually left behind.
    """
    digests: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(workspace).as_posix()
        if relative.startswith(".myriaxon/"):
            continue
        try:
            digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            digests[relative] = "unreadable"
    return digests


def _run_setup(spec: TaskSpec, workspace: Path, timeout: float = 300.0) -> tuple[bool, str]:
    if not spec.setup:
        return True, ""
    try:
        proc = subprocess.run(
            spec.setup,
            cwd=workspace,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return False, f"setup timed out after {timeout}s"
    return proc.returncode == 0, f"setup rc={proc.returncode} out={proc.stdout[-400:]!r} err={proc.stderr[-400:]!r}"


@dataclass
class RunnerConfig:
    suite: list[TaskSpec]
    target: Target
    out_dir: Path
    attempts: int | None = None
    # Optional callable(prompt, model) -> str used by llm-rubric assertions. Type annotation is
    # required, not cosmetic: without it this is a plain class attribute rather than a dataclass
    # field, and the constructor silently rejects the keyword.
    judge_client: object | None = None
    judge_model: str | None = None
    timeout_grace_s: float = DEFAULT_TIMEOUT_GRACE_S
    keep_workspaces: bool = False
    resume: bool = True
    dry_run: bool = False
    progress: object | None = None
    # Where per-instance scratch workspaces are created. Defaults to the OS temp directory, but
    # restricted environments (and any run that must be reproducible from a known path) can point
    # it inside the artifact directory instead.
    workspace_root: Path | None = None
    extra_manifest: dict = field(default_factory=dict)


class Runner:
    def __init__(self, config: RunnerConfig) -> None:
        self.config = config
        self.out_dir = config.out_dir
        self.instances_dir = self.out_dir / "instances"
        self.traces_dir = self.out_dir / "traces"
        self.workspaces_dir = self.out_dir / "workspaces"
        self.instances_jsonl = self.out_dir / "instances.jsonl"
        self.records: list[InstanceRecord] = []
        self.budget_enforced: bool | None = None

    # -- persistence -------------------------------------------------------------
    def _load_existing(self) -> set[tuple[str, int]]:
        done: set[tuple[str, int]] = set()
        if not self.config.resume or not self.instances_jsonl.exists():
            return done
        for line in self.instances_jsonl.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            record = InstanceRecord.from_dict(raw)
            self.records.append(record)
            done.add((record.task_id, record.attempt))
        return done

    def _write_instance(self, record: InstanceRecord) -> None:
        self.instances_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(record.to_dict(), indent=2, default=str)
        (self.instances_dir / f"{record.task_id}__a{record.attempt}__{record.variant}.json").write_text(
            payload, encoding="utf-8"
        )
        with self.instances_jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.to_dict(), default=str) + "\n")

    # -- one instance ------------------------------------------------------------
    def run_instance(self, spec: TaskSpec, attempt: int, variant_index: int) -> InstanceRecord:
        state = RunState(task_id=spec.id, attempt=attempt)
        state.model = self.config.target.model
        state.provider = self.config.target.provider
        state.variants = {"variant": spec.variant_name(variant_index)}
        # NOT tempfile.mkdtemp: Python creates those with mode 0700, which restricted environments
        # (including the sandboxed shell this harness was developed in) refuse to write into,
        # producing a PermissionError on the very first fixture file. A plain mkdir uses normal
        # permissions, and naming the directory after the instance makes a kept workspace
        # self-identifying during debugging.
        root = self.config.workspace_root or Path(tempfile.gettempdir())
        root.mkdir(parents=True, exist_ok=True)
        workspace = root / f"{spec.id}__a{attempt}__{spec.variant_name(variant_index)}"
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)
        state.workspace = str(workspace) if self.config.keep_workspaces else ""

        recorder = TraceRecorder(state)
        try:
            materialize(spec, workspace)
            ok, setup_detail = _run_setup(spec, workspace)
            if not ok:
                state.status = STATUS_ENV_ERROR
                state.detail = setup_detail
                return self._finish(spec, attempt, variant_index, state, recorder, workspace)
            if spec.setup:
                state.extras["setup"] = setup_detail

            state.extras["digest_before"] = workspace_digest(workspace)

            previous_cwd = os.getcwd()
            os.chdir(workspace)
            try:
                agent = build_agent(self.config.target, workspace, spec.budget)
                guard = BudgetGuard(budget=spec.budget, model=self.config.target.model)
                enforced = guard.install(agent)
                # The guard reads the live trace length, so the step ceiling counts tool calls that
                # actually executed -- including those made by sub-agents -- instead of a counter
                # the agent loop would have to remember to increment.
                guard.step_source = recorder.step_count
                self.budget_enforced = enforced if self.budget_enforced is None else (self.budget_enforced and enforced)
                state.extras["budget_enforced"] = enforced
                recorder.usage_source = agent
                agent.tool_context.bench_trace = recorder
                agent.tool_context.bench_guard = guard
                # Sub-agents get their own ToolContext at construction; rebind the factory so the
                # whole tree reports into this instance's trace. (Known limitation, documented:
                # concurrent `parallel_tasks` sub-agents share this attribute, so their per-call
                # metadata may interleave -- step counts stay correct, attribution may not.)
                try:
                    import functools

                    original_factory = agent.tool_context.agent_factory
                    if original_factory is not None:
                        def _hooked_factory(*args, **kwargs):
                            sub = original_factory(*args, **kwargs)
                            rebind = getattr(sub, "tool_context", None)
                            if rebind is not None:
                                rebind.bench_trace = recorder
                                rebind.bench_guard = guard
                                rebind.max_tokens = agent.tool_context.max_tokens
                            guard.install(sub)
                            return sub

                        agent.tool_context.agent_factory = _hooked_factory
                except Exception:  # noqa: BLE001 -- sub-agent instrumentation is best-effort
                    pass

                recorder.turn = 0
                prompt = spec.prompt_for_variant(variant_index)
                state.started_at = time.monotonic()
                self._run_turn(agent, recorder, prompt, spec, guard)
                state.status = STATUS_COMPLETED if state.status == "pending" else state.status
            finally:
                os.chdir(previous_cwd)

            state.input_tokens = int(getattr(agent.usage_totals, "input_tokens", 0) or 0)
            state.output_tokens = int(getattr(agent.usage_totals, "output_tokens", 0) or 0)
            state.cost_usd = estimate_cost_usd(self.config.target.model, state.input_tokens, state.output_tokens)
            state.extras.setdefault("request_count", int(getattr(agent.usage_totals, "request_count", 0) or 0))
            state.extras["steps"] = guard.current_steps()
            state.extras["budget"] = spec.budget.to_dict()
            state.extras["guard_seconds"] = round(guard.elapsed(), 2)
        except BudgetExceeded as exc:
            # Reachable when a check fires outside the wrapped turn call. Recorded as a distinct
            # status so it is never read as "the model failed the task". Consumption is recorded
            # here too -- a budget abort with no step/step-time figures attached would be
            # undiagnosable, which is exactly the situation the budget exists to make diagnosable.
            state.status = STATUS_BUDGET_EXCEEDED
            state.detail = str(exc)
            state.extras["steps"] = exc.steps
            state.extras["budget"] = spec.budget.to_dict()
            state.extras["guard_seconds"] = round(exc.seconds, 2)
            state.extras["budget_cost_usd"] = exc.cost_usd
        except Exception as exc:  # noqa: BLE001 -- classify, never let one instance kill the suite
            state.status, state.detail = _classify_exception(exc)
            if state.status == STATUS_HARNESS_ERROR:
                state.extras["traceback"] = traceback.format_exc()[-4000:]
        finally:
            state.finished_at = state.finished_at or time.monotonic()

        return self._finish(
            spec,
            attempt,
            variant_index,
            state,
            recorder,
            workspace,
        )


    def _run_turn(self, agent, recorder: TraceRecorder, prompt: str, spec: TaskSpec, guard: BudgetGuard) -> None:
        """One agent turn, with a last-resort wall-clock collar.

        The budget guard already stops the loop between requests; this outer deadline covers the
        case where a single tool call never returns. Overshoot is therefore bounded by whichever is
        larger: one model request, or one tool call's own timeout (run_bash defaults to 120s). That
        bound is stated in the standard rather than left implicit.
        """
        deadline = (spec.budget.max_seconds or 0) + self.config.timeout_grace_s
        start = time.monotonic()
        recorder.turn = 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(agent.run_turn, prompt)
            try:
                future.result(timeout=deadline)
                recorder.note_turn(prompt, agent.last_assistant_text(), (time.monotonic() - start) * 1000.0)
            except concurrent.futures.TimeoutError:
                # The pool thread is abandoned deliberately: the process would otherwise hang, and a
                # partial trace plus a `timeout` status is strictly more useful than no result.
                recorder.note_turn(prompt, "", (time.monotonic() - start) * 1000.0, error="wall_clock_timeout")
                raise _TurnTimedOut(f"turn exceeded {deadline:.0f}s")
        # Backstop only. The real enforcement happens inside the loop (before every request) and at
        # the tool-call boundary; this catches a budget that was exactly reached on the final
        # request, after which the loop exited without issuing another one.
        guard.check_after_tool_call()

    def _finish(
        self,
        spec: TaskSpec,
        attempt: int,
        variant_index: int,
        state: RunState,
        recorder: TraceRecorder,
        workspace: Path,
    ) -> InstanceRecord:
        if not state.started_at:
            state.started_at = state.finished_at or time.monotonic()
        if not state.finished_at:
            state.finished_at = time.monotonic()
        totals = getattr(recorder.usage_source, "usage_totals", None)
        if totals is not None:
            state.input_tokens = state.input_tokens or int(getattr(totals, "input_tokens", 0) or 0)
            state.output_tokens = state.output_tokens or int(getattr(totals, "output_tokens", 0) or 0)
            state.extras.setdefault("request_count", int(getattr(totals, "request_count", 0) or 0))
        if state.cost_usd is None:
            state.cost_usd = estimate_cost_usd(state.model, state.input_tokens, state.output_tokens)

        variant_name = spec.variant_name(variant_index)
        try:
            state.extras.setdefault("digest_after", workspace_digest(workspace))
        except OSError as exc:
            state.extras["digest_error"] = repr(exc)

        final_text = state.turns[-1].final_text if state.turns else ""
        results = []
        if state.status == STATUS_COMPLETED:
            ctx = AssertionContext(workspace=workspace, state=state, task=spec.to_dict(), final_text=final_text)
            try:
                results = run_assertions(
                    spec.assertions_for_variant(variant_index), ctx, judge_client=self.config.judge_client
                )
            except Exception as exc:  # noqa: BLE001 -- verifier bug is a harness error
                state.status = STATUS_HARNESS_ERROR
                state.detail = f"assertion engine failed: {exc!r}"
                state.extras["traceback"] = traceback.format_exc()[-4000:]

        passed = bool(results) and all(r.passed for r in results if _is_critical(r))
        if not results and state.status == STATUS_COMPLETED:
            state.status = STATUS_HARNESS_ERROR
            state.detail = "no assertions produced a verdict (task file declares none?)"

        record = InstanceRecord(
            task_id=spec.id,
            kind=spec.kind,
            attempt=attempt,
            variant=variant_name,
            state=state,
            assertions=results,
            passed=passed and state.status == STATUS_COMPLETED,
            spec={
                "id": spec.id,
                "kind": spec.kind,
                "prompt": spec.prompt_for_variant(variant_index),
                "budget": spec.budget.to_dict(),
                "notes": spec.notes,
                "tags": spec.tags,
                "consistency_mode": spec.consistency_mode,
                "complexity": spec.complexity,
            },
            error=state.detail or None,
        )
        recorder.write_jsonl(self.traces_dir / f"{spec.id}__a{attempt}__{variant_name}.jsonl")
        self._write_instance(record)
        if self.config.keep_workspaces:
            target = self.workspaces_dir / f"{spec.id}__a{attempt}__{variant_name}"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(workspace, target, dirs_exist_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)
        return record

    # -- whole suite -------------------------------------------------------------
    def run(self) -> list[InstanceRecord]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        done = self._load_existing()
        for spec in self.config.suite:
            plan = spec.attempt_plan()
            if self.config.attempts:
                plan = (plan * ((self.config.attempts // max(1, len(plan))) + 1))[: self.config.attempts]
            for attempt, variant_index in enumerate(plan):
                if (spec.id, attempt) in done:
                    _report(self.config.progress, f"skip  {spec.id} a{attempt} (already recorded)")
                    continue
                if self.config.dry_run:
                    _report(self.config.progress, f"plan  {spec.id} a{attempt} variant={spec.variant_name(variant_index)}")
                    continue
                _report(self.config.progress, f"run   {spec.id} a{attempt} variant={spec.variant_name(variant_index)}")
                record = self._run_with_retries(spec, attempt, variant_index)
                self.records.append(record)
                _report(
                    self.config.progress,
                    f"  -> {record.state.status} passed={record.passed} "
                    f"steps={record.state.extras.get('steps')} {record.state.wall_clock_s:.1f}s",
                )
                self._check_run_viability(record)
        return self.records

    def _run_with_retries(self, spec: TaskSpec, attempt: int, variant_index: int) -> InstanceRecord:
        """Retry transient provider failures, never deterministic ones.

        Measured need: a single 402 mid-suite cascaded into 13 consecutive unusable instances. Each
        was correctly excluded as non-attributable, so the *score* stayed honest -- but the run lost
        most of its evidence, which is its own kind of failure. Rate limits and 5xx are retried with
        backoff; bad credentials and exhausted credit are not (retrying those only wastes wall-clock
        and hides the real problem, which `_check_run_viability` then reports loudly).
        """
        last: InstanceRecord | None = None
        for retry in range(MAX_ATTEMPT_RETRIES + 1):
            record = self.run_instance(spec, attempt, variant_index)
            if record.state.status not in ("agent_error", "harness_error"):
                return record
            last = record
            if retry >= MAX_ATTEMPT_RETRIES or not is_retryable_from_record(record):
                return record
            delay = RETRY_BACKOFF_SECONDS * (2**retry)
            _report(
                self.config.progress,
                f"      transient failure ({record.state.detail[:80]}) -- retrying in {delay:.0f}s "
                f"[{retry + 1}/{MAX_ATTEMPT_RETRIES}]",
            )
            time.sleep(delay)
        return last  # pragma: no cover -- loop always returns

    def _check_run_viability(self, record: InstanceRecord) -> None:
        """Abort the suite when the failure is clearly environment-wide, instead of grinding through
        every remaining task producing non-attributable instances (and burning wall-clock)."""
        if record.state.status not in NON_ATTRIBUTABLE:
            self._consecutive_unusable = 0
            return
        self._consecutive_unusable = getattr(self, "_consecutive_unusable", 0) + 1
        if self._consecutive_unusable >= 3:
            raise EnvAbort(
                f"{self._consecutive_unusable} consecutive non-attributable instances "
                f"(last: {record.state.detail[:200]}). Stopping the suite -- the environment, not the "
                f"agent, is failing. Fix it and re-run: completed instances are already on disk and "
                f"will be skipped."
            )

    def manifest(self, suite: list[TaskSpec]) -> dict:
        return {
            "standard": f"{STANDARD_ID}@{STANDARD_VERSION}",
            "target": self.config.target.to_dict(),
            "generated_at": time.time(),
            "suite_size": len(suite),
            "kinds": sorted({s.kind for s in suite}),
            "task_ids": [s.id for s in suite],
            "attempts_cap": self.config.attempts,
            "judge_model": self.config.judge_model,
            "budget_enforced": self.budget_enforced,
            "price_table_date": PRICE_TABLE_DATE,
            "python": sys.version.split()[0],
            "cwd": os.getcwd(),
            **self.config.extra_manifest,
        }


class _TurnTimedOut(Exception):
    pass


class EnvAbort(Exception):
    """Raised to stop a suite when the environment is failing, not the agent.

    Distinct from a crash: the caller converts it into a "run incomplete" report over whatever
    instances were completed, so the artifacts stay usable.
    """


def is_retryable_from_record(record: InstanceRecord) -> bool:
    """Retry decision from a finished instance's own recorded detail.

    Goes through the same `is_retryable` predicate as live exceptions so the two cannot drift: a
    marker added for exception classification automatically applies to recorded ones too.
    """
    text = f"{record.state.status} {record.state.detail}"
    if record.state.status == "agent_error":
        return not any(
            marker in text.lower()
            for marker in ("invalid api key", "authentication", "unauthorized", "401", "403", "402", "insufficient credit", "quota")
        )
    if record.state.status == "harness_error":
        # A harness bug will reproduce identically on retry; only retry if it looks like a
        # transient transport problem that surfaced wrapped in an unexpected exception type.
        return any(
            marker in text.lower() for marker in ("timeout", "timed out", "connection", "429", "502", "503", "504")
        )
    return False


def _is_critical(result) -> bool:
    """An assertion is a gate unless the task explicitly marks it advisory (critical: false).

    Advisory assertions still contribute to the graded score but cannot by themselves fail a task;
    that lets a task measure a soft property (grounding quality, cost) without turning the whole
    instance binary on it.
    """
    return bool(getattr(result, "critical", True))


def _classify_exception(exc: Exception) -> tuple[str, str]:
    """Map an exception to a status without blaming the agent for the harness's problems."""
    name = type(exc).__name__
    text = str(exc)
    if isinstance(exc, _TurnTimedOut):
        return STATUS_TIMEOUT, text
    if isinstance(exc, (KeyboardInterrupt, SystemExit)):
        return STATUS_HARNESS_ERROR, "interrupted"
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return STATUS_ENV_ERROR, f"missing dependency: {text}"
    if isinstance(exc, (PermissionError, FileNotFoundError, NotADirectoryError)):
        return STATUS_ENV_ERROR, f"{name}: {text}"
    if isinstance(exc, BudgetExceeded):
        return STATUS_BUDGET_EXCEEDED, text
    lowered = f"{name} {text}".lower()
    if any(
        marker in lowered
        for marker in ("insufficient credit", "402", "quota exceeded", "billing", "payment required")
    ):
        return STATUS_ENV_ERROR, f"account/credit problem: {text[:300]}"
    if any(marker in lowered for marker in ("authentication", "api key", "unauthorized", "401", "403", "invalid_api_key")):
        return STATUS_ENV_ERROR, f"credential problem: {name}: {text[:300]}"
    if any(marker in lowered for marker in ("timeout", "timed out", "connection", "rate limit", "429", "500", "502", "503", "504", "overloaded")):
        return STATUS_AGENT_ERROR, f"upstream/API failure: {name}: {text[:300]}"
    return STATUS_HARNESS_ERROR, f"{name}: {text[:300]}"


def _report(progress, message: str) -> None:
    if progress is None:
        print(message, flush=True)
    elif callable(progress):
        progress(message)
