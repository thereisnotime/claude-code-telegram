# Default recipe: list all available commands
default:
    @just --list

# Install production dependencies
install:
    poetry install --no-dev

# Install all dependencies (including dev) and set up pre-commit hooks
dev:
    poetry install
    poetry run pre-commit install --install-hooks || echo "pre-commit not configured yet"

# Run the bot
run *args='':
    poetry run claude-telegram-bot {{ args }}

# Run the bot with additional .env files layered on top (e.g. just run-env .env.local .env.secrets)
run-env +files:
    poetry run claude-telegram-bot {{ prepend('--env-file ', files) }}

# Run the bot with auto-restart on src/ changes
run-watch:
    poetry run watchfiles "claude-telegram-bot" src/

# Run the bot with debug logging
run-debug:
    poetry run claude-telegram-bot --debug

# Run tests with coverage
test *args='':
    poetry run pytest {{ args }}

# Run a single test file or match pattern
test-one file *args='':
    poetry run pytest {{ file }} -v {{ args }}

# Run tests matching a keyword expression
test-k pattern *args='':
    poetry run pytest -k "{{ pattern }}" -v {{ args }}

# Run all linting checks (black, isort, flake8, mypy)
lint:
    poetry run black --check src tests
    poetry run isort --check-only src tests
    poetry run flake8 src tests
    poetry run mypy src

# Run lint checks without mypy (faster)
lint-fast:
    poetry run black --check src tests
    poetry run isort --check-only src tests
    poetry run flake8 src tests

# Auto-format code with black + isort
format:
    poetry run black src tests
    poetry run isort src tests

# Format and then lint (fix then verify)
fix: format lint-fast

# Type-check only
typecheck:
    poetry run mypy src

# Clean up generated files
clean:
    find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
    find . -type f -name "*.pyc" -delete 2>/dev/null || true
    find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
    rm -rf .coverage htmlcov/ .pytest_cache/ dist/ build/

# Show current version
version:
    @poetry version -s

# Bump patch version (1.2.0 -> 1.2.1), commit, and tag
[confirm("This will bump, commit, tag, and push. Continue?")]
bump-patch:
    #!/usr/bin/env bash
    set -euo pipefail
    poetry version patch
    new_version=$(poetry version -s)
    git add pyproject.toml
    git commit -m "release: v${new_version}"
    git tag "v${new_version}"
    git push && git push origin "v${new_version}"
    echo "Released v${new_version}. Tag pushed — release workflow will run on GitHub."

# Bump minor version (1.2.0 -> 1.3.0), commit, and tag
[confirm("This will bump, commit, tag, and push. Continue?")]
bump-minor:
    #!/usr/bin/env bash
    set -euo pipefail
    poetry version minor
    new_version=$(poetry version -s)
    git add pyproject.toml
    git commit -m "release: v${new_version}"
    git tag "v${new_version}"
    git push && git push origin "v${new_version}"
    echo "Released v${new_version}. Tag pushed — release workflow will run on GitHub."

# Bump major version (1.2.0 -> 2.0.0), commit, and tag
[confirm("This will bump, commit, tag, and push. Continue?")]
bump-major:
    #!/usr/bin/env bash
    set -euo pipefail
    poetry version major
    new_version=$(poetry version -s)
    git add pyproject.toml
    git commit -m "release: v${new_version}"
    git tag "v${new_version}"
    git push && git push origin "v${new_version}"
    echo "Released v${new_version}. Tag pushed — release workflow will run on GitHub."

# Push current version tag to trigger release workflow
[confirm("This will push the current tag. Continue?")]
release:
    #!/usr/bin/env bash
    set -euo pipefail
    current_version=$(poetry version -s)
    git push && git push origin "v${current_version}"
    echo "Pushed v${current_version}. Release workflow will run on GitHub."

# Start bot on remote Mac in tmux (persists after SSH disconnect)
run-remote:
    security unlock-keychain ~/Library/Keychains/login.keychain-db
    tmux new-session -d -s claude-bot 'poetry run claude-telegram-bot'
    @echo "Bot started in tmux session 'claude-bot'"
    @echo "  Attach: just remote-attach"
    @echo "  Stop:   just remote-stop"

# Attach to running bot tmux session
remote-attach:
    tmux attach -t claude-bot

# Stop the bot tmux session
remote-stop:
    tmux kill-session -t claude-bot
