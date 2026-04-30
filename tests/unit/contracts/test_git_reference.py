"""Unit tests for GitReference."""

import pytest
from pydantic import ValidationError

from application_sdk.contracts.types import GitReference
from application_sdk.credentials.ref import CredentialRef


class TestGitReferenceDefaults:
    def test_required_field(self) -> None:
        ref = GitReference(repo_url="https://github.com/org/repo")
        assert ref.repo_url == "https://github.com/org/repo"

    def test_default_branch(self) -> None:
        ref = GitReference(repo_url="https://github.com/org/repo")
        assert ref.branch == "main"

    def test_default_path_empty(self) -> None:
        ref = GitReference(repo_url="https://github.com/org/repo")
        assert ref.path == ""

    def test_default_tag_empty(self) -> None:
        ref = GitReference(repo_url="https://github.com/org/repo")
        assert ref.tag == ""

    def test_default_commit_empty(self) -> None:
        ref = GitReference(repo_url="https://github.com/org/repo")
        assert ref.commit == ""

    def test_default_credential_none(self) -> None:
        ref = GitReference(repo_url="https://github.com/org/repo")
        assert ref.credential is None


class TestGitReferenceImmutability:
    def test_frozen(self) -> None:
        ref = GitReference(repo_url="https://github.com/org/repo")
        with pytest.raises((ValidationError, AttributeError, TypeError)):
            ref.repo_url = "https://github.com/other/repo"  # type: ignore[misc]

    def test_hashable(self) -> None:
        ref1 = GitReference(repo_url="https://github.com/org/repo", branch="main")
        ref2 = GitReference(repo_url="https://github.com/org/repo", branch="dev")
        s = {ref1, ref2}
        assert len(s) == 2

    def test_equality(self) -> None:
        ref1 = GitReference(repo_url="https://github.com/org/repo", branch="main")
        ref2 = GitReference(repo_url="https://github.com/org/repo", branch="main")
        ref3 = GitReference(repo_url="https://github.com/org/repo", branch="dev")
        assert ref1 == ref2
        assert ref1 != ref3


class TestGitReferenceWithCredential:
    def test_with_credential_ref(self) -> None:
        cred = CredentialRef(name="my-token", credential_type="git_token")
        ref = GitReference(
            repo_url="https://github.com/org/private-repo",
            credential=cred,
        )
        assert ref.credential is cred
        assert ref.credential.name == "my-token"

    def test_without_credential(self) -> None:
        ref = GitReference(repo_url="https://github.com/org/public-repo")
        assert ref.credential is None


class TestGitReferenceCheckoutCoordinates:
    def test_branch_override(self) -> None:
        ref = GitReference(
            repo_url="https://github.com/org/repo",
            branch="feature/my-branch",
        )
        assert ref.branch == "feature/my-branch"

    def test_tag_set(self) -> None:
        ref = GitReference(
            repo_url="https://github.com/org/repo",
            tag="v1.2.3",
        )
        assert ref.tag == "v1.2.3"

    def test_commit_set(self) -> None:
        ref = GitReference(
            repo_url="https://github.com/org/repo",
            commit="abc123def456",
        )
        assert ref.commit == "abc123def456"

    def test_path_subdirectory(self) -> None:
        ref = GitReference(
            repo_url="https://github.com/org/repo",
            path="models/my_model",
        )
        assert ref.path == "models/my_model"


class TestGitReferenceAsdict:
    def test_asdict_roundtrip(self) -> None:
        cred = CredentialRef(name="git-cred", credential_type="git_token")
        ref = GitReference(
            repo_url="https://github.com/org/repo",
            branch="release",
            path="subdir",
            tag="v2.0.0",
            commit="deadbeef",
            credential=cred,
        )
        d = ref.model_dump()
        assert d["repo_url"] == "https://github.com/org/repo"
        assert d["branch"] == "release"
        assert d["path"] == "subdir"
        assert d["tag"] == "v2.0.0"
        assert d["commit"] == "deadbeef"
        assert d["credential"]["name"] == "git-cred"
        assert d["credential"]["credential_type"] == "git_token"

    def test_asdict_no_credential(self) -> None:
        ref = GitReference(repo_url="https://github.com/org/repo")
        d = ref.model_dump()
        assert d["credential"] is None
