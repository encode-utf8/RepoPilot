"""Runtime configuration.

Every setting has a safe default and can be overridden through a ``REPOPILOT_*``
environment variable, so deployments never need code changes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

#: Environment variables copied into subprocesses. Everything else, most notably
#: credentials such as ``GITHUB_TOKEN`` or ``AWS_SECRET_ACCESS_KEY``, is dropped.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "SystemDrive",
    "windir",
    "ComSpec",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
)


def default_workspace_root() -> Path:
    """Workspace root kept outside the control repository by default."""

    return Path(tempfile.gettempdir()) / "repopilot" / "workspaces"


def default_log_dir() -> Path:
    return Path(tempfile.gettempdir()) / "repopilot" / "logs"


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_paths(name: str, default: tuple[Path, ...]) -> tuple[Path, ...]:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return tuple(Path(part).expanduser() for part in raw.split(os.pathsep) if part.strip())


@dataclass(frozen=True)
class Settings:
    """Immutable runtime configuration."""

    workspace_root: Path = field(default_factory=default_workspace_root)
    log_dir: Path = field(default_factory=default_log_dir)

    command_timeout_seconds: float = 300.0
    max_command_timeout_seconds: float = 900.0
    clone_timeout_seconds: float = 180.0
    dependency_audit_timeout_seconds: float = 180.0
    max_output_bytes: int = 2_000_000
    max_evidence_chars: int = 2_000
    max_scanned_files: int = 5_000

    python_executable: str = sys.executable
    git_executable: str = "git"
    ruff_command: tuple[str, ...] = (sys.executable, "-m", "ruff")
    pytest_command: tuple[str, ...] = (sys.executable, "-m", "pytest")
    pip_audit_command: tuple[str, ...] = (sys.executable, "-m", "pip_audit")

    enable_ruff: bool = True
    enable_pytest: bool = True
    enable_dependency_audit: bool = True
    enable_heuristics: bool = True

    require_dependency_audit: bool = False
    require_pytest_for_detected_tests: bool = True

    #: Repository code is only executed for controlled fixtures unless the operator
    #: explicitly opts in through configuration.
    allow_untrusted_code_execution: bool = False
    trusted_source_roots: tuple[Path, ...] = ()

    keep_workspace: bool = True
    scheduler_max_workers: int = 4
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST

    # --- Phase 2: model client (OpenAI compatible, points at vLLM) ---

    #: ``stub`` runs the whole agent offline; ``openai`` targets any
    #: OpenAI compatible endpoint (vLLM serves one at ``/v1``).
    model_provider: str = "stub"
    model_base_url: str = "http://127.0.0.1:8000/v1"
    model_name: str = "local-model"
    #: Name of the environment variable holding the API key. The value is read
    #: lazily at request time and never stored on settings, logs or reports.
    model_api_key_env: str = "REPOPILOT_MODEL_API_KEY"
    model_timeout_seconds: float = 60.0
    model_health_timeout_seconds: float = 10.0
    model_max_tokens: int = 1024
    model_temperature: float = 0.0
    model_max_response_chars: int = 20_000
    model_max_prompt_chars: int = 60_000
    model_prompt_cost_per_1k_tokens: float = 0.0
    model_completion_cost_per_1k_tokens: float = 0.0

    # --- Phase 2: finite repair loop limits ---

    agent_max_rounds: int = 6
    agent_max_wall_seconds: float = 900.0
    agent_max_model_calls: int = 12
    agent_max_model_tokens: int = 60_000
    agent_max_tool_calls: int = 40
    agent_max_tool_output_bytes: int = 200_000
    agent_max_diff_lines: int = 400
    agent_max_changed_files: int = 10
    agent_max_parse_retries: int = 1
    agent_max_context_files: int = 6
    agent_max_search_results: int = 40
    agent_max_file_bytes: int = 200_000


def load_settings(**overrides: object) -> Settings:
    """Build :class:`Settings` from the environment, then apply explicit overrides."""

    env = os.environ
    settings = Settings(
        workspace_root=Path(env.get("REPOPILOT_WORKSPACE_ROOT", str(default_workspace_root()))),
        log_dir=Path(env.get("REPOPILOT_LOG_DIR", str(default_log_dir()))),
        command_timeout_seconds=_env_float("REPOPILOT_COMMAND_TIMEOUT_SECONDS", 300.0),
        max_command_timeout_seconds=_env_float("REPOPILOT_MAX_COMMAND_TIMEOUT_SECONDS", 900.0),
        clone_timeout_seconds=_env_float("REPOPILOT_CLONE_TIMEOUT_SECONDS", 180.0),
        dependency_audit_timeout_seconds=_env_float(
            "REPOPILOT_DEPENDENCY_AUDIT_TIMEOUT_SECONDS", 180.0
        ),
        max_output_bytes=_env_int("REPOPILOT_MAX_OUTPUT_BYTES", 2_000_000),
        max_evidence_chars=_env_int("REPOPILOT_MAX_EVIDENCE_CHARS", 2_000),
        max_scanned_files=_env_int("REPOPILOT_MAX_SCANNED_FILES", 5_000),
        enable_ruff=_env_flag("REPOPILOT_ENABLE_RUFF", True),
        enable_pytest=_env_flag("REPOPILOT_ENABLE_PYTEST", True),
        enable_dependency_audit=_env_flag("REPOPILOT_ENABLE_DEPENDENCY_AUDIT", True),
        enable_heuristics=_env_flag("REPOPILOT_ENABLE_HEURISTICS", True),
        require_dependency_audit=_env_flag("REPOPILOT_REQUIRE_DEPENDENCY_AUDIT", False),
        require_pytest_for_detected_tests=_env_flag(
            "REPOPILOT_REQUIRE_PYTEST_FOR_DETECTED_TESTS", True
        ),
        allow_untrusted_code_execution=_env_flag("REPOPILOT_ALLOW_UNTRUSTED_CODE_EXECUTION", False),
        trusted_source_roots=_env_paths("REPOPILOT_TRUSTED_SOURCE_ROOTS", ()),
        keep_workspace=_env_flag("REPOPILOT_KEEP_WORKSPACE", True),
        scheduler_max_workers=_env_int("REPOPILOT_SCHEDULER_MAX_WORKERS", 4),
        model_provider=env.get("REPOPILOT_MODEL_PROVIDER", "stub"),
        model_base_url=env.get("REPOPILOT_MODEL_BASE_URL", "http://127.0.0.1:8000/v1"),
        model_name=env.get("REPOPILOT_MODEL_NAME", "local-model"),
        model_api_key_env=env.get("REPOPILOT_MODEL_API_KEY_ENV", "REPOPILOT_MODEL_API_KEY"),
        model_timeout_seconds=_env_float("REPOPILOT_MODEL_TIMEOUT_SECONDS", 60.0),
        model_health_timeout_seconds=_env_float("REPOPILOT_MODEL_HEALTH_TIMEOUT_SECONDS", 10.0),
        model_max_tokens=_env_int("REPOPILOT_MODEL_MAX_TOKENS", 1024),
        model_temperature=_env_float("REPOPILOT_MODEL_TEMPERATURE", 0.0),
        model_max_response_chars=_env_int("REPOPILOT_MODEL_MAX_RESPONSE_CHARS", 20_000),
        model_max_prompt_chars=_env_int("REPOPILOT_MODEL_MAX_PROMPT_CHARS", 60_000),
        model_prompt_cost_per_1k_tokens=_env_float(
            "REPOPILOT_MODEL_PROMPT_COST_PER_1K_TOKENS", 0.0
        ),
        model_completion_cost_per_1k_tokens=_env_float(
            "REPOPILOT_MODEL_COMPLETION_COST_PER_1K_TOKENS", 0.0
        ),
        agent_max_rounds=_env_int("REPOPILOT_AGENT_MAX_ROUNDS", 6),
        agent_max_wall_seconds=_env_float("REPOPILOT_AGENT_MAX_WALL_SECONDS", 900.0),
        agent_max_model_calls=_env_int("REPOPILOT_AGENT_MAX_MODEL_CALLS", 12),
        agent_max_model_tokens=_env_int("REPOPILOT_AGENT_MAX_MODEL_TOKENS", 60_000),
        agent_max_tool_calls=_env_int("REPOPILOT_AGENT_MAX_TOOL_CALLS", 40),
        agent_max_tool_output_bytes=_env_int("REPOPILOT_AGENT_MAX_TOOL_OUTPUT_BYTES", 200_000),
        agent_max_diff_lines=_env_int("REPOPILOT_AGENT_MAX_DIFF_LINES", 400),
        agent_max_changed_files=_env_int("REPOPILOT_AGENT_MAX_CHANGED_FILES", 10),
        agent_max_parse_retries=_env_int("REPOPILOT_AGENT_MAX_PARSE_RETRIES", 1),
        agent_max_context_files=_env_int("REPOPILOT_AGENT_MAX_CONTEXT_FILES", 6),
        agent_max_search_results=_env_int("REPOPILOT_AGENT_MAX_SEARCH_RESULTS", 40),
        agent_max_file_bytes=_env_int("REPOPILOT_AGENT_MAX_FILE_BYTES", 200_000),
    )
    if not overrides:
        return settings
    return replace(settings, **overrides)  # type: ignore[arg-type]
