# Connecting MCP servers

Myriaxon speaks every MCP transport, so it can reach any MCP server:

| Transport | Used by | How it connects |
|---|---|---|
| **stdio** | local servers launched as a command (`npx`, `uvx`, a script) | spawns the command and speaks JSON-RPC over its stdin/stdout |
| **streamable HTTP** | hosted servers (Figma, Microsoft 365, GitHub, most SaaS) | POSTs JSON-RPC to an HTTPS endpoint |
| **SSE** | older hosted servers | long-lived event stream |

A remote server is tried over streamable HTTP first, then SSE, because implementations differ. A
failure on the first transport is not treated as a failure of the server.

## Install the MCP SDK

MCP support needs the `mcp` package. Without it, a configured server contributes nothing and the MCP
panel says so instead of failing silently.

```powershell
pip install -e ".[mcp]"
```

## Add a server

**From the dashboard:** the **MCP servers** item in the sidebar menu. Choose the transport, then fill
in the command (stdio) or the URL and an access token (remote). **Reload** is not needed — a new
server's tools appear on the next message.

**From the terminal:** `/mcp install <name>` for a catalogued server, or `/mcp search <query>` to
find one. There is no `/mcp add` — this page said otherwise, and anyone following it got the usage
warning instead of a server. `/mcp list` shows what is configured and `/mcp remove <name>` drops one.

**By editing config directly:**

```json
{
  "stdio_servers": [
    { "name": "filesystem", "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "."] }
  ],
  "url_servers": [
    { "name": "figma", "url": "https://mcp.figma.com/mcp", "token": "figd_your_personal_access_token" },
    { "name": "drawio", "url": "https://mcp.draw.io/mcp" }
  ]
}
```

For a server needing a scheme other than `Bearer`, use `headers` instead of `token`:

```json
{ "name": "internal", "url": "https://mcp.example.com/mcp", "headers": { "X-Api-Key": "..." } }
```

## figma

Figma's Dev Mode MCP server is the usual way. There are two options:

1. **Local (recommended by Figma, needs the desktop app):** Figma runs a server on your machine at
   `http://127.0.0.1:3845/mcp`. It requires a Figma desktop app with Dev Mode MCP enabled, and a
   current paid seat.

   ```json
   { "name": "figma", "url": "http://127.0.0.1:3845/mcp" }
   ```

2. **Hosted:** add the URL with a personal access token as above. Create the token in Figma under
   **Settings → Security → Personal access tokens**.

Either way, once connected, Figma's tools appear to the model as `mcp__figma__<tool>`, e.g.
`mcp__figma__get_code`, `mcp__figma__get_image`. Ask for what you want in plain language — the model
chooses the tool.

## draw.io

draw.io ships an MCP server you can run locally:

```json
{ "name": "drawio", "command": "npx", "args": ["-y", "@drawio/mcp"] }
```

If you use a hosted draw.io MCP endpoint, add it as a `url_servers` entry instead.

## Microsoft 365 / Office

Microsoft's MCP servers are hosted and authenticate with **OAuth**, not a static token. Configure the
URL, then:

```json
{ "name": "m365", "url": "https://microsoft365.mcp.claude.com/mcp" }
```

Because the token is short-lived, the practical approach today is to complete the OAuth flow in your
browser, then paste the resulting bearer token into the **Access token** field. When it expires, paste
a new one. Re-saving the server **without** retyping the token keeps the existing one, so editing
other fields will not silently erase it.

## Useful servers to try

| Server | Command / URL |
|---|---|
| Filesystem | `npx -y @modelcontextprotocol/server-filesystem .` |
| Git | `uvx mcp-server-git --repository .` |
| GitHub | `npx -y @modelcontextprotocol/server-github` (needs `GITHUB_PERSONAL_ACCESS_TOKEN`) |
| Fetch (web) | `uvx mcp-server-fetch` |
| SQLite | `uvx mcp-server-sqlite --db-path ./data.db` |
| Memory | `npx -y @modelcontextprotocol/server-memory` |
| Figma | `http://127.0.0.1:3845/mcp` |
| draw.io | `npx -y @drawio/mcp` |
| Everything (test server) | `npx -y @modelcontextprotocol/server-everything` |

Browse more at `https://github.com/modelcontextprotocol/servers`. The dashboard's MCP panel can search
a registry for you.

## How server tools are named

A tool from server `figma` named `get_code` reaches the model as:

```
mcp__figma__get_code
```

Two consequences, both deliberate:

- **A server cannot shadow a built-in tool.** The fixture used in the test suite deliberately exposes
  a tool named `read_file`; it arrives as `mcp__<server>__read_file` and never as `read_file`, so it
  cannot hijack the real one. Verified in `scripts/check_mcp_remote_live.py` and `check_mcp_live.py`.
- Names are sanitised if a server uses characters a provider's function-name rules reject, with a
  short hash suffix so two long names cannot silently collide.

## Troubleshooting

| Symptom | Cause |
|---|---|
| The MCP panel warns "no remote MCP transport is installed" | the `mcp` package is missing — run `pip install -e ".[mcp]"` |
| A stdio server connects in your terminal but not from a sandboxed session | spawning a child process with piped stdio is blocked by the sandbox ("Access is denied" on pipe creation). Use a remote server, or run outside the sandbox. |
| A remote server returns no tools | check the token, and check whether it needs `headers` rather than `token`. The connection error names **both** transports' failures. |
| Tools appear but a call fails with a schema error | the server's schema and its handler disagree; the error comes from the server, not Myriaxon |

Per-server connection failure is reported and skipped — one broken entry never stops the agent.
