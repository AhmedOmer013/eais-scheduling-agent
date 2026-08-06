# Configurable LLM backend (local + hosted) — design

**Date:** 2026-08-05
**Status:** Approved by Ahmed, ready for implementation planning.

## Scope note — read this first

**This sub-project, and the two that follow it (a web UI, Playwright E2E tests), are explicitly outside the EAIS-HR-2159-TA-01 assessment brief's scope.** The brief's §5.2 leaves "which LLM provider" up to the implementer but does not ask for multiple, swappable, deployment-configured providers; §5.3 lists "real integrations" and "production infrastructure" as **out of scope**, and a following sub-project (the web UI) directly contradicts §5.3's explicit "no UI/dashboard" instruction. Ahmed was told this twice, in plain terms, before asking to proceed anyway — this is a deliberate, informed decision, not an oversight. Per this project's own established practice of disclosing scope changes honestly (see `PLAN.md` §8's HTTP-interface addendum), this work is documented in a new, separate `EXTENSIONS.md` at the repo root — **not** woven into `RESEARCH.md`, `PLAN.md`, or `ARCHITECTURE.md`, which remain accurate records of the brief-scoped submission and should not appear to claim this extension work as part of that scope.

## Context

Today, `LLMIntake` (`eais_scheduling_agent/intake/llm.py`) talks to a local Ollama server only, via `OllamaHTTPClient`, hardcoded to Ollama's native `/api/generate` endpoint with hardcoded-default `model="llama3.2"`, `base_url="http://localhost:11434"`, `timeout=10.0`. Ahmed wants to also reach a hosted model — vLLM serving Qwen-72B on a remote virtual server, accessed over Tailscale (exact hostname not yet available) — without hardcoding either backend into the code, and with the choice/connection details configurable at deployment time ("the configuration is theirs").

## Key insight

vLLM's standard serving mode exposes an **OpenAI-compatible** API (`/v1/chat/completions`). Recent Ollama versions expose the *same* OpenAI-compatible shape alongside their native API. That means "local" and "hosted" don't need two separate code paths — one generic OpenAI-compatible client, pointed at different URLs/models/keys via configuration, covers both. Ahmed confirmed this approach over the alternative (keeping the native Ollama client and adding a second, hosted-only client).

## Decisions

### 1. Replace `OllamaHTTPClient` with `OpenAICompatibleHTTPClient`

Same `HTTPClient = Callable[[str], str]` contract `LLMIntake` already depends on — so `LLMIntake.parse()`, `_build_prompt()`, and `_parse_and_validate()` in `intake/llm.py` are **untouched**. Only the client implementation changes.

```python
class OpenAICompatibleHTTPClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None: ...

    def __call__(self, prompt: str) -> str:
        # POST {base_url}/chat/completions
        # body: {"model": model, "messages": [{"role": "user", "content": prompt}],
        #        "response_format": {"type": "json_object"}}
        # header: Authorization: Bearer {api_key}  -- only if api_key is not None
        # returns: response body's choices[0]["message"]["content"]
        ...
```

`response_format: {"type": "json_object"}` is a best-effort hint (vLLM and Ollama's OpenAI-compat layer both support it) — same "don't trust it, validate independently" philosophy `_parse_and_validate` already implements; nothing downstream changes its behavior based on whether the hint was honored.

Any request/connection/timeout/non-2xx failure propagates out of `__call__` unchanged, exactly like today's `OllamaHTTPClient` — `LLMIntake.parse()`'s existing `except Exception: return self._fallback.parse(...)` handling needs no changes.

### 2. Env var contract, read in exactly one place

New `wiring.build_llm_client() -> HTTPClient` in `eais_scheduling_agent/wiring.py`:

| Var | Default | Meaning |
|---|---|---|
| `EAIS_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible API root; client appends `/chat/completions` |
| `EAIS_LLM_MODEL` | `llama3.2` | Model name sent in the request body |
| `EAIS_LLM_API_KEY` | unset | If set, sent as `Authorization: Bearer <key>`; header omitted entirely if unset |
| `EAIS_LLM_TIMEOUT` | `60.0` | Per-request timeout in seconds |

`LLMIntake` itself stays env-agnostic — it only ever sees an already-constructed `HTTPClient` (injected, same as today), so it remains directly unit-testable without touching the environment. Env var reading happens exactly once, in `wiring.build_llm_client()`, matching how `wiring.py` already centralizes deployment-specific assembly (skill packs today; the LLM client now).

Zero-config behavior stays "talks to local Ollama" — `--llm` / `"llm": true` still works out of the box with nothing set. The real behavior change: the *default* now targets Ollama's OpenAI-compatible endpoint (`/v1/chat/completions`) rather than the native `/api/generate` it used before. Functionally equivalent for a locally-running Ollama with the given model pulled; this is the trade-off Ahmed accepted when choosing the unified-client approach. Once the Tailscale URL exists, pointing at the hosted Qwen-72B model is four env vars, no code change.

### 3. Consumers updated to build the client explicitly

`cli.py`'s `main()` and `http_api.py`'s `post_booking()` currently construct `LLMIntake(fallback=OfflineIntake())` and let it default-construct an `OllamaHTTPClient` internally. Both change to:

```python
LLMIntake(fallback=OfflineIntake(), client=wiring.build_llm_client())
```

`LLMIntake.__init__`'s `client: Optional[HTTPClient] = None` parameter stays for direct-construction convenience (tests, ad-hoc scripts) — if genuinely no client is given, its old hardcoded-default construction path can be dropped entirely, since production code now always passes one explicitly; a caller that constructs `LLMIntake` with no client at all is a test or an intentional low-level use, not something the module needs to paper over with an implicit Ollama default.

### 4. Testing

Same pattern as today: tests inject a fake `HTTPClient` callable, zero real network calls anywhere. `tests/test_llm_intake.py` updates to cover the new client's request shape (`messages` array, `response_format` hint) and response shape (`choices[0].message.content`), plus the `Authorization` header appearing only when `api_key` is given. New tests for `wiring.build_llm_client()` cover each env var's default and override, via `monkeypatch.setenv`/`delenv`.

### 5. Documentation

- New `EXTENSIONS.md` at the repo root (see the Scope note above) — states plainly this and the following two sub-projects are a disclosed, deliberate expansion beyond `EAIS-HR-2159-TA-01`'s scope, built at Ahmed's explicit request after being informed of the brief's contrary guidance. Not linked from or referenced by `RESEARCH.md`/`PLAN.md`/`ARCHITECTURE.md`.
- `README.md` gets a short new "Configuring the LLM backend" section listing the four env vars and their defaults, alongside the existing `--llm` documentation.

## Out of scope (explicitly, for this sub-project)

- The actual Tailscale hostname/URL — not yet available; this design makes it a config value, not a code change, for whenever Ahmed has it.
- Streaming responses, retries, or any client-side resilience beyond what already exists (the fallback-to-offline behavior).
- The web UI and Playwright E2E tests — separate sub-projects, specced and built after this one.

## Workflow

Same as every other change in this repo: a feature branch off `develop` (`feature/configurable-llm-backend`), its own tests, a PR, both CI checks green before squash-merge, no merge without Ahmed's explicit go-ahead.
