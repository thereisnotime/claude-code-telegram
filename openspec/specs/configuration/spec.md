# Configuration

## Purpose
Pydantic Settings v2 loads config from environment variables with layered .env file support. Feature flags control optional capabilities.

## Requirements

### Requirement: Layered .env loading
The system SHALL load configuration from layered .env files with override semantics. Base .env loaded first. Additional --env-file arguments layered with override=True (later values win). systemd EnvironmentFile sets process-level vars.

#### Scenario: Home .env overrides repo .env
- **WHEN** the repo .env sets ENABLE_PROJECT_THREADS=false and ~/.env sets it to true
- **THEN** the ~/.env value (true) wins because it is loaded with override=True

### Requirement: Required core settings
The system MUST require core settings to start. TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME, and APPROVED_DIRECTORY are required. Missing values cause a ConfigurationError at startup.

#### Scenario: Bot starts without TELEGRAM_BOT_TOKEN
- **WHEN** TELEGRAM_BOT_TOKEN is not set
- **THEN** the bot fails to start with a clear configuration error

### Requirement: Feature flags
The system SHALL support feature flags for optional capabilities. FeatureFlags in src/config/features.py controls: MCP, git, file uploads, quick actions, session export, image uploads, voice, conversation mode, agentic mode, API server, scheduler. Each maps to an ENABLE_* env var.

#### Scenario: Feature flag disabled
- **WHEN** ENABLE_GIT_INTEGRATION=false
- **THEN** git-related handlers are not registered and /git commands are unavailable

### Requirement: YAML project definitions
The system SHALL load project definitions from YAML. PROJECTS_CONFIG_PATH points to YAML defining projects (slug, name, directory, enabled). Notification-only projects have no directory. Example at config/projects.example.yaml.

#### Scenario: Project config loaded
- **WHEN** the bot starts with ENABLE_PROJECT_THREADS=true and a valid PROJECTS_CONFIG_PATH
- **THEN** the registry is populated with enabled projects and their resolved paths

### Requirement: Security relaxation flags
The system SHALL expose security relaxation flags for trusted environments. DISABLE_SECURITY_PATTERNS=false (default) relaxes input validation. DISABLE_TOOL_VALIDATION=false (default) bypasses tool allowlist. Both documented as trusted-only.

#### Scenario: Security patterns disabled
- **WHEN** DISABLE_SECURITY_PATTERNS=true
- **THEN** shell metacharacters in user input are allowed through to Claude
