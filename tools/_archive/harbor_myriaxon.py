"""Harbor agent adapter: run Myriaxon inside a Terminal-Bench task container.

Harbor drives agents through two shapes. `BaseAgent` runs the agent loop on the *host* and controls
the container through `environment.exec`; `BaseInstalledAgent` installs the agent *into* the
container and runs it there. This is the second, because Myriaxon is a coding agent whose whole value
is reading and writing the task's real filesystem with its own tools -- driving it from outside would
measure a wrapper, not the harness.

Getting Myriaxon into the container without a public package
------------------------------------------------------------
Myriaxon is not on PyPI and its GitHub remote does not exist yet (verified: the repo API returns 404),
so there is nothing for the container to `pip install`. Harbor's `environment.upload_dir` uploads from
the host, so the adapter builds a wheel locally and uploads that. The wheel is built from the working
tree, which means the adapter measures *this checkout* -- stated plainly because it makes a run
irreproducible unless the tree is committed.

Model credentials
-----------------
The task container has to reach a model. Myriaxon reads provider settings from its own config, and the
container has none, so the adapter writes one from the values Harbor passes in (`--model`, and the
provider endpoint and key from `--ae`/environment). Nothing is hard-coded: a missing key fails the
install step with a message saying which variable to set, rather than running a turn that cannot work.

Usage:

    .venv-harbor\\Scripts\\python.exe tools/harbor_main.py run \\
        -d terminal-bench@2.0 -i mailman \\
        -a tools.harbor_myriaxon:MyriaxonAgent \\
        -m deepseek/deepseek-v4.1-flash \\
        --ak base_url=https://api.netraruntime.com/v1 \\
        --ae MYRIAXON_API_KEY=... \\
        -e daytona --env-file .env.daytona -n 1 -y
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from harbor.agents.installed.base import BaseInstalledAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
WHEEL_DIR = REPO / ".uv" / "wheel"
CONFIG_DIR = "/tmp/myriaxon-config"


class MyriaxonAgent(BaseInstalledAgent):
    """Myriaxon, installed and run inside the task container."""

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        base_url: str = "",
        provider: str = "openai_compat",
        max_steps: int = 60,
        **kwargs,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        self.base_url = base_url
        self.provider = provider
        self.max_steps = max_steps
        self._wheel_name = ""

    @staticmethod
    def name() -> str:
        return "myriaxon"

    def version(self) -> str | None:
        try:
            sys.path.insert(0, str(REPO))
            from myriaxon.constants import VERSION
            return VERSION
        except Exception:  # noqa: BLE001
            return None

    # -- build ---------------------------------------------------------------
    def _build_wheel(self) -> Path:
        """Build a wheel from this checkout. Cached, because it is the slow part."""
        WHEEL_DIR.mkdir(parents=True, exist_ok=True)
        existing = sorted(WHEEL_DIR.glob("myriaxon-*.whl"))
        if existing:
            self._wheel_name = existing[0].name
            return existing[0]

        result = subprocess.run(
            [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
             "-w", str(WHEEL_DIR), str(REPO)],
            capture_output=True, text=True, timeout=900, cwd=str(REPO),
        )
        if result.returncode != 0:
            raise RuntimeError(
                "could not build the Myriaxon wheel:\n"
                + (result.stderr or result.stdout or "")[-2000:]
            )
        built = sorted(WHEEL_DIR.glob("myriaxon-*.whl"))
        if not built:
            raise RuntimeError(f"pip wheel reported success but wrote nothing into {WHEEL_DIR}")
        self._wheel_name = built[0].name
        return built[0]

    # -- harbor hooks --------------------------------------------------------
    async def install(self, environment: BaseEnvironment) -> None:
        """Put Myriaxon and its config into the container."""
        wheel = self._build_wheel()

        await self.exec_as_root(
            environment,
            command="apt-get update -qq && apt-get install -y -qq python3 python3-pip python3-venv git",
            timeout_sec=600,
        )

        # A venv, because the task images vary in how their system Python is managed and PEP 668
        # refuses a plain install on the newer ones.
        await self.exec_as_root(
            environment,
            command="python3 -m venv /opt/myriaxon-venv",
            timeout_sec=300,
        )

        await environment.upload_dir(
            source_dir=wheel.parent, target_dir="/tmp/myriaxon-wheel"
        )
        await self.exec_as_root(
            environment,
            command=(
                "/opt/myriaxon-venv/bin/pip install --quiet --no-cache-dir "
                f"/tmp/myriaxon-wheel/{shlex.quote(wheel.name)}"
            ),
            timeout_sec=900,
        )

        # The agent reads provider settings from its own config directory. Writing one here is what
        # lets a container with no home directory and no wizard use the model Harbor was given.
        #
        # This is uploaded as a FILE rather than echoed by a shell command, and that is not a style
        # choice. The first version built the config with
        # `printf '%s' '{"api_key": "..."}' > config.json`, and Harbor logs every command it runs --
        # so a real credential was written verbatim into `trial.log` on every run. Confirmed in the
        # log of the first successful install. A secret must not travel through an argument, an
        # environment variable passed to a logged command, or anything else that gets recorded.
        key = (
            self._get_env("MYRIAXON_API_KEY")
            or self._get_env("OPENAI_API_KEY")
            or self._get_env("DEEPSEEK_API_KEY")
            or ""
        )
        if not key:
            raise RuntimeError(
                "no model API key reached the adapter. The launchers (tools/tb_run.py, "
                "tools/wsl_tb_run.py) set MYRIAXON_API_KEY in the environment from Myriaxon's own "
                "config -- run the benchmark through one of those rather than invoking harbor "
                "directly. Without a key every turn fails with an authentication error and the "
                "score would measure nothing."
            )
        model = self.model_name or "deepseek/deepseek-v4.1-flash"
        config = {
            "provider": self.provider,
            "api_key": key,
            "base_url": self.base_url,
            "model": model,
        }

        staging = Path(tempfile.mkdtemp(prefix="myriaxon-config-"))
        try:
            local_config = staging / "config.json"
            local_config.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
            try:
                os.chmod(local_config, 0o600)
            except OSError:
                pass

            await self.exec_as_root(
                environment,
                command=f"mkdir -p {CONFIG_DIR} && chmod 700 {CONFIG_DIR}",
                timeout_sec=120,
            )
            await environment.upload_file(
                source_path=str(local_config), target_path=f"{CONFIG_DIR}/config.json"
            )
            await self.exec_as_root(
                environment,
                command=f"chmod 600 {CONFIG_DIR}/config.json && chown -R root:root {CONFIG_DIR}",
                timeout_sec=120,
            )
        finally:
            # Removed even on failure: the staging copy is a plaintext credential on the host's disk,
            # and leaving it there would recreate the exposure this design exists to avoid.
            shutil.rmtree(staging, ignore_errors=True)

    async def run(
        self, instruction: str, environment: BaseEnvironment, context: AgentContext
    ) -> None:
        """One Myriaxon turn against the task instruction, from the task's working directory."""
        install_check = await self.exec_as_agent(
            environment,
            command="/opt/myriaxon-venv/bin/python -m myriaxon --version",
            timeout_sec=120,
        )
        # `ExecResult.return_code` is a required int (harbor/environments/base.py), so there is no
        # missing-attribute case to guard -- the previous `getattr(..., 0)` default would have made a
        # renamed field look like success, and the field is named what it is used as.
        code = getattr(install_check, "return_code", None)
        if code != 0:
            # The reason has to travel. Harbor records the command and then "Command outputs captured",
            # so without this a trial that fails to install reports only its exit code and the next
            # four-minute run tells you nothing new.
            raise RuntimeError(
                f"Myriaxon is not runnable in the container (exit {code}).\n"
                f"  stdout: {(getattr(install_check, 'stdout', '') or '')[:600]}\n"
                f"  stderr: {(getattr(install_check, 'stderr', '') or '')[:600]}"
            )

        # The task's own directory. This is where the work has to happen: the verifier reads
        # `/app/regex.txt` and similar absolute paths, so an agent that runs elsewhere produces
        # nothing and scores zero while reporting success.
        #
        # The first version of this used `cd /app 2>/dev/null || cd /`, which silently fell back to
        # the filesystem root when /app was absent -- and the whole trial scored 0.0 with no error,
        # because a missing directory was treated as a normal case. A fallback that cannot work is
        # worse than a failure: it spends the model budget and returns a plausible-looking zero.
        # The directory is therefore resolved and *checked*, and an unusable one raises here.
        working_dir = "/app"
        check = await self.exec_as_agent(
            environment, command=f"test -d {working_dir} && echo present || echo missing", timeout_sec=60
        )
        if "present" not in str(getattr(check, "stdout", "")):
            raise RuntimeError(
                f"{working_dir} does not exist in the task container, so there is nowhere to do the "
                f"task. The environment was not set up as this adapter expects; running anyway would "
                f"score zero for a reason that has nothing to do with the agent."
            )

        # Make the working directory writable by whatever user the turn runs as.
        #
        # This was the cause of three consecutive 0.0 scores, and it is worth stating exactly.
        # `exec_as_agent` runs as the environment's agent user -- measured in this image, `ubuntu` --
        # while `/app` is `root:root` mode 755. So the agent could read the task and could not write
        # its answer: `touch /app/x` returns Permission denied. Nothing raised, because Myriaxon
        # handled the refusal gracefully and printed an explanation, and the task's verifier then
        # reported the missing file. Every hand-run that succeeded had used `docker exec` as root,
        # which is why the difference never showed up outside Harbor.
        #
        # Ownership rather than mode 777: the directory stays closed to other users, and the change is
        # scoped to the one directory the task grades. The agent user is discovered by asking, not
        # assumed -- a wrong guess here fails the same silent way.
        whoami = await self.exec_as_agent(environment, command="id -un", timeout_sec=60)
        agent_user = str(getattr(whoami, "stdout", "") or "").strip().splitlines()[-1:] or [""]
        agent_user = agent_user[0].strip() if agent_user else ""
        if agent_user and agent_user != "root":
            await self.exec_as_root(
                environment,
                command=(
                    f"chown -R {shlex.quote(agent_user)} {working_dir} 2>/dev/null || "
                    f"chmod -R a+rwX {working_dir}"
                ),
                timeout_sec=300,
            )

        # The config has to be readable by that same user: it is written as root with mode 600 so the
        # key stays private, and the turn would otherwise fail to read its own credentials.
        if agent_user and agent_user != "root":
            await self.exec_as_root(
                environment,
                command=f"chown -R {shlex.quote(agent_user)} {CONFIG_DIR}",
                timeout_sec=120,
            )

        # The whole prompt is quoted, so an instruction containing quotes, newlines or shell
        # metacharacters cannot break out of the command. Terminal-Bench instructions routinely
        # contain backticks and paths.
        #
        # `-p` receives the instruction through a FILE rather than an argument. Measured on regex-log:
        # its instruction contains a code fence, an apostrophe and a newline, and the resulting
        # shell-quoting needed `'"'"'` escaping -- which worked, but a prompt that can break the command
        # line is one bad task away from a silent failure.
        staging = Path(tempfile.mkdtemp(prefix="myriaxon-prompt-"))
        try:
            prompt_file = staging / "prompt.txt"
            prompt_file.write_text(instruction, encoding="utf-8", newline="\n")
            await environment.upload_file(
                source_path=str(prompt_file), target_path="/tmp/myriaxon-prompt.txt"
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

        turn = await self.exec_as_agent(
            environment,
            command=(
                f"cd {working_dir} && "
                f"MYRIAXON_CONFIG_DIR={CONFIG_DIR} "
                f"/opt/myriaxon-venv/bin/python -m myriaxon "
                f"--mode auto "
                f'-p "$(cat /tmp/myriaxon-prompt.txt)"'
            ),
            timeout_sec=1800,
        )
        # The turn's exit code is reported, not raised on. An agent can fail noisily and still have
        # left the right artifact on disk, and the task's verifier -- not this adapter -- decides
        # whether the work is correct. Raising here would turn a partial success into an error.
        if getattr(turn, "return_code", 0) != 0:
            logger.warning(
                "Myriaxon exited %s; the verifier still decides the outcome. stderr: %s",
                getattr(turn, "return_code", "?"),
                (getattr(turn, "stderr", "") or "")[:400],
            )

    def _get_env(self, name: str) -> str:
        return os.environ.get(name, "")
