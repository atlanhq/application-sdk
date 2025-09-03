#!/usr/bin/env python3
"""
Script to create GitHub issues for Application SDK v0.2.0 release.
This script reads the markdown issue templates and creates GitHub issues using the GitHub API.

Usage:
    python create-v0.2.0-issues.py

Prerequisites:
    - GitHub CLI (gh) installed and authenticated
    - Or GitHub token set in GITHUB_TOKEN environment variable
"""

import os
import subprocess
import sys
from pathlib import Path

def run_gh_command(cmd):
    """Run a GitHub CLI command and return the result."""
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, check=True)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        print(f"Error running command: {cmd}")
        print(f"Error: {e.stderr}")
        return None

def create_issue_from_markdown(md_file):
    """Create a GitHub issue from a markdown file."""
    if not md_file.exists():
        print(f"File not found: {md_file}")
        return False
        
    content = md_file.read_text()
    
    # Extract title from first line (remove # and strip)
    lines = content.split('\n')
    title = lines[0].replace('# ', '').strip()
    
    # Use the entire content as the issue body
    body = content
    
    # Create the issue
    cmd = f'gh issue create --title "{title}" --body-file "{md_file}" --label "v0.2.0,enhancement"'
    result = run_gh_command(cmd)
    
    if result:
        print(f"✅ Created issue: {title}")
        return True
    else:
        print(f"❌ Failed to create issue: {title}")
        return False

def main():
    """Main function to create all v0.2.0 issues."""
    issues_dir = Path(__file__).parent
    
    # Check if gh CLI is available
    if run_gh_command("gh --version") is None:
        print("❌ GitHub CLI (gh) is not installed or not authenticated.")
        print("Please install GitHub CLI and authenticate with: gh auth login")
        sys.exit(1)
    
    print("🚀 Creating GitHub issues for Application SDK v0.2.0...")
    
    # List of issue files to create
    issue_files = [
        "docker-base-image-security.md",
        "dapr-file-size-optimization.md", 
        "docker-configs-versioning.md",
        "aws-azure-authentication.md",
        "multi-database-extraction.md",
        "memory-leak-verification.md",
        "ui-sdk-exploration.md",
        "process-thread-management.md",
        "log-levels-standardization.md",
        "setup-process-consolidation.md",
        "state-management-multiple-workers.md",
        "dict-type-safety-replacement.md",
        "dapr-bindings-observability.md",
        "public-api-documentation.md"
    ]
    
    created_count = 0
    total_count = len(issue_files)
    
    for issue_file in issue_files:
        md_file = issues_dir / issue_file
        if create_issue_from_markdown(md_file):
            created_count += 1
    
    print(f"\n🎉 Successfully created {created_count}/{total_count} issues!")
    
    if created_count == total_count:
        print("All issues created successfully. You can view them at:")
        print("https://github.com/atlanhq/application-sdk/issues?q=is%3Aissue+label%3Av0.2.0")
    else:
        print(f"⚠️  {total_count - created_count} issues failed to create. Please check the errors above.")

if __name__ == "__main__":
    main()