# RESEARCH.md — Technology & Open-Source Survey

## Scope

The core design problem (per the brief, §2) is composition: `Agent Core + Skill Pack + Sector Manifest + Customer Configuration = the agent a customer sees`, with the hard constraint that the core carries **zero sector-specific logic**. Four categories map directly onto this problem, so one representative candidate was chosen from each rather than a broad, unfocused list:

1. Agent orchestration framework (how a "core" runs pluggable behavior)
2. Skill/plugin composition system (how a "skill pack" is structured and discovered)
3. Policy/rules evaluation engine (how the approval gate should be structured)
4. Agent protocol standard (how manifests/capabilities are conventionally described)

Each candidate below was checked directly against its GitHub repo / release notes / license file as of August 2026 — not taken on faith from any single source.

---

## 1. LangGraph (agent orchestration framework)

- **Repo:** [langchain-ai/langgraph](https://github.com/langchain-ai/langgraph)
- **What it solves:** Graph/state-machine based orchestration of multi-step agent behavior — nodes are units of work, edges are transitions, and a shared state object flows through the graph. This is the closest existing analogue to "a core that orchestrates, permissions and audits while pluggable nodes do the sector-specific work."
- **License:** MIT for the core library. Note: the separate `langgraph-api` server component (used for LangGraph Platform's managed runtime) is Elastic License 2.0, not MIT — a distinction worth being precise about since we would only ever touch the core library.
- **Maintenance:** Very active — latest release 1.2.9 (July 10, 2026), with further PyPI releases through July 28, 2026. Backed by LangChain Inc., large contributor base.
- **What we'd inherit:** A general-purpose graph execution engine, its own state-management vocabulary (checkpoints, threads, reducers), and a dependency on the broader LangChain ecosystem's release cadence and API churn. It solves a more general problem (arbitrary multi-step graphs) than we have (a single linear intake → validate → decide → audit pipeline) — adopting it would mean carrying abstraction weight (checkpointing, streaming, interrupt/resume) we do not need for a ~200-line core.
- **Idea borrowed without adopting the code:** the notion of a strongly-typed, immutable state object passed through discrete stages is a clean shape for our own pipeline (intake → validation → decision → audit), even implemented as plain function composition.

## 2. Microsoft Semantic Kernel (skill/plugin composition system)

- **Repo:** [microsoft/semantic-kernel](https://github.com/microsoft/semantic-kernel)
- **What it solves:** A "plugin" abstraction (originally literally named *Skills* in early previews) that groups related functions under a namespace, callable uniformly whether backed by an LLM prompt or native code. This is a direct precedent for our skill-pack boundary: a named, self-describing unit the core can call without knowing its internals.
- **License:** MIT.
- **Maintenance:** Actively released (Python releases into late 2026), but with an important signal: Microsoft has published the **Microsoft Agent Framework (MAF)** as the "enterprise-ready successor to Semantic Kernel," now at a stable 1.0. That makes Semantic Kernel's multi-year trajectory uncertain — new projects are being steered toward MAF, and SK's future updates may slow as MAF matures.
- **What we'd inherit:** A mature, well-designed plugin/function-namespace pattern, but also a large general-purpose framework (connectors, memory, planners) and — per the maintenance signal above — a real risk of adopting a component that is being quietly superseded.
- **Idea borrowed without adopting the code:** the "named plugin, uniform call interface, self-describing metadata" shape directly informs our skill-pack interface (required fields, validation rules, templates declared *on* the pack, called uniformly by the core).

## 3. Cerbos (policy/authorization decision engine)

- **Repo:** [cerbos/cerbos](https://github.com/cerbos/cerbos)
- **What it solves:** A standalone Policy Decision Point — given a principal, a resource, and an action, it evaluates YAML-defined rules and returns an allow/deny decision with a machine-readable reason. This maps almost one-to-one onto our approval gate (R5): a request either satisfies the rules or comes back `PENDING_APPROVAL` with a reason, never a silent failure.
- **License:** Apache 2.0 (open core; no usage caps on the PDP itself).
- **Maintenance:** Active — latest release v0.51.0 (February 2026), ~4.3k GitHub stars, regular release cadence.
- **What we'd inherit:** A production-grade, network-callable policy service (typically run as a sidecar), its own policy DSL to learn, and an operational dependency (a running Cerbos instance) that is disproportionate to evaluating a handful of scheduling rules (working hours, capacity, double-booking) in-process. This is exactly the "heavy dependency for a two-hundred-line problem" the brief warns against.
- **Idea borrowed without adopting the code:** the core discipline — evaluate rules against a request, return a structured decision *plus the specific reason*, never just true/false — is adopted directly into our own approval-gate design, just implemented as plain Python rather than an external PDP.

## 4. Model Context Protocol (agent protocol standard)

- **Repo:** [modelcontextprotocol/modelcontextprotocol](https://github.com/modelcontextprotocol/modelcontextprotocol)
- **What it solves:** A standardized, versioned contract for how an LLM-facing system exposes discoverable tools/resources/capabilities to a client. Relevant here because our "sector manifest" is conceptually the same kind of thing: a declarative description of what's enabled and what an agent is allowed to do.
- **License:** MIT. Governed under the Linux Foundation, open to community contribution.
- **Maintenance:** Very active — a new specification revision shipped 2026-07-28, with the 2025-11-25 revision as the last stable baseline; official Python SDK maintained in step with the spec.
- **What we'd inherit:** Adopting MCP itself would mean standing up a protocol server/client and its transport/session machinery for a problem that doesn't need cross-process tool discovery — our "manifest" is read once, locally, at startup. Not proportionate here.
- **Idea borrowed without adopting the code:** the convention of a versioned, declarative capability manifest — read this shape, expose this shape, nothing implicit — is exactly the discipline applied to our sector manifest schema (Section on Data Contracts in `ARCHITECTURE.md`).

---

## Recommendation

**Build our own core.** None of the four candidates is adopted directly. Given the actual size of the problem (a single scheduling core, two skill packs, in-process rule evaluation, no multi-tenant or cross-process concerns), every candidate above would mean inheriting either a larger surface area than needed (LangGraph, Semantic Kernel) or an operational dependency out of proportion to the task (Cerbos as a sidecar, MCP as a protocol server). Each is genuinely well-built for the more general problem it targets — none of them is a bad choice in the abstract, they are simply the wrong altitude for a ~200-line, offline-testable prototype built in a matter of hours.

What *is* adopted is the reasoning behind each:

- From **LangGraph**: an explicit, typed state object flowing through discrete stages rather than mutating ad hoc dictionaries.
- From **Semantic Kernel**: a named, self-describing plugin boundary the core calls uniformly, without needing to know what's behind it.
- From **Cerbos**: return a structured decision with a specific machine-readable reason — never a bare boolean, never a crash.
- From **MCP**: a versioned, declarative manifest as the single source of truth for what's enabled, read once and validated, not inferred from code paths.

This keeps the core small enough to audit by eye against R1 (no sector-specific branching), while still being built on ideas that are proven at a larger scale than our own use case.
