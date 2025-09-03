# Application SDK v0.2.0 GitHub Issues

This directory contains GitHub issue templates for the Application SDK v0.2.0 release, based on the requirements discussed in the Slack thread.

## 📋 Issue Summary

The following issues have been created for the v0.2.0 release:

### 🔒 Security & Infrastructure
1. **Docker Base Image Security** (`docker-base-image-security.md`)
   - Improve security of Docker base images
   - Reduce attack surface with minimal base images

2. **Centralized Docker Configuration** (`docker-configs-versioning.md`)
   - Provide versioned Docker configurations from SDK
   - Eliminate need for each app to maintain its own Docker setup

### ⚡ Performance & Optimization  
3. **Dapr File Size Optimization** (`dapr-file-size-optimization.md`)
   - Reduce 100MB Dapr distribution size
   - Improve deployment times and resource usage

4. **Process & Thread Management** (`process-thread-management.md`)
   - Fix ineffective uvloop implementation
   - Eliminate idle/stale threads and improve scaling

5. **Memory Leak Verification** (`memory-leak-verification.md`)
   - Add flamegraph-based memory monitoring
   - Implement decorator for easy profiling

### 🔧 Developer Experience
6. **Setup Process Consolidation** (`setup-process-consolidation.md`)
   - Consolidate setup to two commands: `uv setup` and `uv run`
   - Ensure consistent SDK versions

7. **Log Levels Standardization** (`log-levels-standardization.md`)
   - Fix incorrect log levels across modules
   - Address object store retry logging issues

8. **Type Safety Improvements** (`dict-type-safety-replacement.md`)
   - Replace `Dict[str, Any]` with proper typed models
   - Improve IDE support and eliminate runtime errors

### 🚀 New Features
9. **AWS & Azure Authentication** (`aws-azure-authentication.md`)
   - Add cloud provider authentication support
   - Extensible design for future GCP integration

10. **Multi-Database Extraction** (`multi-database-extraction.md`)
    - Support extracting from multiple databases in single workflow
    - Improve efficiency for enterprise use cases

11. **UI SDK Exploration** (`ui-sdk-exploration.md`)
    - Explore UI SDK development for complete application platform
    - Enable developers to build full applications

### 📊 Monitoring & Observability
12. **Dapr Bindings Observability** (`dapr-bindings-observability.md`)
    - Improve visibility into Dapr bindings status
    - Inspired by Cloudflare's worker SDK approach

13. **State Management for Multiple Workers** (`state-management-multiple-workers.md`)
    - Improve state management with multiple workers
    - Add clear coordination patterns

14. **Public API Documentation** (`public-api-documentation.md`)
    - Host Python reference docs publicly
    - Similar to python.temporal.io

## 🚀 Creating the Issues

### Option 1: Using the Python Script (Recommended)
```bash
# Make sure you have GitHub CLI installed and authenticated
gh auth login

# Run the creation script
cd .github/issues
python create-v0.2.0-issues.py
```

### Option 2: Manual Creation
You can manually create issues by copying the content from each markdown file into GitHub's issue creation interface.

### Option 3: Using GitHub CLI Directly
```bash
# Example for creating a single issue
gh issue create --title "Docker Base Image Security Improvements" --body-file docker-base-image-security.md --label "v0.2.0,enhancement,security"
```

## 📊 Milestone Planning

All issues should be added to the **v0.2.0** milestone for tracking and planning purposes.

## 🏷️ Labels Used

- `v0.2.0` - Release milestone
- `enhancement` - New features and improvements  
- `bug` - Issues that need fixing
- `security` - Security-related improvements
- `performance` - Performance optimizations
- `developer-experience` - DX improvements
- `breaking-change` - Changes that may break existing code

## 📅 Next Steps

1. Create the issues using one of the methods above
2. Create a v0.2.0 milestone in GitHub
3. Assign issues to the milestone
4. Prioritize issues based on impact and effort
5. Add due dates and assignees as needed
6. Begin development work on prioritized items

## 🔗 References

- Original Slack thread discussion
- Application SDK repository: https://github.com/atlanhq/application-sdk
- Sample apps: https://github.com/atlanhq/atlan-sample-apps