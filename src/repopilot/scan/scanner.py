"""Project directory scan and Python project recognition.

Everything here is deterministic and static: files are read as text, nothing from
the repository is imported or executed.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from repopilot.config import Settings
from repopilot.models import ProjectProfile

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".tox",
    ".nox",
    "build",
    "dist",
    "site-packages",
    ".eggs",
    ".idea",
    ".vscode",
}

_REQUIREMENTS_GLOBS = ("requirements*.txt", "requirements/*.txt", "requirements/*.in")

_TOOL_MARKERS: dict[str, tuple[str, ...]] = {
    "ruff": ("ruff", "ruff.toml", ".ruff.toml", "[tool.ruff]"),
    "black": ("black", "[tool.black]"),
    "mypy": ("mypy", "mypy.ini", ".mypy.ini", "[tool.mypy]"),
    "flake8": ("flake8", ".flake8", "[flake8]"),
    "isort": ("isort", ".isort.cfg", "[tool.isort]"),
    "pylint": ("pylint", ".pylintrc", "[tool.pylint]"),
    "bandit": ("bandit", "[tool.bandit]"),
    "pre-commit": ("pre-commit", ".pre-commit-config.yaml"),
    "coverage": ("coverage", ".coveragerc", "[tool.coverage]"),
    "tox": ("tox", "tox.ini"),
    "nox": ("nox", "noxfile.py"),
    "poetry": ("poetry", "[tool.poetry]"),
    "pdm": ("pdm", "pdm.lock"),
    "uv": ("uv", "uv.lock"),
    "hatch": ("hatch", "[tool.hatch]"),
    "pip-audit": ("pip-audit",),
}

_CONFIG_FILE_NAMES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "pdm.lock",
    "uv.lock",
    "tox.ini",
    "noxfile.py",
    "pytest.ini",
    "conftest.py",
    "Makefile",
    "Dockerfile",
    "mypy.ini",
    ".flake8",
    "ruff.toml",
    ".ruff.toml",
    ".pre-commit-config.yaml",
    ".coveragerc",
    ".python-version",
    "runtime.txt",
    "environment.yml",
)


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def iter_files(root: Path, *, max_files: int) -> list[Path]:
    """Walk ``root`` skipping virtualenvs, VCS metadata and build output."""

    files: list[Path] = []
    for path in root.rglob("*"):
        if len(files) >= max_files:
            break
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def _load_pyproject(repo_dir: Path) -> dict:
    path = repo_dir / "pyproject.toml"
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(_read_text(path))
    except tomllib.TOMLDecodeError:
        return {}


def _dependency_texts(repo_dir: Path, pyproject: dict, requirements_files: list[str]) -> list[str]:
    texts = [str(pyproject)]
    for name in ("setup.cfg", "Pipfile", "tox.ini"):
        candidate = repo_dir / name
        if candidate.is_file():
            texts.append(_read_text(candidate))
    for relative in requirements_files:
        texts.append(_read_text(repo_dir / relative))
    return texts


def _detect_python_requires(repo_dir: Path, pyproject: dict) -> tuple[str | None, list[str]]:
    version_files: list[str] = []
    project = pyproject.get("project")
    if isinstance(project, dict):
        requires = project.get("requires-python")
        if isinstance(requires, str) and requires.strip():
            return requires.strip(), version_files
    poetry = pyproject.get("tool", {}).get("poetry") if isinstance(pyproject, dict) else None
    if isinstance(poetry, dict):
        python = poetry.get("dependencies", {}).get("python")
        if isinstance(python, str) and python.strip():
            return python.strip(), version_files

    for name in (".python-version", "runtime.txt"):
        candidate = repo_dir / name
        if candidate.is_file():
            version_files.append(name)
            first = _read_text(candidate).strip().splitlines()
            if first and first[0].strip():
                return first[0].strip(), version_files

    setup_cfg = repo_dir / "setup.cfg"
    if setup_cfg.is_file():
        for line in _read_text(setup_cfg).splitlines():
            if line.strip().lower().startswith("python_requires"):
                _, _, value = line.partition("=")
                if value.strip():
                    return value.strip(), version_files
    return None, version_files


def scan_project(repo_dir: Path, settings: Settings) -> ProjectProfile:
    """Scan ``repo_dir`` and describe the project."""

    repo_dir = repo_dir.resolve()
    files = iter_files(repo_dir, max_files=settings.max_scanned_files)
    relative_files = [path.relative_to(repo_dir).as_posix() for path in files]
    pyproject = _load_pyproject(repo_dir)

    requirements_files = sorted(
        {
            relative
            for relative in relative_files
            for pattern in _REQUIREMENTS_GLOBS
            if Path(relative).match(pattern)
        }
    )
    dependency_manifests: list[str] = []
    if "pyproject.toml" in relative_files:
        dependency_manifests.append("pyproject.toml")
    if "setup.py" in relative_files:
        dependency_manifests.append("setup.py")
    if "setup.cfg" in relative_files:
        dependency_manifests.append("setup.cfg")
    if "Pipfile" in relative_files:
        dependency_manifests.append("Pipfile")
    dependency_manifests.extend(requirements_files)

    config_files = sorted(name for name in _CONFIG_FILE_NAMES if name in relative_files)
    workflow_files = [name for name in relative_files if name.startswith(".github/workflows/")]

    python_files = [name for name in relative_files if name.endswith(".py")]
    is_python_project = bool(
        python_files or dependency_manifests or "pyproject.toml" in relative_files
    )

    dep_texts = _dependency_texts(repo_dir, pyproject, requirements_files)
    dev_tools: list[str] = []
    for tool, markers in _TOOL_MARKERS.items():
        if any(any(marker in text for marker in markers) for text in dep_texts):
            dev_tools.append(tool)
    dev_tools.extend(
        name
        for name in ("pre-commit", "tox", "nox")
        if name in config_files and name not in dev_tools
    )
    if any(name.startswith(".github/workflows/") for name in relative_files):
        dev_tools.append("github-actions")

    test_files = [
        name
        for name in relative_files
        if Path(name).name.startswith("test_") and name.endswith(".py")
    ]
    conftest_files = [name for name in relative_files if Path(name).name == "conftest.py"]
    test_dirs = sorted({str(Path(name).parent.as_posix()) for name in test_files})

    pyproject_text = str(pyproject)
    test_frameworks: list[str] = []
    if (
        test_files
        or conftest_files
        or "pytest.ini" in relative_files
        or "[tool.pytest.ini_options]" in pyproject_text
        or "pytest" in " ".join(dep_texts)
    ):
        test_frameworks.append("pytest")
    if any("unittest" in _read_text(repo_dir / name) for name in test_files):
        test_frameworks.append("unittest")

    suggested_test_command: list[str] | None = None
    if "pytest" in test_frameworks:
        targets = [directory for directory in test_dirs if directory not in (".", "")]
        suggested_test_command = [settings.python_executable, "-m", "pytest", *targets]
    elif "unittest" in test_frameworks:
        suggested_test_command = [settings.python_executable, "-m", "unittest", "discover"]

    python_requires, version_files = _detect_python_requires(repo_dir, pyproject)
    detectable_markers = sorted(set(config_files) | set(workflow_files))

    return ProjectProfile(
        is_python_project=is_python_project,
        primary_language="python" if is_python_project else "unknown",
        python_requires=python_requires,
        python_version_files=version_files,
        dependency_manifests=dependency_manifests,
        requirements_files=requirements_files,
        test_frameworks=test_frameworks,
        test_dirs=test_dirs,
        test_file_count=len(test_files),
        suggested_test_command=suggested_test_command,
        dev_tools=sorted(set(dev_tools)),
        detectable_markers=detectable_markers,
        config_files=config_files,
    )
