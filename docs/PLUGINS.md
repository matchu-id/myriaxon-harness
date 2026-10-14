# Writing a Myriaxon plugin

A plugin is a Python file that adds tools or observes hooks. Nothing is downloaded, installed, or
fetched — you put a file in a directory and it loads.

## Where to put it

| Directory | Applies to |
|---|---|
| `<config dir>/plugins/` | you, everywhere (the config dir is printed at startup and in the dashboard's Plugins panel) |
| `<project>/.myriaxon/plugins/` | that repository, so it can be committed and shared |

Both `*.py` files and package directories (a folder with `__init__.py`) are loaded. Files starting
with `_` are ignored.

## The smallest plugin

```python
PLUGIN = {"name": "acme", "version": "1.0", "description": "Demo plugin"}


def register(api):
    api.add_tool(
        "acme_echo",
        "Echo the text back.",
        {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
        lambda tool_input, ctx: tool_input.get("text", ""),
        mutating=False,
    )
```

Drop that in `plugins/acme.py`, press **Reload plugins** in the dashboard (or restart the CLI), and
the model can call `acme_echo`.

`PLUGIN` is optional but recommended: the name and version appear in the dashboard's Plugins panel.

## Adding a tool

```python
api.add_tool(name, description, input_schema, handler, mutating=False)
```

- `input_schema` is a JSON Schema object. It is validated before your handler runs, so a malformed
  call from the model is reported back to the model rather than reaching you.
- `handler(tool_input, ctx)` returns either a string, or `(text, is_error)`.
- The handler may raise. The exception is caught and returned to the model as a tool error — it will
  not crash the turn.

### `mutating=True` matters

A **mutating** tool is routed through the same confirmation and policy gate as `write_file` or
`run_bash`. That means:

- a `deny` rule in `settings.json` blocks it, and cannot be overridden by auto-approval
- an `ask` rule prompts for it
- in the terminal it shows the usual confirmation panel

A non-mutating tool runs immediately, like a read. Declare `mutating=True` for anything that writes,
executes, or reaches the network.

```python
def deploy(tool_input, ctx):
    ...  # actually does something irreversible
    return "deployed", False

api.add_tool("acme_deploy", "Deploy the service.", schema, deploy, mutating=True)
```

## Observing hooks

```python
api.on(event, handler)
```

| Event | Handler signature | Can change behaviour by returning |
|---|---|---|
| `pre_tool_use` | `(tool_name, tool_input, ctx)` | a non-empty **string blocks the call**, and that string is shown to the model |
| `post_tool_use` | `(tool_name, tool_input, result, is_error, ctx)` | — (observational) |
| `user_prompt` | `(prompt, ctx)` | a replacement prompt string |
| `agent_start` | `(ctx)` | — |
| `agent_stop` | `(ctx)` | — |

Hooks run **in-process**, unlike `hooks.json` shell hooks which spawn a subprocess per call. Use a
plugin hook when you want to inspect every tool call without paying that cost.

A hook that raises is contained: the failure is printed with the plugin's name and the turn
continues.

### Blocking a dangerous command

```python
def gate(tool_name, tool_input, ctx):
    if tool_name == "run_bash" and "prod" in str(tool_input.get("command", "")):
        return "Blocked: commands touching production require a human."
    return None


def register(api):
    api.on("pre_tool_use", gate)
```

## Rules the loader enforces

| Rule | Why |
|---|---|
| A plugin **cannot** register a tool whose name shadows a built-in | silently replacing `write_file` would make the tool list untrustworthy; the attempt is skipped and shown in the panel |
| A plugin that raises on import is **recorded and skipped** | a broken plugin must not stop the agent from starting |
| Unknown hook events and non-callable handlers are **refused** | a typo should fail loudly at registration, not silently at runtime |
| `register()` is called exactly once per plugin per load | re-registering would duplicate hooks |

## Checking your plugin loaded

Dashboard: the **Plugins** item in the sidebar menu. It lists every plugin with its tools and hooks,
and shows the error for any that failed, with a **Reload plugins** button.

Terminal: `/plugins`, or watch startup, which prints a warning naming any plugin that failed.
