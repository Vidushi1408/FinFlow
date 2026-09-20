"""
The Docker image installs only requirements.txt, while CI's test job also installs requirements-dev.txt -- so a
package that is used at runtime but declared only for development passes the tests and then crashes the container
(this happened with Faker). This test compares what the application code imports with what requirements.txt declares.
"""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# import name -> the distribution that provides it, where the two differ
DISTRIBUTION = {
    "sklearn": "scikit-learn", "dotenv": "python-dotenv", "psycopg2": "psycopg2-binary", "faker": "faker",
    "wtforms": "flask-wtf", "werkzeug": "flask", "jinja2": "flask", "yaml": "pyyaml",
}
# Developer-only tools that are deliberately not in the runtime image (excluded by .dockerignore too)
NOT_RUNTIME = {"generate_ppt.py"}
APP_DIRS = ["webapp", "etl", "ml", "data_generator", "database", "scripts"]
APP_FILES = ["etl_pipeline.py", "train_models.py", "logging_config.py"]


def _normalise(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared(path):
    names = set()
    for line in (ROOT / path).read_text().splitlines():
        line = line.strip()
        if line and not line.startswith(("#", "-")):
            names.add(_normalise(re.split(r"[<>=!~\[ ;]", line)[0]))
    return names


def _third_party_imports():
    local = {p.name.removesuffix(".py") for p in ROOT.iterdir() if not p.name.startswith((".", "venv"))}
    found = {}
    files = [f for d in APP_DIRS for f in (ROOT / d).rglob("*.py")] + [ROOT / f for f in APP_FILES]
    for path in files:
        if path.name in NOT_RUNTIME:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names = [n.name.split(".")[0] for n in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name not in sys.stdlib_module_names and name not in local:
                    found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_package_the_app_imports_is_declared_in_requirements_txt():
    runtime = _declared("requirements.txt")
    missing = {
        module: sorted(files)
        for module, files in _third_party_imports().items()
        if _normalise(DISTRIBUTION.get(module, module)) not in runtime
    }
    assert not missing, (
        "Imported by application code but missing from requirements.txt (the Docker image only installs that file):\n"
        + "\n".join(f"  {m}  <- {', '.join(f)}" for m, f in sorted(missing.items()))
    )


def test_runtime_requirements_are_not_hidden_in_the_dev_file():
    hidden = _declared("requirements-dev.txt") & {_normalise(DISTRIBUTION.get(m, m)) for m in _third_party_imports()}
    assert not hidden, f"used by the app but only declared in requirements-dev.txt: {sorted(hidden)}"
