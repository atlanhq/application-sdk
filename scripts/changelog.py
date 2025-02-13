import os
import subprocess
from typing import Optional

from google import genai

client = genai.Client(api_key=os.environ["GOOGLE_GEMINI_API_KEY"])


def get_git_diff(start_commit: str, end_commit: str = "HEAD") -> str:
    """Get git diff between two commits."""
    try:
        result = subprocess.run(
            ["git", "diff", f"{start_commit}..{end_commit}"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Error getting git diff: {e}")


def generate_changelog(diff: str) -> str:
    """Generate a changelog using Gemini AI based on the git diff."""
    prompt = """
    Based on the following git diff, generate a clear and concise changelog.
    Focus on user-facing changes and group them into categories like:
    - Features
    - Bug Fixes
    - Performance Improvements
    - Documentation

    Format the changelog in markdown.

    Git diff:
    {diff}
    """

    response = client.models.generate_content(
        model="gemini-2.0-flash", contents=prompt.format(diff=diff)
    )
    return response.text


def main(start_commit: str, end_commit: Optional[str] = None) -> None:
    """Generate changelog between two git commits."""
    try:
        # Get the git diff
        diff = get_git_diff(start_commit, end_commit or "HEAD")

        # Generate changelog
        changelog = generate_changelog(diff)

        # Print the changelog
        print(changelog)

    except Exception as e:
        print(f"Error generating changelog: {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Generate changelog between git commits using Gemini AI"
    )
    parser.add_argument("start_commit", help="Starting commit hash or reference")
    parser.add_argument(
        "end_commit",
        nargs="?",
        default=None,
        help="Ending commit hash or reference (defaults to HEAD)",
    )

    args = parser.parse_args()
    main(args.start_commit, args.end_commit)
