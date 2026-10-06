"""Bump-PR release candidate: name, pin, mark, look up, promote (FND-3328)."""

from __future__ import annotations

import hashlib
import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import release_candidate as rc  # noqa: E402

REPO = "atlan-example-app"
BASE = f"ghcr.io/atlanhq/{REPO}"
TREE = "a" * 40
INDEX = "application/vnd.oci.image.index.v1+json"


def _manifest(seed: str) -> rc.Manifest:
    body = json.dumps({"schemaVersion": 2, "seed": seed}).encode()
    return rc.Manifest(body=body, media_type=INDEX)


class FakeRegistry(rc.Registry):
    """In-memory registry keyed by tag or digest; PUT stores the exact bytes."""

    def __init__(self, repo: str, user: str = "", token: str = "") -> None:
        super().__init__(repo, user, token)
        self.store: dict[str, rc.Manifest] = {}
        self.puts: list[str] = []
        self.corrupt_put = False

    def add(self, tag: str, manifest: rc.Manifest) -> None:
        self.store[tag] = manifest
        self.store[manifest.digest] = manifest

    def get(self, reference: str) -> Optional[rc.Manifest]:
        return self.store.get(reference)

    def put(self, tag: str, manifest: rc.Manifest) -> None:
        self.puts.append(tag)
        stored = _manifest("tampered") if self.corrupt_put else manifest
        self.add(tag, stored)


def _factory(registry: FakeRegistry):
    def make(repo: str, user: str, token: str) -> FakeRegistry:
        assert repo == registry.base.rsplit("/", 1)[1]
        return registry

    return make


def _git(tree: str = TREE):
    def git(args: list[str]) -> str:
        assert args == ["rev-parse", "HEAD^{tree}"]
        return tree

    return git


def _pyproject(tmp_path: Path, version: str = "1.2.3") -> str:
    path = tmp_path / "pyproject.toml"
    path.write_text(f'[project]\nname = "x"\nversion = "{version}"\n')
    return str(path)


# ── name ──────────────────────────────────────────────────────────────────────


def test_name_lands_every_tag_under_candidate_tree(tmp_path: Path) -> None:
    out = rc.cmd_name({"REPO": REPO, "PYPROJECT": _pyproject(tmp_path)}, git=_git())
    assert out == {
        "branch": "candidate",
        "image_tag": TREE,
        "ghcr_image": f"{BASE}:candidate-{TREE}",
        # A candidate moves no mutable tag.
        "ghcr_branch_tag": f"{BASE}:candidate-{TREE}",
        # The tag tag-and-release.yaml will cut, so the baked identity matches GM.
        "gm_version": "v1.2.3",
    }
    # build/merge compose `<branch>-<image_tag>`: that must be the same ref.
    assert f"{BASE}:{out['branch']}-{out['image_tag']}" == out["ghcr_image"]


def test_name_refuses_a_pyproject_without_a_version(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "x"\n')
    with pytest.raises(rc.CandidateError):
        rc.cmd_name({"REPO": REPO, "PYPROJECT": str(path)}, git=_git())


@pytest.mark.parametrize("repo", ["", "../evil", "a b", "x:y"])
def test_name_refuses_a_bad_repo_name(tmp_path: Path, repo: str) -> None:
    with pytest.raises(rc.CandidateError):
        rc.cmd_name({"REPO": repo, "PYPROJECT": _pyproject(tmp_path)}, git=_git())


# ── pin + mark ────────────────────────────────────────────────────────────────


def test_pin_returns_the_pushed_digest() -> None:
    registry = FakeRegistry(REPO)
    built = _manifest("built")
    registry.add(f"candidate-{TREE}", built)
    out = rc.cmd_pin({"IMAGE": f"{BASE}:candidate-{TREE}"}, factory=_factory(registry))
    assert out == {"image_ref": f"{BASE}:candidate-{TREE}@{built.digest}"}


def test_pin_refuses_a_non_candidate_ref() -> None:
    with pytest.raises(rc.CandidateError):
        rc.cmd_pin(
            {"IMAGE": f"{BASE}:main-abc1234"}, factory=_factory(FakeRegistry(REPO))
        )


def test_mark_copies_the_scanned_digest_not_whatever_the_tag_holds_now() -> None:
    registry = FakeRegistry(REPO)
    scanned = _manifest("scanned")
    registry.add(scanned.digest, scanned)
    # The tag was pushed again after the scan: mark must ignore it.
    registry.add(f"candidate-{TREE}", _manifest("re-pushed"))
    rc.cmd_mark(
        {
            "IMAGE": f"{BASE}:candidate-{TREE}@{scanned.digest}",
            "EXPECTED_REPO": REPO,
        },
        factory=_factory(registry),
    )
    assert registry.store[f"scanned-{TREE}"].digest == scanned.digest


@pytest.mark.parametrize(
    "image",
    [
        f"{BASE}:candidate-{TREE}",  # not pinned
        f"{BASE}:main-abc1234@sha256:{'b' * 64}",  # not a candidate
        f"ghcr.io/other/{REPO}:candidate-{TREE}@sha256:{'b' * 64}",  # wrong org
        f"docker.io/atlanhq/{REPO}:candidate-{TREE}@sha256:{'b' * 64}",
    ],
)
def test_mark_refuses_anything_but_a_pinned_ghcr_candidate(image: str) -> None:
    with pytest.raises(rc.CandidateError):
        rc.cmd_mark(
            {"IMAGE": image, "EXPECTED_REPO": REPO},
            factory=_factory(FakeRegistry(REPO)),
        )


@pytest.mark.parametrize("expected", ["", "atlan-other-app"])
def test_mark_refuses_an_image_of_another_repository(expected: str) -> None:
    """A caller may only mark its own package: the org PAT could write any."""
    registry = FakeRegistry(REPO)
    scanned = _manifest("scanned")
    registry.add(scanned.digest, scanned)
    with pytest.raises(rc.CandidateError, match="calling repository"):
        rc.cmd_mark(
            {
                "IMAGE": f"{BASE}:candidate-{TREE}@{scanned.digest}",
                "EXPECTED_REPO": expected,
            },
            factory=_factory(registry),
        )
    assert f"scanned-{TREE}" not in registry.store


@pytest.mark.parametrize(
    "toml",
    [
        '[project]\nversion = "1.2.3\\nbranch=main\\nimage_tag=known"\n',
        '[project]\nversion = "1.2\\r.3"\n',
        '[project]\nversion = "1.2.3 extra"\n',
        '[project]\nversion = ""\n',
        'project = "oops"\n',
        "[tool.x]\ny = 1\n",
    ],
)
def test_pyproject_version_refuses_anything_but_a_plain_version(
    tmp_path: Path, toml: str
) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text(toml)
    with pytest.raises(rc.CandidateError):
        rc.pyproject_version(path)


def test_lookup_rebuilds_on_malformed_project_metadata(tmp_path: Path) -> None:
    path = tmp_path / "pyproject.toml"
    path.write_text('project = "oops"\n')
    env = {"REPO": REPO, "RELEASE_TAG": "v1.2.3", "PYPROJECT": str(path)}
    assert rc.cmd_lookup(env, factory=_factory(FakeRegistry(REPO)), git=_git()) == {}


def test_main_never_writes_a_line_break_into_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setitem(rc.COMMANDS, "name", lambda _env: {"gm_version": "v1\nx=y"})
    assert rc.main(["release_candidate.py", "name"]) == 1
    assert not output.exists()


# ── lookup ────────────────────────────────────────────────────────────────────


def _lookup_env(tmp_path: Path, release_tag: str = "v1.2.3") -> dict[str, str]:
    return {"REPO": REPO, "RELEASE_TAG": release_tag, "PYPROJECT": _pyproject(tmp_path)}


def test_lookup_finds_the_scanned_candidate_for_this_tree(tmp_path: Path) -> None:
    registry = FakeRegistry(REPO)
    scanned = _manifest("scanned")
    registry.add(f"scanned-{TREE}", scanned)
    out = rc.cmd_lookup(_lookup_env(tmp_path), factory=_factory(registry), git=_git())
    assert out == {"promote_source": f"{BASE}@{scanned.digest}"}


def test_lookup_ignores_an_unscanned_candidate(tmp_path: Path) -> None:
    # A bump PR merged past a red scan has `candidate-` but never `scanned-`.
    registry = FakeRegistry(REPO)
    registry.add(f"candidate-{TREE}", _manifest("unscanned"))
    assert (
        rc.cmd_lookup(_lookup_env(tmp_path), factory=_factory(registry), git=_git())
        == {}
    )


def test_lookup_rebuilds_when_the_tree_differs(tmp_path: Path) -> None:
    registry = FakeRegistry(REPO)
    registry.add(f"scanned-{TREE}", _manifest("scanned"))
    out = rc.cmd_lookup(
        _lookup_env(tmp_path), factory=_factory(registry), git=_git("c" * 40)
    )
    assert out == {}


def test_lookup_rebuilds_when_the_release_tag_is_not_the_baked_version(
    tmp_path: Path,
) -> None:
    registry = FakeRegistry(REPO)
    registry.add(f"scanned-{TREE}", _manifest("scanned"))
    out = rc.cmd_lookup(
        _lookup_env(tmp_path, release_tag="v9.9.9"),
        factory=_factory(registry),
        git=_git(),
    )
    assert out == {}


def test_lookup_fails_open_on_a_registry_error(tmp_path: Path) -> None:
    class Broken(FakeRegistry):
        def get(self, reference: str) -> Optional[rc.Manifest]:
            raise OSError("network down")

    out = rc.cmd_lookup(
        _lookup_env(tmp_path), factory=_factory(Broken(REPO)), git=_git()
    )
    assert out == {}


# ── promote ───────────────────────────────────────────────────────────────────


def _promote_env(digest: str, *tags: str) -> dict[str, str]:
    return {"SOURCE": f"{BASE}@{digest}", "TAGS": "\n".join(tags) + "\n\n"}


def test_promote_copies_the_scanned_bytes_to_every_tag() -> None:
    registry = FakeRegistry(REPO)
    scanned = _manifest("scanned")
    registry.add(f"scanned-{TREE}", scanned)
    tags = [f"{BASE}:main-abc1234", f"{BASE}:main", f"{BASE}:latest", f"{BASE}:1.2.3"]
    out = rc.cmd_promote(
        _promote_env(scanned.digest, *tags), factory=_factory(registry)
    )
    assert out == {"digest": scanned.digest}
    for tag in ("main-abc1234", "main", "latest", "1.2.3"):
        assert registry.store[tag].body == scanned.body
        assert registry.store[tag].digest == scanned.digest
    assert scanned.digest == "sha256:" + hashlib.sha256(scanned.body).hexdigest()


def test_promote_dedupes_tags() -> None:
    registry = FakeRegistry(REPO)
    scanned = _manifest("scanned")
    registry.add(f"scanned-{TREE}", scanned)
    rc.cmd_promote(
        _promote_env(scanned.digest, f"{BASE}:main", f"{BASE}:main"),
        factory=_factory(registry),
    )
    assert registry.puts == ["main"]


def test_promote_fails_when_a_tag_does_not_read_back_as_the_scanned_digest() -> None:
    registry = FakeRegistry(REPO)
    scanned = _manifest("scanned")
    registry.add(f"scanned-{TREE}", scanned)
    registry.corrupt_put = True
    with pytest.raises(rc.CandidateError, match="reads back"):
        rc.cmd_promote(
            _promote_env(scanned.digest, f"{BASE}:main"), factory=_factory(registry)
        )


@pytest.mark.parametrize(
    "tag",
    [
        "ghcr.io/atlanhq/other-app:main",
        f"docker.io/atlanhq/{REPO}:main",
        f"{BASE}@sha256:{'d' * 64}",
    ],
)
def test_promote_refuses_tags_outside_the_source_repo(tag: str) -> None:
    registry = FakeRegistry(REPO)
    scanned = _manifest("scanned")
    registry.add(f"scanned-{TREE}", scanned)
    with pytest.raises(rc.CandidateError, match="refusing"):
        rc.cmd_promote(_promote_env(scanned.digest, tag), factory=_factory(registry))
    assert registry.puts == []


def test_promote_refuses_a_digest_the_registry_serves_differently() -> None:
    registry = FakeRegistry(REPO)
    claimed = "sha256:" + "e" * 64
    registry.store[claimed] = _manifest("something else")
    with pytest.raises(rc.CandidateError, match="served"):
        rc.cmd_promote(
            _promote_env(claimed, f"{BASE}:main"), factory=_factory(registry)
        )


# ── Registry client (HTTP) ────────────────────────────────────────────────────


class _Response:
    def __init__(self, body: bytes, content_type: str = INDEX) -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_registry_get_authenticates_with_push_scope_and_404_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str, Optional[str]]] = []

    def urlopen(request, timeout=None):  # type: ignore[no-untyped-def]
        url = request.full_url
        auth = request.get_header("Authorization")
        seen.append((request.get_method(), url, auth))
        if url.startswith("https://ghcr.io/token"):
            assert "pull%2Cpush" in url
            assert auth and auth.startswith("Basic ")
            return _Response(json.dumps({"token": "t0k"}).encode(), "application/json")
        if auth != "Bearer t0k":
            raise urllib.error.HTTPError(
                url,
                401,
                "unauthorized",
                {
                    "WWW-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:atlanhq/x:pull"'
                },  # type: ignore[arg-type]
                io.BytesIO(),
            )
        if url.endswith("/manifests/missing"):
            raise urllib.error.HTTPError(url, 404, "nf", {}, io.BytesIO())  # type: ignore[arg-type]
        return _Response(b'{"schemaVersion":2}')

    monkeypatch.setattr(rc.rbr, "_urlopen", urlopen)
    registry = rc.Registry(REPO, "bot", "pat")
    got = registry.get("scanned-" + TREE)
    assert got is not None and got.body == b'{"schemaVersion":2}'
    assert got.media_type == INDEX
    assert registry.get("missing") is None
    # One token exchange, reused.
    assert sum(1 for _, url, _ in seen if "/token" in url) == 1
