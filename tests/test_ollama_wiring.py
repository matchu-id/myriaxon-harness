"""The Ollama mode, end to end, against a fake Ollama (`tools/fake_ollama.py`): no Ollama needed, the real one untouched.

What these pin down is what the mode promises a user who has Ollama models on their machine:

* the window Myriaxon budgets against is the one Ollama will REALLY run the model with -- 4,096 for a model nobody
  configured, the model's full 262,144 for the `myriaxon` model that `myriaxon ollama --create` builds (the old code capped
  everything at 32,768 and was wrong in both directions);
* `myriaxon ollama --create` builds that model (so `ollama run myriaxon` works), refuses to overwrite or delete a model it
  did not make, tells the truth about memory, and does not load a model that does not fit;
* `myriaxon --ollama`, and a machine with no key at all, find the model by themselves and run a real turn with it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

from fake_ollama import FakeOllama, nemotron_like, ornith_like, prose_only  # noqa: E402

from myriaxon import ollama  # noqa: E402

GIB = 1024 ** 3
KEY_VARIABLES = ("MYRIAXON_API_KEY", "MYRIAXON_PROVIDER", "MYRIAXON_MODEL", "MYRIAXON_BASE_URL", "GEMINI_API_KEY",
                 "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY", "NETRA_API_KEY", "MYRIAXON_OLLAMA_MAX_CONTEXT",
                 "MYRIAXON_OLLAMA_AUTOCREATE", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_CONTEXT_LENGTH")


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    """No key, provider or Ollama setting from the developer's machine or from conftest leaks in."""
    for name in KEY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    from myriaxon.config import CONFIG_FILE

    CONFIG_FILE.unlink(missing_ok=True)
    yield
    CONFIG_FILE.unlink(missing_ok=True)


@pytest.fixture
def roomy(monkeypatch):
    """A machine with plenty of memory: 32 GB RAM (24 free), a 24 GB GPU (all free)."""
    monkeypatch.setattr(ollama, "system_memory", lambda: (24 * GIB, 32 * GIB))
    monkeypatch.setattr(ollama, "gpu_memory", lambda: (24 * GIB, 24 * GIB))


@pytest.fixture
def cramped(monkeypatch):
    """The developer machine this was built on: 15 GB RAM (1.5 free), an 8 GB GPU (7.7 free)."""
    monkeypatch.setattr(ollama, "system_memory", lambda: (int(1.5 * GIB), 15 * GIB))
    monkeypatch.setattr(ollama, "gpu_memory", lambda: (int(7.7 * GIB), 8 * GIB))


@pytest.fixture
def fake(monkeypatch):
    with FakeOllama() as server:
        monkeypatch.setattr(ollama, "candidate_hosts", lambda: [server.root])
        yield server


def run(args, capsys):
    """Run `myriaxon <args>` in-process; returns (exit code, output with whitespace collapsed)."""
    from myriaxon.cli import main

    code = main(list(args))
    captured = capsys.readouterr()
    return code, " ".join((captured.out + captured.err).split())


# ----------------------------------------------------------------------------------------------- the window Ollama really gives
def test_a_model_nobody_configured_runs_with_4096_whatever_it_declares(fake):
    probe = ollama.detect(with_details=True)
    model = probe.model("ornith-1.5:9b")
    assert model.context_length == 262_144           # what the architecture supports
    assert model.num_ctx == 0                        # what its own settings ask for: nothing
    assert ollama.runtime_context(model) == 4_096    # so this is what Ollama runs it with
    assert ollama.profile_hint(ollama.runtime_context(model)) == "small"


def test_the_window_comes_from_what_is_loaded_when_the_model_is(fake):
    fake.loaded["ornith-1.5:9b"] = {"context_length": 32_768, "size": 1, "size_vram": 1}
    probe = ollama.detect(with_details=True)
    assert probe.loaded == {"ornith-1.5:9b": 32_768}
    assert ollama.runtime_context(probe.model("ornith-1.5:9b"), probe.loaded["ornith-1.5:9b"]) == 32_768


def test_a_256k_window_is_not_capped(fake):
    """The reported defect: the harness treated a model that supports 256K as a 32K one."""
    result = ollama.create_model(fake.root, "myriaxon", "ornith-1.5:9b", 262_144)
    assert result.ok, result.message
    model = ollama.describe_model(fake.root, "myriaxon")
    assert model.num_ctx == 262_144
    assert ollama.runtime_context(model) == 262_144
    assert ollama.practical_context(262_144) == 262_144
    assert ollama.profile_hint(262_144) == "large"


def test_the_optional_ceiling_still_works(fake, monkeypatch):
    ollama.create_model(fake.root, "myriaxon", "ornith-1.5:9b", 262_144)
    monkeypatch.setenv(ollama.OLLAMA_CONTEXT_ENV, "65536")
    assert ollama.runtime_context(ollama.describe_model(fake.root, "myriaxon")) == 65_536
    monkeypatch.setenv(ollama.OLLAMA_CONTEXT_ENV, "not-a-number")
    assert ollama.runtime_context(ollama.describe_model(fake.root, "myriaxon")) == 262_144


def test_the_server_wide_default_window_is_honoured(fake, monkeypatch):
    monkeypatch.setenv(ollama.SERVER_CONTEXT_ENV, "16384")
    assert ollama.runtime_context(ollama.describe_model(fake.root, "ornith-1.5:9b")) == 16_384


def test_a_window_never_exceeds_what_the_model_supports():
    model = ollama.OllamaModel(name="m", context_length=8_192, num_ctx=65_536)
    assert ollama.runtime_context(model) == 8_192


# ------------------------------------------------------------------------------------------------------------- memory arithmetic
def test_kv_cache_cost_per_token_follows_the_architecture(fake):
    ornith = ollama.describe_model(fake.root, "ornith-1.5:9b")
    nemotron = ollama.describe_model(fake.root, "nemotron-3-nano:4b")
    assert ornith.kv_bytes_per_token == 32_768        # 8 attention layers (1 in 4 of 32) x 4 KV heads x (256+256) x 2 bytes
    assert nemotron.kv_bytes_per_token == 16_384      # KV heads listed per layer: four layers keep 8 each
    assert ollama.kv_bytes_per_token({}) == 0         # unknown stays unknown, never a guess


def test_a_plain_transformer_keeps_a_cache_on_every_layer():
    info = {"llama.block_count": 32, "llama.attention.head_count": 32, "llama.attention.head_count_kv": 8,
            "llama.embedding_length": 4096}
    assert ollama.kv_bytes_per_token(info) == 32 * 8 * (128 + 128) * 2


def test_the_memory_estimate_gives_three_honest_verdicts(fake, roomy):
    model = ollama.describe_model(fake.root, "ornith-1.5:9b")
    model.size_bytes = 6_234_423_947
    big = ollama.estimate_fit(model, 262_144)
    assert big.kv == 262_144 * 32_768 == 8 * GIB
    assert big.verdict == "fits"                                   # 24 GB GPU + 24 GB RAM free
    tight = ollama.estimate_fit(model, 262_144, memory=(int(1.5 * GIB), 15 * GIB), gpu=(int(7.7 * GIB), 8 * GIB))
    assert tight.verdict == "tight"                                # ~15 GB needed: fits the machine, not what is free now
    too_big = ollama.estimate_fit(model, 262_144, memory=(2 * GIB, 8 * GIB), gpu=(0, 0))
    assert too_big.verdict == "too-big"
    some_room = dict(memory=(4 * GIB, 15 * GIB), gpu=(int(7.7 * GIB), 8 * GIB))
    assert ollama.estimate_fit(model, 262_144, kv_type="f16", **some_room).verdict == "tight"
    quantised = ollama.estimate_fit(model, 262_144, kv_type="q8_0", **some_room)
    assert quantised.kv < big.kv // 1.5 and quantised.verdict == "fits"        # an 8-bit cache roughly halves the KV cost
    assert ollama.estimate_fit(ollama.OllamaModel(name="x"), 8192).verdict == "unknown"


def test_the_advice_lines_say_what_to_do(fake, cramped):
    model = ollama.describe_model(fake.root, "ornith-1.5:9b")
    model.size_bytes = 6_234_423_947
    text = " ".join(ollama.estimate_fit(model, 262_144).lines("ornith-1.5:9b"))
    assert "262,144-token window" in text and "KV cache" in text
    assert "OLLAMA_KV_CACHE_TYPE=q8_0" in text and "OLLAMA_FLASH_ATTENTION=1" in text
    assert "only if other programs are closed" in text


def test_the_full_window_is_halved_only_while_the_machine_cannot_hold_it(fake):
    model = ollama.describe_model(fake.root, "ornith-1.5:9b")
    model.size_bytes = 6_234_423_947
    window, fit = ollama.default_window(model, memory=(24 * GIB, 32 * GIB), gpu=(24 * GIB, 24 * GIB))
    assert window == 262_144 and fit.verdict == "fits"
    window, fit = ollama.default_window(model, memory=(int(1.5 * GIB), 15 * GIB), gpu=(int(7.7 * GIB), 8 * GIB))
    assert window == 262_144 and fit.verdict == "tight"            # tight is kept: it is what was asked for, with a warning
    # 16 GB and no GPU: the weights fit, the full cache does not, so the window halves until it does.
    window, fit = ollama.default_window(model, memory=(2 * GIB, 16 * GIB), gpu=(0, 0))
    assert window == 65_536 and fit.verdict == "tight"
    # A machine that cannot hold even the weights: nothing a smaller window does helps, so it stops at the floor.
    window, fit = ollama.default_window(model, memory=(2 * GIB, 8 * GIB), gpu=(0, 0))
    assert window == ollama.MIN_AGENT_CONTEXT and fit.verdict == "too-big"


@pytest.mark.parametrize("text, declared, expected", [
    ("max", 262_144, 262_144), ("", 262_144, 262_144), ("MAX", 40_960, 40_960), ("max", 0, ollama.FALLBACK_WINDOW),
    ("65536", 262_144, 65_536), ("64k", 262_144, 65_536), ("256K", 0, 262_144), ("1m", 0, 1_048_576),
    ("65,536", 0, 65_536), ("banana", 262_144, 0), ("-5", 262_144, 0), ("12k8", 0, 0),
])
def test_ctx_is_read_the_way_people_write_it(text, declared, expected):
    assert ollama.parse_window(text, declared) == expected


# ------------------------------------------------------------------------------------------------------------ choosing a base
def test_the_base_is_the_loaded_model_then_the_largest_that_fits(fake, roomy):
    probe = ollama.detect(with_details=True)
    assert ollama.choose_base(probe).name == "ornith-1.5:9b"                 # 9B beats 4B when both fit
    assert ollama.choose_base(probe, "nemotron-3-nano:4b").name == "nemotron-3-nano:4b"   # asked for by name
    assert ollama.choose_base(probe, "no-such-model") is None
    probe.loaded = {"nemotron-3-nano:4b": 4096}
    assert ollama.choose_base(probe).name == "nemotron-3-nano:4b"            # the one already loaded is the one in use


def test_a_model_too_big_for_the_machine_is_not_chosen(monkeypatch):
    huge = ornith_like("huge:397b")
    huge["size"] = 242 * GIB
    huge["details"]["parameter_size"] = "397B"
    with FakeOllama([huge, nemotron_like()]) as server:
        monkeypatch.setattr(ollama, "candidate_hosts", lambda: [server.root])
        probe = ollama.detect(with_details=True)
        picked = ollama.choose_base(probe, memory=(8 * GIB, 16 * GIB), gpu=(8 * GIB, 8 * GIB))
    assert picked.name == "nemotron-3-nano:4b"


def test_a_model_that_cannot_call_tools_is_never_a_base(monkeypatch):
    with FakeOllama([prose_only()]) as server:
        monkeypatch.setattr(ollama, "candidate_hosts", lambda: [server.root])
        assert ollama.choose_base(ollama.detect(with_details=True)) is None


# ------------------------------------------------------------------------------------------------------------------ create / remove
def test_create_sends_the_window_and_the_persona_and_keeps_both(fake):
    seen = []
    result = ollama.create_model(fake.root, "myriaxon", "ornith-1.5:9b", 262_144, progress=seen.append)
    assert result.ok and result.via == "api"
    sent = fake.calls("POST", "/api/create")[0]
    assert sent["from"] == "ornith-1.5:9b" and sent["parameters"] == {"num_ctx": 262_144}
    assert sent["system"].startswith("You are Myriaxon")
    assert seen[-1] == "success"
    made = ollama.describe_model(fake.root, "myriaxon")
    assert made.num_ctx == 262_144 and ollama.made_by_us(made)
    assert not ollama.made_by_us(ollama.describe_model(fake.root, "ornith-1.5:9b"))
    # Nothing about the base model changed.
    assert fake.find("ornith-1.5:9b")["parameters"] == ""


def test_create_reports_ollamas_own_error(fake):
    result = ollama.create_model(fake.root, "myriaxon", "does-not-exist", 32_768)
    assert not result.ok and "not found" in result.message


def test_create_does_not_pretend_when_ollama_drops_the_window(monkeypatch):
    with FakeOllama(keep_window=False) as server:
        monkeypatch.setattr(ollama, "candidate_hosts", lambda: [server.root])
        monkeypatch.setattr(ollama.shutil, "which", lambda name: None)      # and there is no `ollama` command to fall back on
        result = ollama.create_model(server.root, "myriaxon", "ornith-1.5:9b", 262_144)
    assert not result.ok
    assert "did not keep the window" in result.message and "not on PATH" in result.message


def test_create_falls_back_to_the_ollama_command_when_the_api_cannot(monkeypatch):
    with FakeOllama(keep_window=False) as server:
        def fake_cli(base_url, name, base, window, system, timeout):
            entry = json.loads(json.dumps(server.find(base)))
            entry["name"] = name + ":latest"
            entry["parameters"] = f"num_ctx {window}"
            entry["system"] = system
            server.models[entry["name"]] = entry
            return ollama.CreateResult(True, name, base, window, "cli", "created")

        monkeypatch.setattr(ollama, "_cli_create", fake_cli)
        result = ollama.create_model(server.root, "myriaxon", "ornith-1.5:9b", 262_144)
        assert result.ok and result.via == "cli"
        assert ollama.describe_model(server.root, "myriaxon").num_ctx == 262_144


def test_remove_deletes_only_the_named_model(fake):
    ollama.create_model(fake.root, "myriaxon", "ornith-1.5:9b", 65_536)
    ok, message = ollama.remove_model(fake.root, "myriaxon")
    assert ok and fake.find("myriaxon") is None and fake.find("ornith-1.5:9b") is not None
    assert ollama.remove_model(fake.root, "myriaxon")[0] is False


def test_loading_a_model_proves_the_window_it_really_runs_with(fake):
    ollama.create_model(fake.root, "myriaxon", "ornith-1.5:9b", 65_536)
    ok, message, entry = ollama.probe_load(fake.root, "myriaxon")
    assert ok and entry["context_length"] == 65_536
    assert ollama.detect(with_details=True).loaded == {"myriaxon:latest": 65_536}


def test_a_load_that_ollama_refuses_says_why(monkeypatch):
    with FakeOllama(memory_error="model requires more system memory (15.1 GiB) than is available (9.4 GiB)") as server:
        ok, message, entry = ollama.probe_load(server.root, "ornith-1.5:9b")
    assert not ok and "more system memory" in message and entry == {}


# ------------------------------------------------------------------------------------------------------------------- the command
def test_create_command_builds_the_model_and_points_myriaxon_at_it(fake, roomy, capsys):
    from myriaxon.config import load_config

    code, out = run(["ollama", "--create"], capsys)
    assert code == 0, out
    assert fake.window_of("myriaxon") == 262_144                       # the model's FULL window, not 4,096 and not 32K
    assert fake.find("myriaxon")["system"].startswith("You are Myriaxon")
    assert "Creating 'myriaxon' from ornith-1.5:9b with a 262,144-token window" in out
    assert "ollama run myriaxon" in out and "myriaxon ollama --remove" in out
    assert "It runs." in out and "262,144-token window" in out          # loaded once to prove it
    saved = load_config()
    assert (saved.provider, saved.model, saved.base_url, saved.api_key) == ("ollama", "myriaxon", fake.base_url, "ollama")


def test_create_command_options(fake, roomy, capsys):
    from myriaxon.config import load_config

    code, out = run(["ollama", "--create", "--from", "nemotron-3-nano:4b", "--ctx", "64k", "--name", "side",
                     "--no-use", "--no-check"], capsys)
    assert code == 0, out
    assert fake.window_of("side") == 65_536 and fake.find("side")["name"] == "side:latest"
    assert load_config() is None                                      # --no-use left the saved configuration alone
    assert not fake.calls("POST", "/api/generate")                    # --no-check did not load it
    assert "(plain chat; no tools)" in out and "--name side" in out


@pytest.mark.parametrize("ctx, message", [
    ("banana", "--ctx must be"), ("2k", "no room to work"), ("999999999", "supports 262,144 tokens at most"),
])
def test_create_command_refuses_a_window_that_cannot_work(fake, roomy, capsys, ctx, message):
    code, out = run(["ollama", "--create", "--ctx", ctx], capsys)
    assert code == 2 and message in out
    assert not fake.calls("POST", "/api/create")


def test_create_command_can_print_the_modelfile_instead(fake, roomy, capsys):
    code, out = run(["ollama", "--create", "--modelfile", "--ctx", "128k"], capsys)
    assert code == 0
    assert "FROM ornith-1.5:9b" in out and "PARAMETER num_ctx 131072" in out and 'SYSTEM """You are Myriaxon' in out
    assert not fake.calls("POST", "/api/create")


def test_create_command_never_overwrites_a_model_it_did_not_make(monkeypatch, roomy, capsys):
    mine = ornith_like("myriaxon:latest")
    mine["system"] = "my own persona"
    with FakeOllama([ornith_like(), mine]) as server:
        monkeypatch.setattr(ollama, "candidate_hosts", lambda: [server.root])
        code, out = run(["ollama", "--create"], capsys)
        assert code == 1 and "did not make" in out
        assert server.find("myriaxon")["system"] == "my own persona"
        assert not server.calls("POST", "/api/create")


def test_create_command_does_not_load_a_model_the_machine_cannot_hold_now(fake, cramped, capsys):
    code, out = run(["ollama", "--create"], capsys)
    assert code == 0, out
    assert fake.window_of("myriaxon") == 262_144                       # still the full window: that is what was asked for
    assert not fake.calls("POST", "/api/generate")                     # but it was not loaded to find out
    assert "Not loaded to check" in out and "OLLAMA_KV_CACHE_TYPE=q8_0" in out


def test_create_command_reports_a_model_that_would_not_load(monkeypatch, roomy, capsys):
    with FakeOllama(memory_error="model requires more system memory (15.1 GiB) than is available (9.4 GiB)") as server:
        monkeypatch.setattr(ollama, "candidate_hosts", lambda: [server.root])
        code, out = run(["ollama", "--create"], capsys)
        assert code == 1
        assert "would not load" in out and "more system memory" in out and "--ctx 131072" in out
        assert server.find("myriaxon") is not None                      # created all the same; only the load failed


def test_create_command_with_nothing_answering_says_so(monkeypatch, capsys):
    monkeypatch.setattr(ollama, "candidate_hosts", lambda: ["http://127.0.0.1:9"])
    code, out = run(["ollama", "--create"], capsys)
    assert code == 1 and "Ollama" in out


def test_remove_command_deletes_ours_and_refuses_the_rest(fake, roomy, capsys):
    run(["ollama", "--create", "--no-check"], capsys)
    code, out = run(["ollama", "--remove"], capsys)
    assert code == 0 and "Removed 'myriaxon'" in out and fake.find("myriaxon") is None
    assert fake.find("ornith-1.5:9b") is not None
    assert "Myriaxon was set to use it" in out
    code, out = run(["ollama", "--remove"], capsys)
    assert code == 0 and "nothing to remove" in out
    code, out = run(["ollama", "--remove", "--name", "ornith-1.5:9b"], capsys)
    assert code == 1 and "was not made by" in out and fake.find("ornith-1.5:9b") is not None


def test_use_command_saves_the_model_and_warns_when_its_window_is_too_small(fake, capsys):
    from myriaxon.config import load_config

    code, out = run(["ollama", "--use", "nemotron-3-nano:4b"], capsys)
    assert code == 0
    assert "context 4,096 tokens (the model supports 262,144)" in out and "too small for the agent" in out
    assert load_config().model == "nemotron-3-nano:4b"
    code, out = run(["ollama", "--use", "typo-model"], capsys)
    assert code == 1 and "no local model named 'typo-model'" in out


def test_use_command_replaces_another_providers_key_with_the_placeholder(fake, capsys):
    from myriaxon.config import CONFIG_FILE

    CONFIG_FILE.write_text(json.dumps({"provider": "deepseek", "api_key": "sk-real-secret", "base_url": "https://x/",
                                       "model": "m", "fallback_model": "other"}), encoding="utf-8")
    run(["ollama", "--use", "ornith-1.5:9b"], capsys)
    saved = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    assert saved["api_key"] == "ollama" and saved["provider"] == "ollama" and saved["fallback_model"] == ""
    assert "sk-real-secret" not in CONFIG_FILE.read_text(encoding="utf-8")


def test_json_report_carries_the_real_window_and_whether_it_is_loaded(fake, roomy, capsys):
    code, out = run(["ollama", "--json"], capsys)
    models = {m["name"]: m for m in json.loads(out)["models"]}
    assert models["ornith-1.5:9b"]["usable_context"] == 4_096 and models["ornith-1.5:9b"]["context_length"] == 262_144
    assert models["ornith-1.5:9b"]["kv_bytes_per_token"] == 32_768 and models["ornith-1.5:9b"]["supports_tools"]
    run(["ollama", "--create"], capsys)
    code, out = run(["ollama", "--json"], capsys)
    models = {m["name"]: m for m in json.loads(out)["models"]}
    assert models["myriaxon:latest"]["usable_context"] == 262_144 and models["myriaxon:latest"]["loaded"]


def test_the_report_names_the_fix_for_a_window_that_is_too_small(fake, capsys):
    code, out = run(["ollama"], capsys)
    assert code == 0
    assert "runs with" in out and "4,096" in out and "myriaxon ollama --create" in out


# -------------------------------------------------------------------------------------------------------------------- wiring in
def test_wire_builds_the_myriaxon_model_once_and_reuses_it(fake, roomy):
    first, why = ollama.wire()
    assert first is not None, why
    assert first.model == "myriaxon" and first.window == 262_144 and first.base_url == fake.base_url
    assert any("Created the Ollama model 'myriaxon'" in note for note in first.notes)
    second, _ = ollama.wire()
    assert second.model == "myriaxon" and second.notes == []
    assert len(fake.calls("POST", "/api/create")) == 1


def test_wire_can_be_told_not_to_create(fake, roomy, monkeypatch):
    monkeypatch.setenv(ollama.AUTOCREATE_ENV, "0")
    wiring, _ = ollama.wire()
    assert wiring.model == "ornith-1.5:9b" and wiring.window == 4_096
    assert not fake.calls("POST", "/api/create")


def test_wire_uses_a_named_model_as_it_is_and_explains_a_wrong_name(fake, roomy):
    wiring, _ = ollama.wire(model="nemotron-3-nano:4b")
    assert wiring.model == "nemotron-3-nano:4b" and not fake.calls("POST", "/api/create")
    wiring, why = ollama.wire(model="nope")
    assert wiring is None and "no model called 'nope'" in why and "ornith-1.5:9b" in why


def test_wire_explains_why_nothing_can_be_used(monkeypatch):
    monkeypatch.setattr(ollama, "candidate_hosts", lambda: ["http://127.0.0.1:9"])
    wiring, why = ollama.wire()
    assert wiring is None and "Ollama" in why
    with FakeOllama([prose_only()]) as server:
        monkeypatch.setattr(ollama, "candidate_hosts", lambda: [server.root])
        wiring, why = ollama.wire()
    assert wiring is None and "none of them can call tools" in why


def test_wire_shrinks_the_window_only_when_the_machine_cannot_hold_it(fake, monkeypatch):
    monkeypatch.setattr(ollama, "system_memory", lambda: (2 * GIB, 8 * GIB))
    monkeypatch.setattr(ollama, "gpu_memory", lambda: (0, 0))
    wiring, _ = ollama.wire()
    assert 8_192 <= wiring.window < 262_144 and fake.window_of("myriaxon") == wiring.window


# ------------------------------------------------------------------------------------------------------ what the agent is told
def test_the_agent_is_told_the_real_window_and_size(fake):
    ollama.create_model(fake.root, "myriaxon", "ornith-1.5:9b", 262_144)
    assert ollama.agent_facts(fake.base_url, "myriaxon") == (262_144, "9b")
    assert ollama.agent_facts(fake.base_url, "ornith-1.5:9b") == (4_096, "9b")
    assert ollama.agent_facts(fake.base_url, "no-such-model") == (0, "")
    assert ollama.limits_for(fake.base_url, "myriaxon") == 262_144
    assert ollama.agent_facts("http://127.0.0.1:9/v1", "myriaxon") == (0, "")      # nothing answering: unknown, not a crash


def test_startup_says_the_three_things_that_go_wrong(fake, cramped):
    root = fake.root
    assert any("too small for the agent" in note for note in ollama.startup_notes(root + "/v1", "ornith-1.5:9b"))
    ollama.create_model(fake.root, "myriaxon", "ornith-1.5:9b", 262_144)
    notes = ollama.startup_notes(root + "/v1", "myriaxon")
    assert len(notes) == 1 and "262,144-token window needs about" in notes[0] and "--ctx 131072" in notes[0]
    assert "no model called ghost" in ollama.startup_notes(root + "/v1", "ghost")[0]
    assert "not answering" in ollama.startup_notes("http://127.0.0.1:9/v1", "myriaxon")[0]


def test_startup_is_silent_when_there_is_nothing_to_say(fake, roomy):
    ollama.create_model(fake.root, "myriaxon", "ornith-1.5:9b", 262_144)
    assert ollama.startup_notes(fake.base_url, "myriaxon") == []


def test_errors_from_a_local_model_come_with_the_fix():
    assert "Is Ollama running?" in ollama.hint_for_error("Connection refused", connection=True)
    assert "--ctx 65536" in ollama.hint_for_error("model requires more system memory (15.1 GiB) than is available (9.4 GiB)")
    assert "myriaxon ollama" in ollama.hint_for_error("model 'x' not found")
    assert ollama.hint_for_error("some other failure") == ""


# ------------------------------------------------------------------------------------------------------ resolving the configuration
def test_a_machine_with_no_key_and_an_ollama_just_works(fake, roomy, capsys):
    from myriaxon.config import resolve_config
    from myriaxon.theme import get_console

    config = resolve_config(get_console(quiet=True))
    assert (config.provider, config.model, config.base_url, config.api_key) == ("ollama", "myriaxon", fake.base_url, "ollama")
    assert fake.window_of("myriaxon") == 262_144


def test_a_machine_with_a_key_never_touches_ollama(fake, monkeypatch):
    from myriaxon.config import resolve_config
    from myriaxon.theme import get_console

    monkeypatch.setenv("MYRIAXON_API_KEY", "sk-test")
    monkeypatch.setenv("MYRIAXON_PROVIDER", "deepseek")
    config = resolve_config(get_console(quiet=True))
    assert config.provider == "deepseek"
    assert fake.requests == []


def test_the_ollama_flag_uses_ollama_even_when_a_cloud_provider_is_saved(fake, roomy):
    from myriaxon.config import CONFIG_FILE, resolve_config
    from myriaxon.theme import get_console

    CONFIG_FILE.write_text(json.dumps({"provider": "deepseek", "api_key": "sk-x", "base_url": "https://x/", "model": "m"}),
                           encoding="utf-8")
    config = resolve_config(get_console(quiet=True), ollama=True)
    assert config.provider == "ollama" and config.model == "myriaxon"
    assert json.loads(CONFIG_FILE.read_text(encoding="utf-8"))["provider"] == "deepseek"      # a one-run choice: nothing saved


def test_the_ollama_flag_with_a_model_uses_that_model(fake, roomy):
    from myriaxon.config import resolve_config
    from myriaxon.theme import get_console

    config = resolve_config(get_console(quiet=True), ollama=True, model="nemotron-3-nano:4b")
    assert config.model == "nemotron-3-nano:4b"
    assert not fake.calls("POST", "/api/create")


def test_the_ollama_flag_without_ollama_explains_and_exits(monkeypatch):
    """The message reaches the user, asserted against the CONSOLE'S OWN BUFFER.

    This test used to read `capsys.readouterr().out`, and it failed intermittently in a full-suite run while
    passing alone -- the long-standing flake recorded in `conftest.py`. The cause is now known and is a defect
    in the TEST rather than in the product:

        E  assert ('Ollama' in '')
        Captured stdout call
          Ollama is installed but not answering at http://127.0.0.1:9. Start it with: ...

    The message WAS printed -- pytest captured it at the file-descriptor level and showed it in the failure
    report -- but `capsys` had an empty string for it. `capsys` reads the object pytest installed as
    `sys.stdout` for THIS test, and a Rich `Console` resolves its stream through state that pytest rewrites
    between tests, so which of the two captures the output depends on run ordering. The assertion was testing
    pytest's capture plumbing rather than the product.

    Binding the Console to a buffer this test owns removes the dependency entirely: there is no ordering under
    which `StringIO` fails to receive a write. The same reasoning as the rest of the suite's tests, which
    assert on returned values rather than on captured console output.
    """
    from io import StringIO

    from rich.console import Console

    from myriaxon.config import resolve_config
    from myriaxon.theme import MYRIAXON_THEME

    buffer = StringIO()
    console = Console(theme=MYRIAXON_THEME, file=buffer, width=100, no_color=True)

    monkeypatch.setattr(ollama, "candidate_hosts", lambda: ["http://127.0.0.1:9"])
    with pytest.raises(SystemExit) as stopped:
        resolve_config(console, ollama=True)
    assert stopped.value.code == 1

    out = " ".join(buffer.getvalue().split())
    assert "Ollama" in out, f"the reason was not explained; console said: {out[:200]!r}"
    assert "myriaxon --reauth" in out, f"the way out was not offered; console said: {out[:200]!r}"


def test_a_saved_ollama_choice_is_kept(fake, capsys):
    from myriaxon.config import resolve_config
    from myriaxon.theme import get_console

    run(["ollama", "--use", "nemotron-3-nano:4b"], capsys)
    config = resolve_config(get_console(quiet=True))
    assert (config.provider, config.model) == ("ollama", "nemotron-3-nano:4b")
    assert not fake.calls("POST", "/api/create")                       # the user's choice is not replaced behind their back


def test_the_setup_wizard_lists_ollama_as_a_provider():
    from myriaxon import config

    assert ("ollama", "Ollama (a model on this computer, no key)") in config._PROVIDER_LABELS


# --------------------------------------------------------------------------------------------------------------- a real agent turn
def test_myriaxon_dash_dash_ollama_runs_a_turn_against_the_model_it_built(fake, roomy, capsys):
    code, out = run(["--ollama", "-p", "say hello"], capsys)
    assert code == 0, out
    assert "Hello from the fake Ollama." in out
    asked = [r for r in fake.chat.requests if r.get("messages")]
    assert asked, "the chat endpoint was never called"
    assert asked[0]["model"] == "myriaxon"
    assert asked[0]["messages"][0]["role"] == "system"                 # Myriaxon's own prompt, which replaces the Modelfile persona
    assert fake.window_of("myriaxon") == 262_144


def test_a_turn_with_no_key_at_all_wires_itself(fake, roomy, capsys):
    code, out = run(["-p", "say hello"], capsys)
    assert code == 0, out
    assert "Hello from the fake Ollama." in out and "using the Ollama model on this computer" in out


def test_a_turn_on_a_model_that_is_gone_explains_what_to_do(monkeypatch, capsys):
    """Ollama stopped (or the model was deleted) after the configuration was saved: say so, do not show a bare connection error."""
    from myriaxon.config import CONFIG_FILE

    with FakeOllama() as server:
        CONFIG_FILE.write_text(json.dumps({"provider": "ollama", "api_key": "ollama", "base_url": server.base_url,
                                           "model": "ornith-1.5:9b"}), encoding="utf-8")
        host = server.root
    monkeypatch.setattr(ollama, "candidate_hosts", lambda: [host])
    code, out = run(["-p", "say hello"], capsys)
    assert code != 0
    assert "Is Ollama running?" in out
