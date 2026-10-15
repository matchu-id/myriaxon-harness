# Project Anker: The Most Advanced Agent Harness Ever Built

## Vision Document v0.1 — Architecture, Principles & Roadmap

**Author:** Anker (Myriaxon Locus)
**Date:** 2026-10-15
**Status:** Design Phase — Blueprint

---

## 1. Executive Summary

Project Anker is a next-generation agent harness designed from first principles to overcome the fundamental limitations of every major agent framework in existence today. Rather than layering orchestration patterns on top of chat-completion APIs, Anker treats *agency itself* as a first-class runtime abstraction — with native support for autonomous multi-agent orchestration, deep tool integration, recursive self-improvement sandboxing, persistent memory architectures, and self-measuring introspection.

The central thesis: **current agent frameworks are not frameworks for agents. They are frameworks for LLM calls dressed in agentic clothing.** LangGraph is a state machine with LLM nodes. CrewAI is a role-play prompt scheduler. AutoGen (now maintenance-mode) is a conversation loop with pluggable participants. None of them treat the agent as a persistent, self-aware, tool-wielding entity with a lifecycle, a memory hierarchy, and the ability to reflect on and modify its own behavior.

Anker changes that.

---

## 2. Landscape Analysis: What Exists Today

### 2.1 LangGraph (LangChain Inc.)

| Aspect | Assessment |
|---|---|
| **Core abstraction** | State machine (nodes + edges) over a typed state dict |
| **Strengths** | Durable execution, persistence, human-in-the-loop checkpoints, subgraph composition |
| **Limitations** | Agents are nodes in a graph, not entities with identity; no native tool sandboxing; memory is a persistence layer, not a cognitive architecture; no agent-to-agent communication protocol — just state passing; no self-reflection or self-modification capabilities |

### 2.2 CrewAI

| Aspect | Assessment |
|---|---|
| **Core abstraction** | Role-based agents with assigned tools, organized into Crews + Flows |
| **Strengths** | Simple mental model, role-playing works for many use cases, good ergonomics |
| **Limitations** | Agents are prompt-defined personas, not runtime entities; no durable state across runs (beyond AMP enterprise); no dynamic tool discovery; no recursive delegation; linear/sequential orchestration is the default; no memory hierarchy beyond conversation context; no real tool sandboxing |

### 2.3 AutoGen / Microsoft Agent Framework

| Aspect | Assessment |
|---|---|
| **Core abstraction** | Conversational agent with multi-agent chat protocols |
| **Strengths** | Strong multi-agent conversation patterns (e.g., group chat, nested chats), MCP support, rich tooling |
| **Limitations** | Agents are chat participants, not persistent entities; state management is conversation-scoped; no native self-improvement; maintenance mode for original AutoGen; MAF is enterprise-oriented but still chat-centric |

### 2.4 Other Notable Approaches

- **Semantic Kernel** (Microsoft): Function-calling orchestrator with planner — strong on tool composition, weak on multi-agent state and persistence.
- **Dify / Flowise**: Visual workflow builders — no agent self-awareness, no state persistence beyond flow variables.
- **OpenAI Assistants API**: Thread-centric, stateless from the assistant's perspective — no cross-conversation memory, no agent-internal architecture.
- **Griptape**: Structured with pipelines and workflows — better data flow model, but limited multi-agent and no self-evolution.
- **Cognosis / Microagents**: Novel but niche — no production-scale architecture.

### 2.5 Common Deficiencies Across All Frameworks

1. **No agent identity** — Agents are ephemeral; they have no persistent identity, no lifespan beyond a single run.
2. **No memory hierarchy** — Working memory, episodic memory, semantic memory, procedural memory are conflated into a single "context window" or a flat persistence layer.
3. **No tool sandboxing** — Tools run in-process or with no security boundary; no capability-based security for tool access.
4. **No self-reflection** — Agents cannot inspect their own execution trace, evaluate their decisions, and adjust future behavior.
5. **No recursive self-improvement** — No framework allows an agent to spawn a sandboxed instance of itself, try a modified strategy, evaluate the result, and adopt the improvement.
6. **No multi-agent protocol** — Agents communicate via LLM-generated text; there's no typed, structured agent-to-agent protocol with guarantees.
7. **No architectural introspection** — The harness cannot inspect its own runtime configuration, available tools, or internal state and report on them to itself or to other agents.
8. **No lifecycle management** — No born/live/sleep/die lifecycle for agents; no health checks, no restart policies.
9. **Observability is bolted-on** — Tracing is a post-hoc debug tool, not a first-class runtime primitive that agents can consume.
10. **Single LLM backend assumption** — Most frameworks assume a single model provider; multi-model routing is ad-hoc.

---

## 3. Core Principles of Anker

### Principle 1: Agent as First-Class Entity

An agent is not a chat participant or a node in a graph. An agent is a persistent runtime entity with:
- A **unique identity** (UUID, name, version)
- A **lifecycle** (spawn, init, run, pause, sleep, die)
- **Internal state** (working memory, episodic memory, procedural memory, semantic memory separated into distinct stores)
- A **tool belt** (capability-granted, sandboxed tool access)
- A **communication port** (typed agent-to-agent protocol)
- An **introspection layer** (self-metrics, execution trace, decision log)

### Principle 2: Memory Hierarchy (Not Just Context)

```
+-----------------------+
| Working Memory        |  Ephemeral, current-turn context (token-budgeted)
+-----------------------+
| Episodic Memory       |  Agent's past execution history, indexed by time and task
+-----------------------+
| Semantic Memory        |  Learned facts, concepts, persistent knowledge
+-----------------------+
| Procedural Memory     |  Learned skills, strategies, tool usage patterns
+-----------------------+
| Collective Memory     |  Shared across an agent fleet (swarm knowledge base)
+-----------------------+
```

Each layer has its own storage backend, retrieval strategy, and decay policy.

### Principle 3: Tool Sandboxing with Capability-Based Security

Every tool runs in a sandbox with:
- A **capability manifest** (what resources it needs: filesystem paths, network endpoints, environment variables, execution permissions)
- **Resource limits** (CPU time, memory, network bandwidth, file writes)
- An **audit trail** (every tool call is logged with arguments, return value hash, duration, exit status)
- **Revocable grants** (tools can be denied mid-execution by policy)
- **Sandbox technology**: Docker containers, Firecracker microVMs, or WASM-based sandboxes depending on the tool's resource profile

### Principle 4: Typed Agent-to-Agent Protocol (A2A-T)

Current "multi-agent" systems pass text strings between LLM calls. Anker defines a typed protocol:
- **Message envelope**: sender_id, recipient_id, message_type, payload (typed), correlation_id, ttl, priority
- **Message types**: request, response, broadcast, subscribe, publish, delegate, escalate, report
- **Guarantees**: at-least-once delivery, exactly-once for idempotent messages, ordering per correlation_id
- **Discovery**: agents register capabilities with a local registry; other agents can query "who can do X"

### Principle 5: Self-Reflection & Introspection

Every agent maintains:
- An **execution trace** — a DAG of every decision, tool call, and message sent, with timestamps and latency
- A **self-evaluation loop** — after each task (or periodically), the agent evaluates its own performance against defined metrics (accuracy, completeness, efficiency, tool utilization)
- A **strategy journal** — what approach was tried, what worked, what didn't, what would change next time

### Principle 6: Recursive Self-Improvement Sandbox

The harness provides a secure sandbox where an agent can:
1. Fork a read-only copy of its own state
2. Modify its strategy, prompt, tool selection, or decision logic
3. Run the modified version on a set of benchmark tasks
4. Compare results against the baseline
5. If improved, propose the change as a patch to itself or to the fleet

This is the foundation for agents that get better over time without manual tuning.

### Principle 7: Multi-Model Routing

Not everything needs GPT-4. Anker routes decisions through an **LLM Router**:
- **Fast path**: small, cheap models for classification, extraction, formatting (e.g., Llama 3.2 3B, Claude Haiku)
- **Reasoning path**: strong models for planning, coding, complex reasoning (e.g., Claude Opus, GPT-4o, Gemini Pro)
- **Specialized path**: fine-tuned models for domain-specific tasks (code generation, medical, legal)
- The router learns from feedback which model works best for which task type.

### Principle 8: Self-Measuring Harness

The harness itself is instrumented:
- All agent operations are captured in a **telemetry stream** (structured events, not logs)
- The harness exposes a **health endpoint** with agent status, queue depths, error rates, memory usage
- **Anomaly detection** on telemetry triggers alerts or auto-remediation (e.g., restart a stuck agent, escalate to human)
- The harness can **query itself** — an agent can ask "what's the current system load?" or "how many tasks are queued for me?"

---

## 4. Architecture Blueprint

### 4.1 High-Level System Architecture (Text Diagram)

```
                           +----------------------------------+
                           |         Control Plane             |
                           |  (Orchestrator / Supervisor)      |
                           |  - Agent registry                 |
                           |  - Task scheduler                 |
                           |  - Policy engine                  |
                           |  - Lifecycle manager              |
                           +---------+------------------------+
                                     |
          +--------------------------+---------------------------+----------+
          |                          |                           |          |
+---------v--------+      +---------v--------+      +----------v-------+  |
|  Agent Pod A     |      |  Agent Pod B     |      |  Agent Pod C     |  |
|  Identity: ...   |      |  Identity: ...   |      |  Identity: ...   |  |
|  +------------+  |      |  +------------+  |      |  +------------+  |  |
|  | LLM Proxy  |  |      |  | LLM Proxy  |  |      |  | LLM Proxy  |  |  |
|  +------------+  |      |  +------------+  |      |  +------------+  |  |
|  +------------+  |      |  +------------+  |      |  +------------+  |  |
|  | Memory Hub |  |      |  | Memory Hub |  |      |  | Memory Hub |  |  |
|  +------------+  |      |  +------------+  |      |  +------------+  |  |
|  +------------+  |      |  +------------+  |      |  +------------+  |  |
|  | Tool Belt  |  |      |  | Tool Belt  |  |      |  | Tool Belt  |  |  |
|  +------------+  |      |  +------------+  |      |  +------------+  |  |
|  +------------+  |      |  +------------+  |      |  +------------+  |  |
|  | Sandbox    |  |      |  | Sandbox    |  |      |  | Sandbox    |  |  |
|  +------------+  |      |  +------------+  |      |  +------------+  |  |
+---------+--------+      +---------+--------+      +---------+--------+  |
          |                          |                           |       |
          +--------------------------+---------------------------+-------+
                                     |
                  +------------------+------------------+
                  |                                     |
         +--------v--------+              +-------------v----------+
         | Collective       |              | Self-Improvement      |
         | Memory Store     |              | Sandbox Cluster       |
         | (Shared KV +     |              | (Fork / Test /        |
         |  Vector DB)      |              |  Evaluate / Patch)    |
         +------------------+              +-----------------------+ 
                 
         +--------------------------------------------+
         |           Infrastructure Layer             |
         |  - Telemetry pipeline (OpenTelemetry)       |
         |  - LLM Router (multi-provider)              |
         |  - Auth / Policy / Secrets Vault            |
         |  - Storage backends (SQLite, PostgreSQL, S3) |
         +--------------------------------------------+
```

### 4.2 Agent Internal Architecture

```
   +------------------------------+
   |        Agent Core            |
   |  - Identity (UUID + name)    |
   |  - Lifecycle state machine   |
   |  - Strategy engine           |  <-- Plans, selects approach
   +-----+------------------------+
         |
   +-----v------------------------+
   |     Reasoning Loop           |
   |  1. Receive / dequeue task   |
   |  2. Retrieve relevant memory |
   |  3. Select strategy          |
   |  4. Route to LLM through     |
   |     router                   |
   |  5. Execute tool calls       |
   |  6. Store experience in      |
   |     episodic memory           |
   |  7. Self-evaluate            |
   |  8. Sleep / await next task  |
   +------------------------------+
```

### 4.3 Memory Architecture Detail

```
+------------------------------------------------+
|              Memory Hub                          |
|                                                  |
|  Working Memory (in-memory, LRU-evicted)        |
|  +------------------------------------------+  |
|  | Current task context, recent tool results |  |
|  | Token-budgeted (e.g., 8k tokens max)      |  |
|  +------------------------------------------+  |
|                                                  |
|  Episodic Memory (SQLite / PostgreSQL)          |
|  +------------------------------------------+  |
|  | (session_id, timestamp, task_desc,        |  |
|  |  strategy, tool_calls, outcome, latency,  |  |
|  |  self_eval_score)                         |  |
|  +------------------------------------------+  |
|                                                  |
|  Semantic Memory (Vector DB: Chroma / pgvector) |
|  +------------------------------------------+  |
|  | Text embeddings of learned facts,        |  |
|  | domain knowledge, agent's own notes      |  |
|  +------------------------------------------+  |
|                                                  |
|  Procedural Memory (SQLite with strategy JSON)  |
|  +------------------------------------------+  |
|  | (strategy_name, trigger_pattern,          |  |
|  |  prompt_template, tool_sequence,          |  |
|  |  success_rate, uses_count)                |  |
|  +------------------------------------------+  |
+------------------------------------------------+
```

---

## 5. Key Design Decisions & Rationale

### 5.1 Why Not Build on LangGraph / CrewAI / AutoGen?

| Decision | Rationale |
|---|---|
| **Not a state machine abstraction** | State machines are good for deterministic workflows; agents need non-deterministic, recursive, self-modifying execution |
| **Not a chat-loop abstraction** | Chat loops conflate conversation with computation; agents need typed message protocols, not text-in/text-out |
| **Not a role-play abstraction** | Role-playing is a prompt pattern, not an architecture; it doesn't scale to non-prompt-based strategies |
| **First-class memory hierarchy** | Every existing framework treats memory as "whatever fits in the context window plus a vector store bolted on" |
| **Built-in self-improvement** | No existing framework allows an agent to modify its own behavior based on self-evaluation |

### 5.2 When to Borrow from Existing Systems

- **Durable execution**: Borrow concepts from LangGraph's checkpoint/restart + Temporal.io's workflow patterns
- **Tool MCP protocol**: Align with Anthropic's Model Context Protocol for tool interop
- **Telemetry**: Use OpenTelemetry standard for the observability layer
- **Agent-to-agent protocol**: Build on A2A (Google's Agent-to-Agent) but extend it with typed payloads

---

## 6. Prioritized Roadmap

### Phase 0: Foundation (Weeks 1-4)
**Goal: Core agent runtime with working memory, tool belt, and single-agent task execution.**

- [ ] **P0**: Define Agent identity + lifecycle state machine
- [ ] **P0**: Build Reasoning Loop (perceive, plan, act, evaluate)
- [ ] **P0**: Working memory with token budgeting
- [ ] **P0**: Tool interface with capability manifests
- [ ] **P1**: LLM Router (multi-provider, model selection by task type)
- [ ] **P1**: Episodic memory store for agent execution history
- [ ] **P1**: Telemetry pipeline (events, not text logs)

### Phase 1: Multi-Agent & Communication (Weeks 5-8)
**Goal: Multiple agents can communicate, delegate, and coordinate.**

- [ ] **P0**: Typed A2A protocol (message envelope, delivery guarantees)
- [ ] **P0**: Agent registry + capability discovery
- [ ] **P1**: Task scheduler / orchestrator (control plane)
- [ ] **P1**: Broadcast / subscription / delegation patterns
- [ ] **P2**: Human-in-the-loop handoff protocol

### Phase 2: Memory & Persistence (Weeks 9-12)
**Goal: Full memory hierarchy with persistence across restarts.**

- [ ] **P0**: Semantic memory (vector DB integration for learned facts)
- [ ] **P0**: Procedural memory (strategy learning and recall)
- [ ] **P1**: Collective memory (shared across agent fleet)
- [ ] **P1**: Memory decay policies (TTL, LRU, importance-based)
- [ ] **P2**: Cross-agent memory sync (eventual consistency)

### Phase 3: Self-Improvement & Sandboxing (Weeks 13-16)
**Goal: Agents can evaluate and improve their own behavior.**

- [ ] **P0**: Self-evaluation loop (metrics, scoring, strategy journal)
- [ ] **P0**: Recursive sandbox (fork agent state, test modifications)
- [ ] **P1**: Patch generation and proposal protocol
- [ ] **P1**: Human approval gates for self-modification
- [ ] **P2**: Benchmark task suite for measuring improvement

### Phase 4: Tool Sandboxing & Security (Weeks 17-20)
**Goal: Safe execution of arbitrary tools.**

- [ ] **P0**: Sandbox interface abstraction (Docker / WASM / subprocess)
- [ ] **P0**: Capability manifest schema and enforcement
- [ ] **P1**: Resource limits and audit trail for all tool calls
- [ ] **P1**: Secrets vault integration (external secret stores)
- [ ] **P2**: Policy engine for tool access control

### Phase 5: Introspection & Observability (Weeks 21-24)
**Goal: The harness can see itself and report.**

- [ ] **P0**: Harness health endpoint and self-query API
- [ ] **P1**: Execution trace visualization (web UI)
- [ ] **P1**: Anomaly detection on telemetry
- [ ] **P2**: Agent-to-harness protocol (agents query harness state)
- [ ] **P2**: Auto-remediation of stuck or misbehaving agents

### Phase 6: Production Hardening (Weeks 25+) 
- [ ] Horizontal scaling of agent pods
- [ ] Multi-tenant isolation
- [ ] Persistent control plane (survive harness restart)
- [ ] Full MCP tool interop
- [ ] Public API / SDK for external agent development

---

## 7. Success Metrics

| Metric | Target |
|---|---|
| **Task completion rate** | >95% for defined task categories |
| **Self-improvement gain** | >20% accuracy/efficiency improvement after 100 iterations |
| **Memory recall accuracy** | >90% relevant retrieval on episodic/semantic queries |
| **Multi-agent throughput** | Linear scaling with agent count (within resource budget) |
| **Tool sandbox escape** | Zero escapes in security testing |
| **Time to first task** | < 5 minutes from install to running a task |

---

## 8. Open Questions (Awaiting Owner Decision)

1. **Language**: Python-first with Rust core for performance-critical loops? Or TypeScript for broader ecosystem?
2. **Sandbox tech**: Docker for heavy tools, WASM for lightweight? Or Firecracker for stronger isolation?
3. **Self-improvement guardrails**: What level of self-modification is allowed without human approval?
4. **Storage default**: SQLite for single-host, PostgreSQL for distributed? Both?
5. **LLM Router cost model**: Budget-aware routing (spend $ on hard tasks, not on easy ones)?

---

## 9. Glossary

| Term | Definition |
|---|---|
| **Agent Pod** | A runtime instance of an agent with its own memory hub, tool belt, and LLM proxy |
| **A2A-T** | Typed Agent-to-Agent protocol: structured message passing with type guarantees |
| **Capability Manifest** | Declared resource needs for a tool (filesystem, network, permissions) |
| **Strategy Engine** | Component that selects which approach/strategy to use for a given task |
| **Memory Hub** | Aggregate of all memory layers for a single agent |
| **LLM Router** | Routes requests to the appropriate model based on task type, cost, latency |
| **Control Plane** | Orchestrator managing agent lifecycle, task scheduling, policy enforcement |
| **Recursive Sandbox** | Secure container where an agent can fork itself to test modifications |

---

## 10. Appendix: Comparative Feature Matrix

| Feature | LangGraph | CrewAI | AutoGen/MAF | Anker (Vision) |
|---|---|---|---|---|
| Persistent agent identity | ❌ | ❌ | ❌ | ✅ |
| Multi-layer memory | ❌ | ❌ | ❌ | ✅ |
| Typed A2A protocol | ❌ | ❌ | ❌ | ✅ |
| Tool sandboxing | ❌ | ❌ | ❌ | ✅ |
| Self-reflection loop | ❌ | ❌ | ❌ | ✅ |
| Recursive self-improvement | ❌ | ❌ | ❌ | ✅ |
| Multi-model routing | ❌ | ❌ | Partial | ✅ |
| Capability-based security | ❌ | ❌ | ❌ | ✅ |
| Introspection/self-query | ❌ | ❌ | ❌ | ✅ |
| Agent lifecycle | ❌ | ❌ | ❌ | ✅ |
| Durable execution | ✅ | ❌ | Partial | ✅ |
| Human-in-the-loop | ✅ | Partial | ✅ | ✅ |
| MCP tool interop | ✅ | ✅ | ✅ | ✅ |
| Telemetry (structured) | Via LangSmith | Via AMP | Via MAF | Built-in OTEL |

---

*End of vision document v0.1. This document will evolve as implementation begins and architectural decisions are validated.*
