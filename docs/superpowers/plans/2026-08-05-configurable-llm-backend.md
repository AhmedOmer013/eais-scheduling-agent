# Configurable LLM Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Ollama-only `LLMIntake` client with a generic OpenAI-compatible client, configured entirely via environment variables read in one place, so the same code path reaches either a local Ollama server or a hosted vLLM server (Qwen-72B, over Tailscale) with no code change — only config.

**Architecture:** `intake/llm.py`'s `OllamaHTTPClient` is replaced by `OpenAICompatibleHTTPClient`, which speaks the OpenAI `/chat/completions` shape both vLLM and recent Ollama versions support. `wiring.py` gains `build_llm_client()`, the sole place that reads the four `EAIS_LLM_*` env vars and constructs the client; `cli.py` and `http_api.py` call it explicitly instead of relying on an implicit default. `LLMIntake` itself is simplified to require an injected client — nothing internal to it changes behaviorally.

**Tech Stack:** Python stdlib only (`urllib.request`, `os.environ`) — no new dependency, matching the existing precedent.

## Global Constraints

- `core/` is never modified by this plan.
- No new runtime dependency — stdlib `urllib.request` only, same as the client being replaced.
- No test opens a real network socket (including loopback) — every test uses either an injected fake `HTTPClient` callable or a monkeypatched `urllib.request.urlopen`, preserving the project's existing "no network access in tests" claim in `README.md`'s "Run tests" section.
- Every task lands on branch `feature/configurable-llm-backend` (already checked out, branched off `develop`), gets its own commit(s), and must pass the full test suite before being considered done.
- Design spec: `docs/superpowers/specs/2026-08-05-configurable-llm-backend-design.md` — consult it for the *why* behind a decision if a step's rationale isn't obvious from the code alone.
- This work, and the two sub-projects planned after it (a web UI, Playwright E2E tests), are **explicitly outside** the `EAIS-HR-2159-TA-01` assessment brief's scope — see Task 4's `EXTENSIONS.md`. Nothing in this plan touches `RESEARCH.md`, `PLAN.md`, or `ARCHITECTURE.md`.

---

### Task 1: `OpenAICompatibleHTTPClient` replaces `OllamaHTTPClient`

**Files:**
- Modify: `eais_scheduling_agent/intake/llm.py`
- Modify: `tests/test_llm_intake.py`

**Interfaces:**
- Produces (consumed by Task 2's `wiring.build_llm_client()`):
  - `OpenAICompatibleHTTPClient(base_url: str, model: str, api_key: Optional[str] = None, timeout: float = 60.0)` — implements the existing `HTTPClient = Callable[[str], str]` protocol. `__call__(prompt: str) -> str` POSTs to `{base_url}/chat/completions` and returns `response["choices"][0]["message"]["content"]`.
  - `LLMIntake.__init__(self, fallback: IntakeService, client: HTTPClient) -> None` — `client` is now **required** (was `Optional[HTTPClient] = None` with an implicit `OllamaHTTPClient` default); the `model`/`base_url`/`timeout` convenience parameters are removed entirely, since they only existed to configure that implicit default. Every existing call site in this codebase already passes `client=` explicitly (confirmed by grep before writing this plan), so this is not a breaking change to any test — only to `cli.py`/`http_api.py`, fixed in Task 3.

- [ ] **Step 1: Write failing tests for `OpenAICompatibleHTTPClient`'s request shape**

Add to `tests/test_llm_intake.py`, near the top (after the existing imports, before `class _FakeClient:`):

```python
import urllib.request

from eais_scheduling_agent.intake.llm import OpenAICompatibleHTTPClient


class _FakeHTTPResponse:
    """Minimal stand-in for the context-manager object `urlopen()` returns."""

    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False
```

Then add this new test class (anywhere after that, e.g. right before `class TestLLMIntakeSatisfiesInterface:`):

```python
class TestOpenAICompatibleHTTPClient:
    """Tests the real HTTPClient implementation directly, via a monkeypatched
    `urllib.request.urlopen` -- no real socket, loopback or otherwise, per
    this plan's Global Constraints.
    """

    def test_posts_to_chat_completions_with_messages_shape(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return _FakeHTTPResponse(
                json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(
            base_url="http://example.invalid/v1", model="test-model", timeout=42.0
        )

        client("some prompt")

        assert captured["url"] == "http://example.invalid/v1/chat/completions"
        assert captured["method"] == "POST"
        assert captured["body"]["model"] == "test-model"
        assert captured["body"]["messages"] == [{"role": "user", "content": "some prompt"}]
        assert captured["body"]["response_format"] == {"type": "json_object"}
        assert captured["timeout"] == 42.0

    def test_extracts_content_from_choices_message(self, monkeypatch):
        def fake_urlopen(request, timeout):
            return _FakeHTTPResponse(
                json.dumps(
                    {"choices": [{"message": {"content": '{"practitioner": "Dr. A"}'}}]}
                ).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(base_url="http://example.invalid/v1", model="m")

        result = client("prompt")

        assert result == '{"practitioner": "Dr. A"}'

    def test_no_authorization_header_when_api_key_not_given(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            return _FakeHTTPResponse(
                json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(base_url="http://example.invalid/v1", model="m")

        client("prompt")

        assert captured["authorization"] is None

    def test_authorization_bearer_header_when_api_key_given(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["authorization"] = request.get_header("Authorization")
            return _FakeHTTPResponse(
                json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(
            base_url="http://example.invalid/v1", model="m", api_key="secret-key"
        )

        client("prompt")

        assert captured["authorization"] == "Bearer secret-key"

    def test_trailing_slash_on_base_url_does_not_double_up(self, monkeypatch):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return _FakeHTTPResponse(
                json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")
            )

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        client = OpenAICompatibleHTTPClient(base_url="http://example.invalid/v1/", model="m")

        client("prompt")

        assert captured["url"] == "http://example.invalid/v1/chat/completions"
```

- [ ] **Step 2: Run to verify these tests fail**

Run: `python -m pytest tests/test_llm_intake.py -v -k OpenAICompatibleHTTPClient`
Expected: `FAIL` / `ERROR` — `ImportError: cannot import name 'OpenAICompatibleHTTPClient'`

- [ ] **Step 3: Replace `OllamaHTTPClient` with `OpenAICompatibleHTTPClient` in `intake/llm.py`**

Replace the entire `class OllamaHTTPClient:` block (from `class OllamaHTTPClient:` through the end of its `__call__` method, i.e. the whole class) with:

```python
class OpenAICompatibleHTTPClient:
    """Real `HTTPClient`: POSTs to any OpenAI-compatible `/chat/completions` endpoint.

    Both a local Ollama server (recent versions expose an OpenAI-compatible
    API alongside their native one) and a hosted vLLM server serving a
    larger model use this same request/response shape -- "local" and
    "hosted" are the same code path here, differing only in which
    `base_url` / `model` / `api_key` are configured. See
    `wiring.build_llm_client`, the one place those are read from the
    environment.

    Uses stdlib `urllib.request` only -- no new dependency, same precedent
    as the client this replaces.

    Never constructed directly by tests -- tests inject their own fake
    `HTTPClient` callable, or monkeypatch `urllib.request.urlopen` to test
    this class's own request-building logic without a real socket. See
    `tests/test_llm_intake.py::TestOpenAICompatibleHTTPClient`.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ) -> None:
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout

    def __call__(self, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                # Best-effort hint, same as the native client's "format":
                # "json" -- not relied upon; `_parse_and_validate` below
                # validates independently either way.
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(
            self._url,
            data=payload,
            headers=headers,
            method="POST",
        )
        # Any failure here (connection refused, DNS failure, timeout, a
        # non-2xx status raised by urllib as HTTPError) propagates to the
        # caller unchanged -- `LLMIntake.parse` is the layer that decides
        # what to do about it.
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            body = response.read().decode("utf-8")
        envelope = json.loads(body)
        return envelope["choices"][0]["message"]["content"]
```

Update the module docstring's "Two independently swappable layers" section (point 1, currently describing `HTTPClient` / `OllamaHTTPClient` and Ollama's `/api/generate`) to instead say:

```
    1. **The HTTP client** (`HTTPClient` / `OpenAICompatibleHTTPClient`) --
       the only part of this module that touches the network. `LLMIntake`
       accepts any zero-state callable `str -> str` (prompt in, model's
       raw text out) via its `client` constructor argument.
       `OpenAICompatibleHTTPClient` is the real implementation, speaking
       the OpenAI-compatible `/chat/completions` shape that both a local
       Ollama server and a hosted vLLM server understand -- which backend
       it talks to is purely a matter of which `base_url`/`model`/`api_key`
       it was constructed with (see `wiring.build_llm_client`), never a
       code difference. Tests inject a fake callable instead; see
       `tests/test_llm_intake.py`. This is what makes "zero real network
       calls in tests" possible without a mocking framework: dependency
       injection, not patching.
```

Now update `LLMIntake.__init__` from:

```python
    def __init__(
        self,
        fallback: IntakeService,
        client: Optional[HTTPClient] = None,
        model: str = "llama3.2",
        base_url: str = "http://localhost:11434",
        timeout: float = 10.0,
    ) -> None:
        self._fallback = fallback
        self._client: HTTPClient = (
            client
            if client is not None
            else OllamaHTTPClient(model=model, base_url=base_url, timeout=timeout)
        )
```

to:

```python
    def __init__(
        self,
        fallback: IntakeService,
        client: HTTPClient,
    ) -> None:
        self._fallback = fallback
        self._client = client
```

And its docstring's `Args:` block from:

```
        client: Injectable `HTTPClient` (prompt string in, model's raw
            text response out; may raise on failure). Defaults to a real
            `OllamaHTTPClient`. Tests always pass their own fake callable
            here so no test touches a real network -- see
            `tests/test_llm_intake.py`.
        model: Ollama model name, used only when `client` is not supplied
            (it configures the default `OllamaHTTPClient`).
        base_url: Ollama server base URL, same caveat as `model`.
        timeout: Per-request timeout in seconds, same caveat as `model`.
```

to:

```
        client: `HTTPClient` (prompt string in, model's raw text response
            out; may raise on failure). Required -- production code
            builds one explicitly via `wiring.build_llm_client()` (see
            `cli.py`/`http_api.py`); tests always pass their own fake
            callable so no test touches a real network -- see
            `tests/test_llm_intake.py`.
```

- [ ] **Step 3b: Fix the test file's own module docstring**

`tests/test_llm_intake.py`'s module docstring (top of the file) currently claims:

```
None of them touch a real socket, `urllib`, or Ollama: per
the T13 brief, this environment (and CI) has no LLM runtime installed, and
the whole point of the injectable-client design in `intake/llm.py` is that
the test suite never needs one. `OllamaHTTPClient` (the real implementation)
is never constructed anywhere in this file.
```

This is now wrong twice over: the class is renamed, and `TestOpenAICompatibleHTTPClient` (added in Step 1) *does* construct the real client directly — just never over a real socket, since `urllib.request.urlopen` is monkeypatched in every one of its tests. Replace that last sentence with:

```
`OpenAICompatibleHTTPClient` (the real implementation) is constructed
directly in `TestOpenAICompatibleHTTPClient` below, but every one of
those tests monkeypatches `urllib.request.urlopen` first -- no test in
this file ever opens a real socket.
```

- [ ] **Step 4: Run to verify the new tests pass**

Run: `python -m pytest tests/test_llm_intake.py -v -k OpenAICompatibleHTTPClient`
Expected: `PASS` (5 tests)

- [ ] **Step 5: Run the full `test_llm_intake.py` file to confirm no regression**

Run: `python -m pytest tests/test_llm_intake.py -v`
Expected: `PASS` (all existing tests plus the 5 new ones) — every existing test already passes `client=` explicitly (confirmed before writing this plan), so removing the implicit default does not affect them.

- [ ] **Step 6: Commit**

```bash
git add eais_scheduling_agent/intake/llm.py tests/test_llm_intake.py
git commit -m "Replace OllamaHTTPClient with OpenAICompatibleHTTPClient

Speaks the OpenAI-compatible /chat/completions shape both vLLM and
recent Ollama versions support, so local and hosted models are the
same code path -- differing only in base_url/model/api_key, wired in
Task 2/3. LLMIntake's client parameter is now required; every existing
call site already passed it explicitly."
```

---

### Task 2: `wiring.build_llm_client()` — the one place env vars are read

**Files:**
- Modify: `eais_scheduling_agent/wiring.py`
- Modify: `tests/test_wiring.py`

**Interfaces:**
- Consumes: `OpenAICompatibleHTTPClient` (Task 1), importable as `eais_scheduling_agent.intake.llm.OpenAICompatibleHTTPClient`.
- Produces (consumed by Task 3): `wiring.build_llm_client() -> HTTPClient` — reads `EAIS_LLM_BASE_URL` (default `"http://localhost:11434/v1"`), `EAIS_LLM_MODEL` (default `"llama3.2"`), `EAIS_LLM_API_KEY` (default `None`, i.e. `os.environ.get(...)` with no fallback string), `EAIS_LLM_TIMEOUT` (default `60.0`, parsed as `float`).

- [ ] **Step 1: Write failing tests for `build_llm_client`**

Add to `tests/test_wiring.py`, after the existing imports (add `import os` if not already present) and before or after the existing test classes:

```python
from eais_scheduling_agent.intake.llm import OpenAICompatibleHTTPClient


class TestBuildLLMClient:
    def test_defaults_when_no_env_vars_set(self, monkeypatch):
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        client = wiring.build_llm_client()

        assert isinstance(client, OpenAICompatibleHTTPClient)
        assert client._url == "http://localhost:11434/v1/chat/completions"
        assert client._model == "llama3.2"
        assert client._api_key is None
        assert client._timeout == 60.0

    def test_reads_base_url_override(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_BASE_URL", "http://100.64.0.5:8000/v1")
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        client = wiring.build_llm_client()

        assert client._url == "http://100.64.0.5:8000/v1/chat/completions"

    def test_reads_model_override(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_MODEL", "Qwen/Qwen2.5-72B-Instruct")
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        client = wiring.build_llm_client()

        assert client._model == "Qwen/Qwen2.5-72B-Instruct"

    def test_reads_api_key_override(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_API_KEY", "secret-key")
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_TIMEOUT", raising=False)

        client = wiring.build_llm_client()

        assert client._api_key == "secret-key"

    def test_reads_timeout_override_as_float(self, monkeypatch):
        monkeypatch.setenv("EAIS_LLM_TIMEOUT", "120")
        monkeypatch.delenv("EAIS_LLM_BASE_URL", raising=False)
        monkeypatch.delenv("EAIS_LLM_MODEL", raising=False)
        monkeypatch.delenv("EAIS_LLM_API_KEY", raising=False)

        client = wiring.build_llm_client()

        assert client._timeout == 120.0
```

(These read `client._url`/`._model`/`._api_key`/`._timeout` — private attributes — deliberately: `build_llm_client`'s whole job is constructing the client with the right values, and `OpenAICompatibleHTTPClient` has no public getters for them. This mirrors how `TestLoadManifestForRender` etc. already inspect real objects' concrete state rather than mocking.)

- [ ] **Step 2: Run to verify these tests fail**

Run: `python -m pytest tests/test_wiring.py -v -k BuildLLMClient`
Expected: `FAIL` / `ERROR` — `AttributeError: module 'eais_scheduling_agent.wiring' has no attribute 'build_llm_client'`

- [ ] **Step 3: Add `build_llm_client()` to `wiring.py`**

Add `import os` to the top of `eais_scheduling_agent/wiring.py`'s imports, and add this import:

```python
from eais_scheduling_agent.intake.llm import HTTPClient, OpenAICompatibleHTTPClient
```

Add these module-level constants near `DEFAULT_MANIFEST_DIR`:

```python
#: Defaults for `build_llm_client()` below -- deliberately preserve
#: today's zero-config behavior (a local Ollama server on its default
#: port), now via its OpenAI-compatible endpoint rather than its native
#: one. Overridden via environment variables, never hardcoded elsewhere.
_DEFAULT_LLM_BASE_URL = "http://localhost:11434/v1"
_DEFAULT_LLM_MODEL = "llama3.2"
_DEFAULT_LLM_TIMEOUT = 60.0
```

Add this function, after `render_confirmation` and before `class CachingIntake`:

```python
def build_llm_client() -> HTTPClient:
    """Build the LLM `HTTPClient` from environment configuration.

    The one place `EAIS_LLM_*` environment variables are read -- `cli.py`
    and `http_api.py` both call this rather than reading the environment
    themselves, and `LLMIntake`/`OpenAICompatibleHTTPClient` stay entirely
    environment-agnostic (directly constructible and testable with no env
    var setup at all).

    Reads:
        EAIS_LLM_BASE_URL: OpenAI-compatible API root (default: local
            Ollama's OpenAI-compatible endpoint). `/chat/completions` is
            appended by `OpenAICompatibleHTTPClient` itself.
        EAIS_LLM_MODEL: Model name sent in each request (default:
            "llama3.2", the locally-pulled default this project has
            always assumed).
        EAIS_LLM_API_KEY: If set, sent as an `Authorization: Bearer`
            header; unset means no auth header at all (a local Ollama
            server needs none; a hosted vLLM server may require one).
        EAIS_LLM_TIMEOUT: Per-request timeout in seconds, as a float
            (default: 60.0 -- a larger, remotely-hosted model is expected
            to respond slower than a small local one).

    Unset variables fall back to the exact defaults `OllamaHTTPClient`
    (the client this replaced) used to hardcode, so `--llm` /
    `"llm": true` with no configuration at all keeps working exactly as
    before.
    """
    base_url = os.environ.get("EAIS_LLM_BASE_URL", _DEFAULT_LLM_BASE_URL)
    model = os.environ.get("EAIS_LLM_MODEL", _DEFAULT_LLM_MODEL)
    api_key = os.environ.get("EAIS_LLM_API_KEY")
    timeout = float(os.environ.get("EAIS_LLM_TIMEOUT", _DEFAULT_LLM_TIMEOUT))
    return OpenAICompatibleHTTPClient(
        base_url=base_url, model=model, api_key=api_key, timeout=timeout
    )
```

- [ ] **Step 4: Run to verify the new tests pass**

Run: `python -m pytest tests/test_wiring.py -v -k BuildLLMClient`
Expected: `PASS` (5 tests)

- [ ] **Step 5: Run the full `test_wiring.py` file to confirm no regression**

Run: `python -m pytest tests/test_wiring.py -v`
Expected: `PASS` (all existing tests plus the 5 new ones)

- [ ] **Step 6: Commit**

```bash
git add eais_scheduling_agent/wiring.py tests/test_wiring.py
git commit -m "Add wiring.build_llm_client(), the one place LLM env vars are read

EAIS_LLM_BASE_URL / EAIS_LLM_MODEL / EAIS_LLM_API_KEY / EAIS_LLM_TIMEOUT,
all with defaults matching today's zero-config Ollama behavior. Neither
LLMIntake nor OpenAICompatibleHTTPClient reads the environment directly."
```

---

### Task 3: Wire it into `cli.py` and `http_api.py`; fix now-stale references

**Files:**
- Modify: `eais_scheduling_agent/cli.py`
- Modify: `eais_scheduling_agent/http_api.py`
- Modify: `tests/test_http_api.py` (docstring only)
- Modify: `tests/test_cli.py` (docstring only)
- Modify: `README.md`

**Interfaces:**
- Consumes: `wiring.build_llm_client()` (Task 2).

- [ ] **Step 1: Update `cli.py`'s `main()` to pass the client explicitly**

In `eais_scheduling_agent/cli.py`, change:

```python
    offline = OfflineIntake()
    real_intake: IntakeService = LLMIntake(fallback=offline) if args.llm else offline
```

to:

```python
    offline = OfflineIntake()
    real_intake: IntakeService = (
        LLMIntake(fallback=offline, client=wiring.build_llm_client())
        if args.llm
        else offline
    )
```

- [ ] **Step 2: Update `http_api.py`'s `post_booking()` to pass the client explicitly**

In `eais_scheduling_agent/http_api.py`, change:

```python
        if use_llm:
            intake = wiring.CachingIntake(LLMIntake(fallback=OfflineIntake()))
        else:
            intake = wiring.CachingIntake(OfflineIntake())
```

to:

```python
        if use_llm:
            intake = wiring.CachingIntake(
                LLMIntake(fallback=OfflineIntake(), client=wiring.build_llm_client())
            )
        else:
            intake = wiring.CachingIntake(OfflineIntake())
```

- [ ] **Step 3: Run the CLI and HTTP test suites to confirm no regression**

Run: `python -m pytest tests/test_cli.py tests/test_http_api.py -v`
Expected: `PASS`, same counts as before this task — `TestLLMFlagFallsBackCleanly` (both files' versions) still falls back cleanly, since `wiring.build_llm_client()`'s default (`http://localhost:11434/v1/chat/completions`) still has nothing listening in this environment, so the connection still fails and `LLMIntake.parse()`'s existing exception handling still catches it.

- [ ] **Step 4: Fix stale `OllamaHTTPClient` references in test docstrings**

In `tests/test_http_api.py`, change the module docstring's:

```
Flask itself never
opens a real socket (`test_client()` is in-process), matching the
project's existing "no network access in tests" discipline (see
README.md's "Run tests" section). The one exception is the `"llm": true`
path (`TestLLMFlagFallsBackCleanly` below), which exercises the real
`LLMIntake` -> `OllamaHTTPClient` -> `urllib.request.urlopen(...)` chain
```

to:

```
Flask itself never
opens a real socket (`test_client()` is in-process), matching the
project's existing "no network access in tests" discipline (see
README.md's "Run tests" section). The one exception is the `"llm": true`
path (`TestLLMFlagFallsBackCleanly` below), which exercises the real
`LLMIntake` -> `OpenAICompatibleHTTPClient` -> `urllib.request.urlopen(...)` chain
```

In `tests/test_cli.py`, change `TestLLMFlagFallsBackCleanly`'s docstring from:

```python
class TestLLMFlagFallsBackCleanly:
    """`--llm` with no local Ollama server still completes via fallback.
```

to:

```python
class TestLLMFlagFallsBackCleanly:
    """`--llm` with no LLM backend reachable still completes via fallback.
```

- [ ] **Step 5: Fix stale Ollama-specific wording in `README.md`**

In `README.md`, change the `### --llm mode` section's opening paragraph from:

```
`--llm` swaps the offline parser for an LLM-backed one (via a local
[Ollama](https://ollama.com) server, model `llama3.2` by default). It
requires a locally running Ollama instance to do anything beyond what
offline mode already does — **it is not required for the default
path**, and `--llm` is safe to pass even without Ollama running: on any
failure (Ollama not running, unreachable, or returning something
unusable) it automatically and silently falls back to the same offline
parser used by default, never raising and never blocking. Verified in
this environment (no Ollama installed here):
```

to:

```
`--llm` swaps the offline parser for an LLM-backed one, talking to any
OpenAI-compatible API — a local [Ollama](https://ollama.com) server by
default (model `llama3.2`), or a hosted server (e.g. vLLM) if configured
-- see "Configuring the LLM backend" below. It requires a reachable LLM
server to do anything beyond what offline mode already does — **it is
not required for the default path**, and `--llm` is safe to pass even
without one running: on any failure (unreachable, timed out, or
returning something unusable) it automatically and silently falls back
to the same offline parser used by default, never raising and never
blocking. Verified in this environment (no local model server running
here):
```

In `README.md`'s "Known gaps" section, change:

```
- **No local LLM runtime is installed in this environment.** `LLMIntake`'s
  real Ollama-calling code (`OllamaHTTPClient`) is exercised in
  production but not against a real network call in this environment or
  CI; its fallback and validation logic are fully tested against
  injected fakes, and its failure-handling contract is exercised
  end-to-end (see `--llm` above).
```

to:

```
- **No local LLM runtime is installed in this environment.** `LLMIntake`'s
  real network-calling code (`OpenAICompatibleHTTPClient`) is exercised
  in production but not against a real network call in this environment
  or CI; its request-building logic is directly tested via a
  monkeypatched `urlopen`, its fallback and validation logic are fully
  tested against injected fakes, and its failure-handling contract is
  exercised end-to-end (see `--llm` above).
```

- [ ] **Step 6: Run the full test suite**

Run: `python -m pytest`
Expected: `PASS`, count up by 10 from before this plan's Task 1 (5 `OpenAICompatibleHTTPClient` tests + 5 `build_llm_client` tests).

- [ ] **Step 7: Commit**

```bash
git add eais_scheduling_agent/cli.py eais_scheduling_agent/http_api.py tests/test_http_api.py tests/test_cli.py README.md
git commit -m "Wire wiring.build_llm_client() into cli.py and http_api.py

Both --llm and \"llm\": true now build their client explicitly rather
than relying on LLMIntake's old implicit Ollama default. Fixes stale
OllamaHTTPClient references left over in test docstrings and README."
```

---

### Task 4: Disclosure and configuration docs

**Files:**
- Create: `EXTENSIONS.md`
- Modify: `README.md`

**Interfaces:** None — documentation only.

- [ ] **Step 1: Create `EXTENSIONS.md` at the repo root**

```markdown
# EXTENSIONS.md

This file exists to keep one thing unambiguous: everything described here
is **outside the scope of the `EAIS-HR-2159-TA-01` technical assessment
brief**, built afterward at the repo owner's explicit request, after being
told plainly that it goes against the brief's own scope guidance. It is
not referenced from `RESEARCH.md`, `PLAN.md`, or `ARCHITECTURE.md`, which
remain accurate, unmodified records of the brief-scoped submission.

## Why this file exists

The brief (§5.3) explicitly lists "real integrations" and "production
infrastructure" as out of scope, and separately instructs against building
"any user interface, dashboard or front end" (also §5.3), warning that
doing so "will not earn points and may cost you points for poor scoping."
The work below does some of exactly that. It was a deliberate, informed
choice, not an oversight -- and per this project's own established
practice of disclosing scope changes rather than hiding them (see
`PLAN.md` §8), it gets the same treatment here, in its own clearly
separated file.

## Extensions

### 1. Configurable LLM backend (local + hosted)

`LLMIntake`'s HTTP client (`eais_scheduling_agent/intake/llm.py`) now
speaks a generic OpenAI-compatible `/chat/completions` API instead of
being hardcoded to a local Ollama server. Configured via four environment
variables, read in one place (`wiring.build_llm_client()`):

- `EAIS_LLM_BASE_URL` (default: `http://localhost:11434/v1`)
- `EAIS_LLM_MODEL` (default: `llama3.2`)
- `EAIS_LLM_API_KEY` (default: unset -- no auth header sent)
- `EAIS_LLM_TIMEOUT` (default: `60.0` seconds)

Both a local Ollama server and a hosted vLLM server (the intended use: a
Qwen-72B model, reached over Tailscale) are the same code path -- only
the configuration differs. See
`docs/superpowers/specs/2026-08-05-configurable-llm-backend-design.md`
for the full design rationale.

### 2. Web UI *(planned, not yet built)*

A browser-based front end for making booking requests, built on top of
the existing `POST /bookings` / `GET /audit` HTTP API
(`eais_scheduling_agent/http_api.py`). Will get its own design spec and
plan before implementation, same as everything else in this repo.

### 3. Playwright end-to-end tests *(planned, not yet built)*

Drives the web UI above through a real browser once it exists.
```

- [ ] **Step 2: Add a "Configuring the LLM backend" section to `README.md`**

Add this new section immediately after the `### --llm mode` subsection (i.e. right before `## Run the HTTP API (optional)`):

```markdown
### Configuring the LLM backend

`--llm` (CLI) and `"llm": true` (HTTP API) both talk to whatever
OpenAI-compatible server these environment variables point at. Unset,
they default to a local Ollama server -- set them to reach a different
local model or a hosted one instead:

| Variable | Default | Meaning |
|---|---|---|
| `EAIS_LLM_BASE_URL` | `http://localhost:11434/v1` | OpenAI-compatible API root; `/chat/completions` is appended automatically |
| `EAIS_LLM_MODEL` | `llama3.2` | Model name sent in each request |
| `EAIS_LLM_API_KEY` | unset | Sent as `Authorization: Bearer <key>` if set; omitted entirely otherwise |
| `EAIS_LLM_TIMEOUT` | `60.0` | Per-request timeout in seconds |

This is not part of the original assessment brief's scope -- see
`EXTENSIONS.md`.
```

- [ ] **Step 3: Run the full test suite one more time**

Run: `python -m pytest`
Expected: `PASS`, same count as Task 3's end state (docs-only changes in this task).

- [ ] **Step 4: Commit**

```bash
git add EXTENSIONS.md README.md
git commit -m "Add EXTENSIONS.md and README LLM-backend config section"
```

---

## After all four tasks

- [ ] Push the branch: `git push -u origin feature/configurable-llm-backend`
- [ ] Open a PR against `develop`, wait for both CI checks (Python 3.11, Python 3.12). **Do not merge without Ahmed's explicit go-ahead**, same as every other PR this session.
