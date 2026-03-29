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

# Download a whisper.cpp GGML model (e.g. just setup-whisper-model small)
setup-whisper-model model='base':
    #!/usr/bin/env bash
    set -euo pipefail
    # Strip "model=" prefix if someone passes model=base instead of just base
    model_name="{{ model }}"
    model_name="${model_name#model=}"
    model_dir="$HOME/.cache/whisper-cpp"
    model_file="${model_dir}/ggml-${model_name}.bin"
    url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-${model_name}.bin"
    mkdir -p "$model_dir"
    if [ -f "$model_file" ]; then
        echo "Model already exists: $model_file"
        echo "Delete it first if you want to re-download."
        exit 0
    fi
    echo "Downloading ggml-${model_name}.bin ..."
    curl -L -o "$model_file" "$url"
    echo "Saved to $model_file"

# Build and install whisper.cpp from source (e.g. just setup-whisper-build cuda)
setup-whisper-build gpu='none':
    #!/usr/bin/env bash
    set -euo pipefail
    # Strip "gpu=" prefix if someone passes gpu=none instead of just none
    gpu_opt="{{ gpu }}"
    gpu_opt="${gpu_opt#gpu=}"
    build_dir="$(mktemp -d)"
    echo "Cloning whisper.cpp into $build_dir ..."
    git clone --depth 1 https://github.com/ggerganov/whisper.cpp.git "$build_dir/whisper.cpp"
    cd "$build_dir/whisper.cpp"
    cmake_flags=""
    case "$gpu_opt" in
        cuda)  cmake_flags="-DWHISPER_CUBLAS=ON" ;;
        metal) cmake_flags="-DWHISPER_METAL=ON" ;;
        none)  ;;
        *)     echo "Unknown gpu option '$gpu_opt'. Use: none, cuda, metal"; exit 1 ;;
    esac
    echo "Building with cmake ${cmake_flags:-"(CPU only)"} ..."
    cmake -B build $cmake_flags
    cmake --build build --config Release
    binary="build/bin/whisper-cli"
    if [ ! -f "$binary" ]; then
        binary="build/bin/main"
    fi
    echo ""
    echo "Build complete: $build_dir/whisper.cpp/$binary"
    echo ""
    echo "To install system-wide:"
    echo "  sudo cp $build_dir/whisper.cpp/$binary /usr/local/bin/whisper-cpp"
    echo ""
    echo "Or set in .env:"
    echo "  WHISPER_CPP_BINARY_PATH=$build_dir/whisper.cpp/$binary"

# Full local whisper.cpp setup: install ffmpeg, build binary, download model (e.g. just setup-whisper small cuda)
setup-whisper model='base' gpu='none':
    #!/usr/bin/env bash
    set -euo pipefail
    model_name="{{ model }}"
    model_name="${model_name#model=}"
    echo "=== Step 1: Check ffmpeg ==="
    if command -v ffmpeg &>/dev/null; then
        echo "ffmpeg is already installed: $(which ffmpeg)"
    else
        echo "ffmpeg not found. Install it:"
        echo "  Ubuntu/Debian: sudo apt install -y ffmpeg"
        echo "  macOS:         brew install ffmpeg"
        echo "  Alpine:        apk add ffmpeg"
        exit 1
    fi
    echo ""
    echo "=== Step 2: Build whisper.cpp ==="
    just setup-whisper-build {{ gpu }}
    echo ""
    echo "=== Step 3: Download model ==="
    just setup-whisper-model "$model_name"
    echo ""
    echo "=== Done ==="
    echo "Add to your .env:"
    echo "  VOICE_PROVIDER=local"
    echo "  WHISPER_CPP_MODEL_PATH=$model_name"

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
