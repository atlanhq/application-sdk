# 🛠️ Consolidate Setup Process to Two Commands

## 📝 Summary
Consolidate the entire setup process into just two commands: `uv setup` (handles all dependencies and configurations) and `uv run` (starts the application), ensuring all commands use consistent SDK versions.

## 💡 Basic Example
```bash
# Current: Multiple complex setup steps
pip install -r requirements.txt
dapr init
temporal server start-dev
python setup.py install
# ... many more steps

# Proposed: Simplified two-command setup
uv setup    # Handles ALL dependencies and configurations
uv run      # Starts the application
```

## 🎯 Motivation
- **Developer Experience**: Drastically simplify onboarding for new developers
- **Consistency**: Ensure all commands use consistent SDK versions
- **Reliability**: Reduce setup errors and configuration drift
- **Automation**: Automate complex setup procedures
- **Documentation**: Simplify setup documentation and guides

## 💼 Acceptance Criteria
- [ ] Design unified setup command that handles:
  - [ ] Python dependencies installation
  - [ ] Dapr initialization and configuration
  - [ ] Temporal setup (if needed)
  - [ ] Environment configuration
  - [ ] SDK version consistency checks
- [ ] Create unified run command that:
  - [ ] Starts all required services
  - [ ] Validates configuration
  - [ ] Provides clear status feedback
- [ ] Ensure version consistency across all components
- [ ] Update all documentation to use new setup process
- [ ] Create migration guide for existing setups
- [ ] Add troubleshooting guide for common setup issues
- [ ] Test setup process across different platforms (Windows, Mac, Linux)

## 🔧 Technical Implementation
```bash
# uv setup should handle:
- UV package manager setup
- Python dependencies (uv sync --all-extras --all-groups)
- Dapr runtime initialization
- Temporal server setup (if local development)
- Configuration file generation
- Version compatibility checks

# uv run should handle:
- Service startup orchestration
- Health checks
- Environment validation
- Clear status reporting
```

## 🏷️ Labels
- `enhancement`
- `developer-experience`
- `setup`
- `v0.2.0`

## 🔗 Reference
Related to Application SDK v0.2.0 release planning discussion