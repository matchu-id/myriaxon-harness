# WhatsApp / messaging integration

Drive the harness from a chat app, and get a summary when a turn finishes.

Configure it in the dashboard with the **Messaging** item in the sidebar menu, or by editing
`whatsapp.json` in the config directory.

## Security first: the allowlist

**An empty allowlist means nobody may drive the agent.** Not everybody — nobody. An agent that runs
shell commands, reachable by anyone who finds the number, is a remote code execution service, so a
misconfiguration fails closed and the refusal names the allowlist.

```json
{ "allow": ["+15551234567"] }
```

Numbers are compared by digits only, so `whatsapp:+15551234567`, `15551234567` and
`+1 (555) 123-4567` all match the same person. Twilio, Meta and a human writing it by hand all
format it differently, and comparing the raw strings would refuse a caller who *is* on the list.

A **shared secret** is a second lock, checked against the `X-Myriaxon-Secret` header whenever one is set:

```json
{ "secret": "a-long-random-string" }
```

> **On the header name:** `X-Myriaxon-Secret` is the header to send. The code also reads the
> pre-rebrand `X-Ioh-Secret` — and a shipped `x-iow-secret` typo — so an already-deployed webhook
> keeps working, but the current name is checked first.

## Twilio (WhatsApp)

```json
{
  "provider": "twilio",
  "enabled": true,
  "allow": ["+15551234567"],
  "notify": "+15551234567",
  "account_sid": "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
  "auth_token": "your-auth-token",
  "from_number": "whatsapp:+14155238886"
}
```

Point the Twilio **messaging webhook** at:

```
https://<your-public-host>/api/inbound
```

For a quick local test, expose the port with a tunnel (`ngrok http 8765`) and use the URL it gives you.

## Meta WhatsApp Cloud API

```json
{
  "provider": "meta",
  "enabled": true,
  "allow": ["15551234567"],
  "notify": "15551234567",
  "phone_number_id": "1234567890",
  "access_token": "your-access-token"
}
```

Meta posts JSON nested as `entry[].changes[].value.messages[]`; that shape is understood, and a
**status callback** (delivered/read) arriving at the same URL is recognised and ignored rather than
answered as if it were a message.

## Generic bridge / self-hosted

Any bridge that can POST JSON works. The field names are configurable:

```json
{
  "provider": "generic",
  "enabled": true,
  "allow": ["+15551234567"],
  "send_url": "https://your-bridge.example.com/send",
  "send_method": "POST",
  "send_headers": { "X-Api-Key": "..." },
  "reply_field": "message.text",
  "sender_field": "message.sender"
}
```

## What it does

| | |
|---|---|
| **Inbound** | `POST /api/inbound` → parsed → allowlist checked → runs a turn in a new session → replies to the sender |
| **Outbound** | the reply is sent back; `notify` additionally receives a summary at turn end |
| **Always answers 200** | a refused message must not be retried forever by the provider. The JSON body says `ok: false` and why. |
| **Never blocks a turn** | notifications are sent on a worker thread, and a provider outage is recorded and counted rather than raised into the transcript |

## Credential handling

The API never returns a credential — only `has_secret` / `has_auth_token` / `has_access_token`. This
creates a two-way subtlety that is handled explicitly:

- **Blank means "keep".** The UI cannot show you the stored token, so if blank meant "new value", every
  unrelated edit would erase it.
- **Removing one needs its own signal.** Otherwise a rotated or leaked token could be replaced but
  never deleted. Use the **remove the stored shared secret** checkbox, or `clear_secrets` in the API:

```json
{ "clear_secrets": ["secret", "auth_token"] }
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `sender … is not on the allowlist` | add the number to `allow`, digits only is enough |
| `bad or missing shared secret` | a secret is stored; send it in the `X-Ioh-Secret` header, or clear the stored one |
| `messaging is disabled` | `enabled` is false |
| `no inbound message: looked for sender in … and text in …` | the payload shape is not what the provider expects — set `reply_field` / `sender_field` for a custom bridge |
| Reply never arrives | check `/api/messaging` stats (`sent` / `failed`) and the `send_error` in the webhook response. A bad `send_url`, token or account SID shows there. |

## Verified

`scripts/check_messaging.py` (42 checks) runs a real HTTP provider stand-in and asserts parsing for all
three shapes, allowlist enforcement, form and JSON encoding, basic and bearer auth, that a failing
provider is recorded rather than raised, and a **full round trip** — inbound webhook → a real agent turn
→ reply sent back to the provider.

`scripts/check_messaging_ui.py` covers the panel and the `/api/inbound` webhook over real HTTP,
including every refusal path and the clear-secret behaviour.
