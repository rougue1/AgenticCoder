import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ==========================================
# CONFIG LOADER
# ==========================================

_config_cache: dict | None = None

DEFAULTS = {
    "models": {
        "architect": "deepseek-r1:32b",
        "surgeon": "qwen2.5-coder:32b",
        "healer": "qwen2.5-coder:7b",
        "validator": "qwen2.5-coder:14b",
    },
    "provider": "ollama",
    "ollama_base_url": "http://localhost:11434",
    "conda_env": "agent_app_env",
    "max_healer_retries": 3,
    "healer_escalate_after": 1,
    "healer_context_budget": 6000,
    "surgeon_min_output_chars": 100,
    "surgeon_patch_retry": True,
    "suite_run_every_n_tasks": 1,
    "context_window": 32768,
    # Per-model num_ctx overrides (Ollama only), matched by substring against
    # the model name; longest match wins. Sized for 16GB VRAM + 32GB RAM:
    # 32B Q4 weights (~19GB) already spill into system RAM, and their KV
    # cache costs ~262KB/token — 24576 tokens keeps the total under ~26GB.
    # 7B/14B models fit on-GPU with their full 32k native window.
    "context_windows": {
        "32b": 24576,
        "14b": 32768,
        "7b": 32768,
    },
    # 0 = no HTTP timeout: requests block until the model finishes, however
    # long that takes. Walk-away mode for full unattended builds.
    "request_timeout": 0,
    "git_autocommit": False,
    "snapshot_files": True,
    "auto_approve_privileged": False,
}


def load_config(root_dir: Path | None = None) -> dict:
    """
    Loads agentic-coder.yaml from root_dir (or the file's parent directory).
    Merges with DEFAULTS so missing keys always have a value.
    Caches result after first load.
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    config = _deep_merge({}, DEFAULTS)

    if root_dir is None:
        root_dir = Path(__file__).parent.parent

    yaml_path = root_dir / "agentic-coder.yaml"
    if yaml_path.exists():
        try:
            import yaml  # PyYAML — installed in conda env
            with open(yaml_path, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}
            # Compat alias: accept legacy 'api_base' in place of 'ollama_base_url'.
            # Strips a trailing /v1 path segment that Ollama does not use for /api/chat.
            if "api_base" in user_config and "ollama_base_url" not in user_config:
                raw = user_config["api_base"].rstrip("/")
                if raw.endswith("/v1"):
                    raw = raw[:-3]
                user_config["ollama_base_url"] = raw
                print(
                    "[CONFIG] Deprecated key 'api_base' detected — "
                    "please rename to 'ollama_base_url' in agentic-coder.yaml."
                )
            config = _deep_merge(config, user_config)
        except ImportError:
            print(
                "[CONFIG] PyYAML not installed — using all defaults. Run: pip install pyyaml"
            )
        except Exception as e:
            print(
                f"[CONFIG] Failed to parse agentic-coder.yaml: {e}. Using defaults."
            )

    _config_cache = config
    return config


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merges override into base. Returns new dict."""
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(
                val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def get_model(tier: str, config: dict | None = None) -> str:
    """Resolves tier name ('architect','surgeon','healer','validator') to model string."""
    if config is None:
        config = load_config()
    return config.get("models",
                      {}).get(tier,
                              DEFAULTS["models"].get(tier, "qwen2.5-coder:7b"))


def get_healer_escalation_model(config: dict | None = None) -> str:
    """
    Returns the escalation model for late-stage healer retries.
    Honors the top-level 'healer_escalation_model' key if set in config.
    Falls back to the surgeon model.
    """
    if config is None:
        config = load_config()
    explicit = config.get("healer_escalation_model")
    if explicit:
        return explicit
    return config.get("models", {}).get("surgeon",
                                        DEFAULTS["models"]["surgeon"])


def resolve_context_window(model: str, config: dict) -> int:
    """
    Resolves num_ctx for one model (Ollama only — num_ctx is an Ollama
    option; other providers ignore it).

    The 'context_windows' config dict maps model-name substrings to token
    counts (e.g. "32b": 24576). The longest matching pattern wins, so a
    specific model name can override a parameter-size pattern. Falls back
    to the flat 'context_window' key when nothing matches.
    """
    overrides = config.get("context_windows")
    if isinstance(overrides, dict):
        model_lower = model.lower()
        best_len, best_value = -1, None
        for pattern, value in overrides.items():
            pattern_str = str(pattern).lower()
            if pattern_str and pattern_str in model_lower:
                if len(pattern_str) > best_len:
                    try:
                        best_len, best_value = len(pattern_str), int(value)
                    except (TypeError, ValueError):
                        continue
        if best_value is not None:
            return best_value
    return config.get("context_window", 32768)


# ==========================================
# PRIMARY LLM QUERY INTERFACE
# ==========================================


def query_llm(
    tier: str,
    system_prompt: str,
    user_prompt: str,
    config: dict | None = None,
    override_model: str | None = None,
) -> str:
    """
    Main entry point for all LLM calls in the pipeline.
    Routes to the correct provider based on config['provider'].

    Args:
        tier: 'architect' | 'surgeon' | 'healer' | 'validator'
        system_prompt: Full system instruction string (already includes steering context)
        user_prompt: Task-specific user message
        config: Optional pre-loaded config dict. Loads from yaml if None.
        override_model: Bypasses tier resolution — used for healer escalation.

    Returns:
        Raw LLM response string with </think> tokens stripped.
    """
    if config is None:
        config = load_config()

    model = override_model if override_model else get_model(tier, config)
    provider = config.get("provider", "ollama")

    _tier_temps = config.get("temperature", {})
    temperature = (_tier_temps.get(tier) if isinstance(_tier_temps, dict)
                   and tier in _tier_temps else
                   (0.0 if "coder" in model else 0.6))

    _tier_tokens = config.get("max_tokens", {})
    num_predict = (_tier_tokens.get(tier) if isinstance(_tier_tokens, dict)
                   and tier in _tier_tokens else 4096)

    options = {
        "temperature": temperature,
        "num_ctx": resolve_context_window(model, config),
        "num_predict": num_predict,
    }

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    print(f"  [{tier.upper()}] → {model}")

    if config.get("debug_prompts", False):
        print(f"  [DEBUG] system (first 500 chars):\n{system_prompt[:500]}")
        print(f"  [DEBUG] user (first 500 chars):\n{user_prompt[:500]}")

    if provider == "ollama":
        return _call_ollama(
            model=model,
            messages=messages,
            options=options,
            base_url=config.get("ollama_base_url", "http://localhost:11434"),
            timeout=config.get("request_timeout", 0),
        )
    elif provider == "openai":
        return _call_openai(model, messages, options, config)
    elif provider == "anthropic":
        return _call_anthropic(model, messages, options, config)
    else:
        print(
            f"[CRITICAL] Unknown provider '{provider}' in config. Supported: ollama, openai, anthropic"
        )
        sys.exit(1)


# ==========================================
# PROVIDER IMPLEMENTATIONS
# ==========================================


def _call_ollama(
    model: str,
    messages: list,
    options: dict,
    base_url: str,
    timeout: int | None,
) -> str:
    """
    Calls local Ollama /api/chat endpoint.
    keep_alive=0 forces immediate VRAM eviction so the next model
    can load without OOM errors on single-GPU setups.
    Strips DeepSeek-R1 <think>...</think> chain-of-thought tokens.

    timeout <= 0 (or None) means no HTTP timeout — the request blocks until
    the model finishes, however long generation takes. The runaway-generation
    guard is num_predict, not the socket timeout.
    """
    url = f"{base_url.rstrip('/')}/api/chat"
    if not timeout or timeout <= 0:
        timeout = None

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": 0,
        "options": options,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )

    # Transient failures (connection refused during a model reload, socket
    # timeout while a large model loads) are retried with backoff before
    # giving up — a single hiccup must not abort a multi-hour unattended run.
    # HTTP errors (e.g. model not found) are immediately fatal.
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                res_data = json.loads(response.read().decode("utf-8"))

                content = res_data.get("message", {}).get("content", "")

                if not content:
                    print(
                        f"[WARN] Empty response from Ollama for model '{model}'.")
                    return ""

                # Strip DeepSeek-R1 chain-of-thought reasoning block
                if "</think>" in content:
                    content = content.split("</think>")[-1].strip()

                return content

        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(
                f"[CRITICAL] Ollama HTTP {e.code} for model '{model}': {body[:300]}"
            )
            sys.exit(1)
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < max_attempts:
                wait = 10 * attempt
                print(f"[LLM] Ollama request failed ({e}) — retrying in "
                      f"{wait}s (attempt {attempt}/{max_attempts})...")
                time.sleep(wait)
                continue
            print(f"[CRITICAL] Cannot reach Ollama at {url} after "
                  f"{max_attempts} attempts.")
            print(f"           Is Ollama running? Run: ollama serve")
            print(f"           Error: {e}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            print(
                f"[CRITICAL] Unexpected Ollama response shape for '{model}': {e}"
            )
            sys.exit(1)
        except Exception as e:
            print(
                f"[CRITICAL] Unexpected error calling Ollama model '{model}': {e}")
            sys.exit(1)


def _call_openai(model: str, messages: list, options: dict,
                 config: dict) -> str:
    """
    OpenAI-compatible provider stub.
    Requires: pip install openai
    Requires config key: openai_api_key
    """
    try:
        from openai import OpenAI
    except ImportError:
        print(
            "[CRITICAL] openai package not installed. Run: pip install openai")
        sys.exit(1)

    api_key = config.get("openai_api_key") or __import__("os").environ.get(
        "OPENAI_API_KEY")
    if not api_key:
        print(
            "[CRITICAL] No OpenAI API key found. Set 'openai_api_key' in agentic-coder.yaml or OPENAI_API_KEY env var."
        )
        sys.exit(1)

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=options.get("num_predict", 4096),
        temperature=options.get("temperature", 0.0),
    )
    return response.choices[0].message.content or ""


def _call_anthropic(model: str, messages: list, options: dict,
                    config: dict) -> str:
    """
    Anthropic provider stub.
    Requires: pip install anthropic
    Requires config key: anthropic_api_key
    """
    try:
        import anthropic
    except ImportError:
        print(
            "[CRITICAL] anthropic package not installed. Run: pip install anthropic"
        )
        sys.exit(1)

    api_key = config.get("anthropic_api_key") or __import__("os").environ.get(
        "ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "[CRITICAL] No Anthropic API key. Set 'anthropic_api_key' in agentic-coder.yaml or ANTHROPIC_API_KEY env var."
        )
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Anthropic requires system prompt as a top-level param, not in messages array
    system_content = next(
        (m["content"] for m in messages if m["role"] == "system"), "")
    user_messages = [m for m in messages if m["role"] != "system"]

    response = client.messages.create(
        model=model,
        max_tokens=options.get("num_predict", 8192),
        system=system_content,
        messages=user_messages,
        temperature=options.get("temperature", 0.0),
    )
    return response.content[0].text if response.content else ""


# ==========================================
# JSON UTILITIES
# ==========================================


def clean_and_parse_json(raw_text: str) -> dict:
    """
    Strips common LLM markdown wrappers from a JSON string and parses it.
    Handles ```json, ``` fences, and leading/trailing whitespace.
    Raises json.JSONDecodeError on parse failure so callers can handle it explicitly.
    """
    cleaned = raw_text.strip()

    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]

    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]

    cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Second attempt: find the first { and last } and extract
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(cleaned[start:end])
        raise


def query_llm_with_json_retry(
    tier: str,
    system_prompt: str,
    user_prompt: str,
    config: dict,
    expected_keys: list[str],
    context_label: str,
    fatal: bool = True,
) -> dict | None:
    """
    Wraps query_llm with a bounded corrective-retry loop for calls that must
    return a JSON object. Implements the hard constraint: retry up to 2 times
    with a corrective reprompt before halting.

    Args:
        tier:           Agent tier string ('architect', 'surgeon', etc.)
        system_prompt:  Full assembled system prompt (steering already injected).
        user_prompt:    Task-specific user message for the initial call.
        config:         Loaded config dict.
        expected_keys:  List of top-level JSON keys the response must contain.
                        Included in the corrective prompt so the model knows
                        exactly what shape is required.
        context_label:  Human-readable label for error messages, e.g. 'Architect plan'
                        or 'SDD documents'.

    Returns:
        Parsed dict on success.
        Calls sys.exit(1) after 2 failed corrective retries — no return on failure.
    """

    response = query_llm(tier, system_prompt, user_prompt, config)
    last_error = ""

    # Hard constraint: retry up to 2 times with a corrective prompt before
    # halting. A response that parses but lacks a required key is just as
    # unusable as unparseable JSON, so both spend the same retry budget.
    for attempt in range(3):  # initial response + two corrective retries
        try:
            parsed = clean_and_parse_json(response)
            missing = [k for k in expected_keys if k not in parsed]
            if not missing:
                return parsed
            last_error = f"missing required key(s): {', '.join(missing)}"
        except json.JSONDecodeError as e:
            last_error = f"not valid JSON: {e}"

        if attempt == 2:
            break

        print(f"[{tier.upper()}] {context_label} response unusable "
              f"({last_error}) — corrective reprompt "
              f"(attempt {attempt + 1}/3)...")
        keys_desc = "\n".join(f"  '{k}': ..." for k in expected_keys)
        corrective_user = (
            f"Your previous response was unusable.\n"
            f"Problem: {last_error}\n"
            f"Your output (first 500 chars):\n{response[:500]}\n\n"
            f"Return ONLY a valid JSON object containing at least these "
            f"{len(expected_keys)} key(s):\n{keys_desc}\n"
            f"No markdown fences. No explanation. No text before or after the JSON."
        )
        response = query_llm(tier, system_prompt, corrective_user, config)

    print(f"[CRITICAL] {context_label} failed after 2 corrective retries: "
          f"{last_error}\n"
          f"Raw (first 500): {response[:500]}")
    if fatal:
        sys.exit(1)
    return None
