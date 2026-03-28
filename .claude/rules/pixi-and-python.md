# Pixi and Python Standards

## Package Management
- ALWAYS use pixi for package management. NEVER use pip, conda, or poetry directly.
- All dependencies go in pyproject.toml — runtime deps in [project.dependencies], dev deps in [dependency-groups].
- Run commands via `pixi run <task>` or `pixi run -e dev <task>`, never with bare `python` or `pytest`.
- All pytest configuration belongs in pyproject.toml under [tool.pytest.ini_options]. No pytest.ini files.

## Python Standards
- Use type hints for all function signatures and class attributes.
- Use pathlib.Path for filesystem paths, never string concatenation.
- Use f-strings for string formatting.
- Constants in UPPER_SNAKE_CASE.
- Keep files under 500 lines — refactor into submodules if larger.
- No silent exception handling — catch specific exceptions, let unexpected ones propagate.
- No silent fallbacks — fail loudly when something is wrong.
