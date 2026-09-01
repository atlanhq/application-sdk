import argparse
import logging
import os
import re
import subprocess
import sys

import release_guard
import semver

PYPROJECT = "pyproject.toml"


def _set_output(key, value):
    """Write a step output for the calling workflow.

    Falls back to stdout for local runs, where GITHUB_OUTPUT is unset.
    """
    gho = os.environ.get("GITHUB_OUTPUT")
    if gho:
        with open(gho, "a") as f:
            f.write(f"{key}={value}\n")
    else:
        logging.info(f"OUTPUT: {key}={value}")


# Commits scoped to sub-packages that manage their own versioning are dropped
# from the SDK bump walk. Path-only exclusion in git log doesn't cover mixed
# PRs (a squash commit that touches both SDK files and sub-package files), so
# we also filter by conventional-commit scope on the subject line.
_SUBPKG_RE = re.compile(r"^[a-z]+\((contract-toolkit|conformance)\)!?:")


def _git(*args: str, quiet: bool = False) -> str:
    """Run git with an explicit argv list and return stdout, stripped.

    Deliberately not `shell=True`. Command output interpolated into a shell
    string is not guaranteed to be a single line, and a newline silently splits
    one command into two — the second half is then executed as a command name.
    An argv list makes a multi-line value a plain bad argument instead.

    Args:
        *args: Arguments passed to git.
        quiet: Discard stderr. Only for probes whose failure is expected and
            handled by the caller, so the log is not polluted with a `fatal:`
            line describing a non-problem.
    """
    return (
        subprocess.check_output(
            ["git", *args],
            stderr=subprocess.DEVNULL if quiet else None,
        )
        .decode()
        .strip()
    )


def last_release_tag() -> str | None:
    """Return the most recent non release-candidate version tag, or None.

    None means the repository has never been released — it has no tag matching
    `v[0-9]*`, or all of its tags predate that convention.
    """
    try:
        return _git(
            "describe",
            "--tags",
            "--abbrev=0",
            "--match=v[0-9]*",
            "--exclude=*rc*",
            quiet=True,
        )
    except subprocess.CalledProcessError:
        return None


def _resolve_rev_range() -> list[str]:
    """Resolve the revision range to walk when determining the version bump.

    Returns:
        List[str]: `["<tag>..HEAD"]` when a released version tag exists,
                   otherwise `["HEAD"]` to walk the full history.

    The untagged case walks HEAD rather than deriving a start point from
    `git rev-list --max-parents=0 HEAD`. That command emits one SHA *per root
    commit*, so a repository whose history was grafted from more than one root
    yields a multi-line value — which is not a usable revision. It also excludes
    the root commit itself from the range, dropping it from the changelog.
    """
    last_tag = last_release_tag()
    if last_tag is None:
        logging.info("No release tag found - walking full history from HEAD")
        return ["HEAD"]

    logging.info(f"Last tag found: {last_tag}")
    return [f"{last_tag}..HEAD"]


def get_commits_since_last_tag() -> list[str]:
    """Get all commits since the last non release-candidate tag.

    Returns:
        List[str]:  List of commit messages since the last git non release-candidate tag.
                    If no such tag exists, returns every commit reachable from HEAD.
    """
    rev_range = _resolve_rev_range()
    try:
        # Get all commits in that range, excluding sub-packages that manage
        # their own versioning and changelogs (contract-toolkit, conformance).
        commits = _git(
            "log",
            *rev_range,
            "--pretty=format:%s%n%b",
            "--",
            ".",
            ":(exclude)contract-toolkit",
            ":(exclude)packages/conformance",
        ).split("\n")
        # Filter out empty lines that may appear between commits
        commits = [commit for commit in commits if commit.strip()]
        # Drop sub-package scoped commits that slipped through the path filter
        # (e.g. a mixed PR whose squash subject is feat(conformance): …).
        commits = [c for c in commits if not _SUBPKG_RE.match(c.splitlines()[0])]
        logging.info(f"Found {len(commits)} commits in {' '.join(rev_range)}")
        return commits

    except subprocess.CalledProcessError as e:
        logging.error(f"Error retrieving commits: {e}")
        raise e


def parse_conventional_commits(commits: list[str]) -> tuple[bool, bool, bool]:
    """Parse conventional commit messages to determine version bump type.

    Args:
        commits (List[str]): List of commit messages to analyze.

    Returns:
        Tuple[bool, bool, bool]: A tuple containing three flags:
            - is_breaking: True if breaking changes are detected
            - is_feature: True if new features are detected
            - is_fix: True if bug fixes are detected
    """
    logging.info(f"Parsing {len(commits)} conventional commits")
    is_breaking = False
    is_feature = False
    is_fix = False

    breaking_pattern = "!:"
    breaking_change = "BREAKING CHANGE:"
    feature_pattern = r"^feat[(!:]"
    fix_pattern = r"^fix[(!:]"

    for commit in commits:
        if re.search(
            breaking_pattern, commit, re.MULTILINE | re.IGNORECASE
        ) or re.search(breaking_change, commit, re.MULTILINE | re.IGNORECASE):
            is_breaking = True
        elif re.search(feature_pattern, commit, re.MULTILINE | re.IGNORECASE):
            is_feature = True
        elif re.search(fix_pattern, commit, re.MULTILINE | re.IGNORECASE):
            is_fix = True

    logging.info(
        f"Commit analysis results - Breaking: {is_breaking}, Feature: {is_feature}, Fix: {is_fix}"
    )
    return is_breaking, is_feature, is_fix


def calculate_version_bump(
    current_version: str, commits: list[str], current_branch: str
) -> str:
    """Calculate the next version based on conventional commits, semver rules, and branch name.

    Args:
        current_version (str): Current version string (e.g., "1.0.0")
        commits (List[str]): List of conventional commit messages

    Returns:
        str: New version string based on conventional commit analysis and branch name.
            Returns current version if no bump is needed.
    """
    logging.info(
        f"Calculating version bump from {current_version} for {current_branch}"
    )
    version = semver.VersionInfo.parse(current_version)

    if current_branch == "main":
        is_breaking, is_feature, is_fix = parse_conventional_commits(commits=commits)
        logging.info(f"Breaking: {is_breaking}, Feature: {is_feature}, Fix: {is_fix}")

        if is_breaking:
            new_version = version.bump_major()
            logging.info(
                f"Breaking change detected - bumping major version to {new_version}"
            )
        elif is_feature:
            new_version = version.bump_minor()
            logging.info(f"Feature detected - bumping minor version to {new_version}")
        elif is_fix:
            new_version = version.next_version(part="patch")
            logging.info(f"Fix detected - bumping version to {new_version}")
        else:
            new_version = version.next_version(part="patch")
            logging.info(
                f"No changes detected - bumping patch version to {new_version}"
            )

        return str(new_version)
    else:
        logging.error(
            f"Unexpected branch '{current_branch}'. Only 'main' is supported."
        )
        sys.exit(1)


def update_pyproject_version(new_version: str) -> None:
    """Update the version in pyproject.toml using uv.

    Args:
        new_version (str): Version string to set in pyproject.toml

    Raises:
        subprocess.CalledProcessError: If uv fails to update the version
    """
    logging.info(f"Updating pyproject.toml version to {new_version}")
    try:
        subprocess.run(
            [
                "uvx",
                "--from=toml-cli",
                "toml",
                "set",
                "--toml-path=pyproject.toml",
                "project.version",
                new_version,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        logging.info("Successfully updated pyproject.toml version")
    except subprocess.CalledProcessError as e:
        logging.error(f"Failed to update version in pyproject.toml: {e}")
        raise


def apply_first_release_floor(
    new_version: str, floor: str | None, has_release_tag: bool
) -> str:
    """Raise a repository's *first* release to at least `floor`.

    Apps are scaffolded at 0.1.0, so a plain conventional-commit bump would
    publish their first ever release as 0.2.0 and trickle up from there. A
    first release is a 1.0.0 event, so on the first release only, anything
    below the floor is raised to it.

    Args:
        new_version (str): Version produced by the conventional-commit bump.
        floor (str | None): Minimum version for a first release. None or empty
            disables the floor entirely (the SDK's own release passes nothing).
        has_release_tag (bool): Whether the repository has been released before.

    Returns:
        str: `floor` when this is a first release and the computed bump is below
             it, otherwise `new_version` unchanged.

    A floor, not an assignment: repositories already at or past it must keep
    bumping normally. 13 of the 36 currently-untagged apps sit at exactly 1.0.0
    in pyproject.toml, and forcing the version to 1.0.0 there would produce a
    bump PR that does not change the version at all.
    """
    if not floor or has_release_tag:
        return new_version

    if semver.VersionInfo.parse(floor) <= semver.VersionInfo.parse(new_version):
        logging.info(
            f"First release, but {new_version} already meets the {floor} floor - leaving as is"
        )
        return new_version

    logging.info(
        f"First release for this repository - raising {new_version} to {floor}"
    )
    return floor


def main():
    """Main entry point for the version update process.

    Sets up logging and orchestrates the version update workflow:
    1. Gets current version
    2. Retrieves commits since last tag
    3. Calculates version bump
    4. Applies the first-release floor, if one was requested
    5. Updates pyproject.toml with new version
    """
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
    )
    logging.info("Starting version update process")

    parser = argparse.ArgumentParser(description="Bump the project version.")
    parser.add_argument(
        "branch", help="Branch being released (only 'main' is supported)"
    )
    parser.add_argument("current_version", help="Current version from pyproject.toml")
    parser.add_argument(
        "--first-release-version",
        default="",
        help=(
            "Minimum version for a repository's first release, i.e. one with no "
            "v* tag yet. Empty (the default) disables the floor and uses the "
            "plain conventional-commit bump."
        ),
    )
    args = parser.parse_args()

    current_branch = args.branch
    current_version = args.current_version
    commits = get_commits_since_last_tag()

    new_version = calculate_version_bump(
        current_version=current_version, commits=commits, current_branch=current_branch
    )
    new_version = apply_first_release_floor(
        new_version,
        floor=args.first_release_version,
        has_release_tag=last_release_tag() is not None,
    )

    # This job may be running against a frozen `pull_request` merge ref that
    # predates an already merged-and-published release, in which case
    # `current_version` is stale and `new_version` is one that already shipped.
    # See release_guard for the incident (application-sdk#3570) and for why the
    # check reads the version on the target branch rather than testing for a
    # tag. Must run before pyproject.toml is touched.
    #
    # Callers gate their changelog and commit steps on this output. A caller
    # pinned to an older sdk_scripts_ref never sets it, and an unset output
    # reads as empty, so those steps simply run as they always did.
    skip, remote_version = release_guard.already_released(
        PYPROJECT, new_version, branch=current_branch
    )
    if skip:
        logging.warning(
            release_guard.skip_message(
                PYPROJECT, new_version, remote_version, branch=current_branch
            )
        )
        _set_output("skip", "true")
        return

    _set_output("skip", "false")
    _set_output("new", new_version)
    update_pyproject_version(new_version=new_version)


if __name__ == "__main__":
    main()
