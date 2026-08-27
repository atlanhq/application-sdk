"""Unit tests for evidence collection and redaction (child G, FND-243).

**These are claims, and the risk sits somewhere specific.** No redaction filter
existed at this boundary before, so there is no original to run a differential
against — but unlike the queue starter, the cost of a wrong claim here is not a
failed test run. It is a credential in a retained CI artifact. So the tests are
weighted accordingly: the ones that carry the most are the shapes a filter
plausibly misses, not the ones it obviously catches.

Four such shapes, each of which a key-name-only filter gets wrong:

* the JSON form, ``"apiKey": "..."`` — the key is quoted, so a pattern anchored
  on ``key=`` or ``key:`` never sees it, and the same log carries both forms;
* ``Authorization: Bearer <token>`` — the value class stops at the first space,
  which is right everywhere else and ships the token here;
* an ODBC ``PWD={se;cret}`` — the value's own ``;`` ends a naive match early and
  leaks the tail;
* a bare literal with no key beside it — invisible to key matching by
  construction, which is exactly why ``resolve_e2e_tenant.py`` runs a
  ``--mask-only`` pass over the *values*.

And one shape it must **not** touch: ``run_id`` and ``correlation_uuid``, which
are what a person navigates a report by. Over-redaction is the intended failure
direction, but a filter that redacted those would make the bundle unusable for
the thing it exists to do.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from application_sdk.testing.harness.cluster import PodPhase, PodState
from application_sdk.testing.harness.evidence import (
    PLACEHOLDER,
    EvidenceBundle,
    collect_pod_evidence,
    redact,
    redact_text,
    secrets_from_environment,
    write_bundle,
)
from application_sdk.testing.harness.expectations import UNREADABLE, Finding

# ---------------------------------------------------------------------------
# The shapes a key-name filter misses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("password=hunter2 host=db1", "password=*** host=db1"),
        ('"apiKey": "abc123def"', '"apiKey": "***"'),
        ("'client_secret': 'shh-dont'", "'client_secret': '***'"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9", "Authorization: ***"),
        ("DRIVER={x};UID=sa;PWD={se;cret};", "DRIVER={x};UID=sa;PWD=***;"),
        ("X-Api-Key: zzz-top-secret", "X-Api-Key: ***"),
        ("AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI", "AWS_SECRET_ACCESS_KEY=***"),
        ("postgresql://svc:pa55@db.internal/x", "postgresql://***@db.internal/x"),
        ("set-cookie: session=deadbeef", "set-cookie: ***"),
        ("private_key=-----BEGIN", "private_key=***"),
    ],
    ids=[
        "bare key=value",
        "JSON string value",
        "single-quoted value",
        "auth scheme plus token",
        "ODBC braced value with a semicolon",
        "hyphenated header name",
        "screaming-snake env name",
        "URL userinfo",
        "cookie",
        "PEM body",
    ],
)
def test_a_credential_shaped_value_does_not_survive(line: str, expected: str) -> None:
    """Exact equality, not ``leaked not in redacted``.

    The weaker form was here first and it cannot reject the failure that
    actually happens. Partial redaction — the escaped-quote truncation, a value
    class that stops one character early — leaves output that satisfies every
    "the secret is gone"-shaped assertion you can write about it: the *whole*
    literal is indeed absent, and a placeholder is indeed present. Only naming
    the entire expected line rejects a residue.

    It also pins the other half of the contract, which a negative assertion
    cannot express at all: that the neighbouring host, the ODBC ``UID`` and the
    next ``key=value`` pair **survive**. An over-redacting filter is a bundle
    nobody can act on, and it would pass the weak form perfectly.
    """
    assert redact_text(line) == expected


def test_the_neighbouring_pair_in_a_connection_string_survives() -> None:
    """Over-redaction is the safe direction, but not to the point of erasing the
    host — a redacted bundle that cannot say which database was reached is a
    bundle nobody can act on."""
    assert redact_text("password=hunter2 host=db1 port=5432") == (
        f"password={PLACEHOLDER} host=db1 port=5432"
    )


@pytest.mark.parametrize(
    "line",
    [
        "run_id=1700000000 correlation_uuid=abc-123",
        "workflow_id=metadata-extraction-42 task_queue=atlan-postgres-prod",
        "auth_type=basic",
        "connected as uid=sa",
    ],
    ids=["ids", "workflow identity", "auth_type", "ODBC uid"],
)
def test_what_a_report_is_navigated_by_survives(line: str) -> None:
    """The one deliberate non-over-redaction. ``auth_type`` is why a bare
    ``auth`` is not in the fragment list, and ``uid`` is a user name rather than
    a credential — the same call ``redact_secrets`` makes and for the same
    reason."""
    assert redact_text(line) == line


def test_a_json_artifact_stays_parseable_after_redaction() -> None:
    """A masked value inside valid JSON beats a valid secret inside broken JSON,
    but a bundle whose machine-readable half stopped parsing at the redaction
    step is strictly worse than either."""
    body = json.dumps({"apiKey": "abc123", "host": "db1"})

    parsed = json.loads(redact_text(body))

    assert parsed == {"apiKey": PLACEHOLDER, "host": "db1"}


# ---------------------------------------------------------------------------
# The literal-value filter
# ---------------------------------------------------------------------------


def test_a_bare_literal_with_no_key_beside_it_is_blanked() -> None:
    """The shape key-name matching cannot see, and the reason the ``--mask-only``
    prior art registers values rather than names."""
    line = "GET /api/meta/atlas?t=glpat-ABCDEF failed"

    assert redact_text(line, secrets=["glpat-ABCDEF"]) == (
        f"GET /api/meta/atlas?t={PLACEHOLDER} failed"
    )


def test_a_two_character_literal_is_ignored() -> None:
    """Blanking it would destroy the evidence to protect nothing — a value that
    short is either not a secret or is a substring of half the log."""
    assert redact_text("db1 is up", secrets=["db"]) == "db1 is up"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (r'"password": "part\"secret"', '"password": "***"'),
        (r"{'client_secret': 'a\'b'}", "{'client_secret': '***'}"),
    ],
    ids=["JSON with an escaped double quote", "escaped single quote"],
)
def test_an_escaped_quote_inside_a_value_does_not_truncate_the_match(
    line: str, expected: str
) -> None:
    """The tail after an escaped quote is the leak a naive ``"[^"]*"`` ships.

    Any credential containing a quote is escaped by whatever serialiser wrote
    the log, so this is the *normal* shape for such a value rather than an edge
    case — and truncating at the escape leaves the remainder in an uploaded
    artifact while the line still looks redacted.

    **Exact equality, and that is the point.** The first version of this test
    asserted ``"secret" not in redacted or "part" not in redacted``, plus a
    placeholder count — and all three of those hold for the *buggy* output
    ``'"password": "***"secret"'``, because ``part`` is already gone and there
    is still exactly one placeholder. A revert of ``_KEYED_VALUE_RE`` would have
    gone green. A partial-redaction bug leaves output that satisfies every
    "the secret is gone"-shaped assertion you can write about it; only naming
    the whole expected line rejects it.
    """
    assert redact_text(line) == expected


def test_the_replacement_order_is_enforced_where_the_replacing_happens() -> None:
    """Unsorted input must be as safe as sorted input.

    ``secrets_from_environment`` sorts longest-first, but ``redact`` and
    ``write_bundle`` take any ``Sequence[str]`` — a caller assembling a plain
    list has no reason to know the order carries a correctness property. Pinned
    with a deliberately unsorted sequence, which the sorted-producer test below
    cannot exercise.
    """
    redacted = redact_text("saw tok-abcdef", secrets=["tok", "tok-abcdef"])

    assert redacted == f"saw {PLACEHOLDER}"
    assert "abcdef" not in redacted


def test_a_literal_that_prefixes_another_does_not_leave_a_tail() -> None:
    """Longest first, and this is why. Substituting the prefix first turns
    ``tok-abcdef`` into ``***-abcdef`` — a partial secret in a file that claims
    to have none."""
    secrets = secrets_from_environment(
        {"API_TOKEN": "tok", "OTHER_TOKEN": "tok-abcdef"}
    )

    assert redact_text("saw tok-abcdef", secrets=secrets) == f"saw {PLACEHOLDER}"


def test_secrets_are_collected_by_the_same_key_shape_the_text_filter_uses() -> None:
    """One definition of "credential-shaped" for both filters, so a fragment
    added to the list strengthens the value pass too."""
    collected = secrets_from_environment(
        {
            "ATLAN_API_KEY": "key-value",
            "SDR_CLIENT_SECRET": "secret-value",
            "ATLAN_BASE_URL": "https://tenant.example.com",
            "HOME": "/root",
        }
    )

    assert set(collected) == {"key-value", "secret-value"}


def test_the_tenant_url_is_contributed_on_request() -> None:
    """Not credential-shaped by name and not a credential by value, but a tenant
    hostname identifies a customer environment and the bundle is retained."""
    collected = secrets_from_environment(
        {"ATLAN_BASE_URL": "https://tenant.example.com"},
        also=("ATLAN_BASE_URL",),
    )

    assert collected == ("https://tenant.example.com",)


def test_a_blank_or_absent_variable_contributes_nothing() -> None:
    """A blank literal would match everywhere and blank the whole file."""
    assert secrets_from_environment({"API_TOKEN": "  "}, also=("NOT_SET",)) == ()


# ---------------------------------------------------------------------------
# The bundle
# ---------------------------------------------------------------------------


def _bundle() -> EvidenceBundle:
    return EvidenceBundle(
        label="TestPostgresE2E — postgres",
        findings=(
            Finding(
                subject="Table",
                detail="expected >= 10, saw 0 (password=hunter2)",
                expectation="floor",
            ),
            Finding(subject="Column", detail="search failed", expectation=UNREADABLE),
        ),
        logs={"pod-a/worker": ("token=abc", "starting up")},
        readings={
            "asset_counts": {"Table": 0, "Column": 0},
            "credential": {"username": "svc", "password": "hunter2"},
            "run_id": 1_700_000_000,
        },
        artifacts={"traceback.txt": "AuthError: api_key=abc123"},
    )


def test_redact_never_mutates_its_input() -> None:
    """A caller that logs locally and uploads remotely holds both; an in-place
    scrub makes the local copy useless."""
    bundle = _bundle()

    redact(bundle)

    assert bundle.readings["credential"] == {"username": "svc", "password": "hunter2"}


def test_a_credential_body_is_replaced_whole_rather_than_descended_into() -> None:
    """The key already says the value is a credential, so there is nothing to
    preserve inside it — and descending would pass through any sub-key the
    fragment list has not seen."""
    assert redact(_bundle()).readings["credential"] == PLACEHOLDER


def test_counts_stay_numbers() -> None:
    """The readings mapping is where the asset counts live, and a bundle that
    stringified them would be unusable for the question it exists to answer."""
    assert redact(_bundle()).readings["asset_counts"] == {"Table": 0, "Column": 0}


def test_the_expectation_marker_is_not_run_through_the_text_filter() -> None:
    """It is one of a closed set a report groups on. A future marker containing
    a fragment — ``token_shape``, say — would come out as ``***`` and stop
    matching :data:`UNREADABLE`."""
    assert redact(_bundle()).findings[1].expectation == UNREADABLE


def test_redaction_is_idempotent() -> None:
    """``write_bundle`` redacts unconditionally, so a caller that redacted first
    must not end up with a doubly-mangled bundle."""
    once = redact(_bundle())

    assert redact(once) == once


# ---------------------------------------------------------------------------
# Writing it out
# ---------------------------------------------------------------------------


def test_write_bundle_redacts_without_being_asked(tmp_path: Path) -> None:
    """This is the boundary the bundle crosses. Making redaction the caller's
    responsibility is what leaves the one path that forgot.

    Absence over the whole written directory rather than exact equality, and
    deliberately so: the question at *this* level is whether every file the
    boundary produces went through the filter, not what the filter does to one
    line. What the filter does is pinned exactly, line by line, above — so a
    partial-redaction residue is rejected there, where the assertion can name a
    whole expected output, rather than here, where it would have to name a
    directory.
    """
    write_bundle(_bundle(), tmp_path, secrets=["hunter2"])

    written = "\n".join(
        path.read_text(encoding="utf-8")
        for path in tmp_path.rglob("*")
        if path.is_file()
    )
    assert "hunter2" not in written
    assert "abc123" not in written


def test_the_report_is_machine_readable(tmp_path: Path) -> None:
    write_bundle(_bundle(), tmp_path)

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    assert report["label"] == "TestPostgresE2E — postgres"
    assert [finding["subject"] for finding in report["findings"]] == ["Table", "Column"]
    assert report["readings"]["asset_counts"] == {"Table": 0, "Column": 0}


def test_the_bundle_round_trips_non_ascii(tmp_path: Path) -> None:
    """Every file is UTF-8, and reading one back needs to say so.

    This is a regression test with a specific history: the first revision's
    assertions read with a bare ``read_text()``, which uses the *locale*
    encoding — UTF-8 on Linux and macOS, cp1252 on Windows. The suite was green
    on two platforms and red on the third, on a label carrying an em dash that
    ``BaseE2ETest`` puts there. The bug was in the reads, not the writes, but it
    is worth an assertion of its own either way: a connector name, a driver's
    error message and a pod's log line are all places non-ASCII arrives in a
    bundle, and evidence that mangles them is evidence someone mistrusts.
    """
    bundle = EvidenceBundle(
        label="Suite — postgres",
        logs={"pod/worker": ("café ✓",)},
        readings={"note": "naïve"},
        artifacts={"traceback.txt": "ValueError: ünicode"},
    )

    write_bundle(bundle, tmp_path)

    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report["label"] == "Suite — postgres"
    assert report["readings"]["note"] == "naïve"
    assert (tmp_path / "logs" / "pod-worker.log").read_text(
        encoding="utf-8"
    ) == "café ✓"
    assert (tmp_path / "traceback.txt").read_text(encoding="utf-8") == (
        "ValueError: ünicode"
    )


def test_each_log_source_gets_its_own_file(tmp_path: Path) -> None:
    """A person opens one container's log; a script parses the report. One blob
    serves neither."""
    write_bundle(_bundle(), tmp_path)

    assert (tmp_path / "logs" / "pod-a-worker.log").read_text(
        encoding="utf-8"
    ).splitlines()[1] == ("starting up")


def test_a_log_source_stays_one_path_segment(tmp_path: Path) -> None:
    """A source name carries ``pod/container``. Left alone the slash becomes a
    directory level, so ``logs/`` would hold a tree of pod directories instead of
    one file per source — and a reviewer scanning a downloaded artifact would
    have to descend into each.

    The flattening does mean a source literally named ``a-b`` and one named
    ``a/b`` land on the same file. Not guarded: both names come from Kubernetes,
    which does not allow a ``-``-for-``/`` substitution to produce a real
    collision, and a uniquifying suffix would cost every normal file a name a
    person has to decode.
    """
    write_bundle(EvidenceBundle(label="x", logs={"pod-a/worker": ("one",)}), tmp_path)

    assert [path.name for path in (tmp_path / "logs").iterdir()] == ["pod-a-worker.log"]


def test_an_artifact_cannot_escape_the_output_directory(tmp_path: Path) -> None:
    """An artifact key is a relative path a caller may assemble from a pod name.
    Neither ``..`` nor a leading ``/`` is rejected — an evidence write must not
    fail on a naming quirk — but neither may write outside the directory."""
    output = tmp_path / "bundle"

    written = write_bundle(
        EvidenceBundle(label="x", artifacts={"../../escaped.txt": "no"}), output
    )

    assert not (tmp_path.parent / "escaped.txt").exists()
    # Non-empty first: `all()` over nothing is true, and "it wrote nothing" is a
    # different outcome from "it wrote inside the directory".
    assert written
    assert all(output in path.parents for path in written)


def test_an_unwritable_directory_is_reported_not_raised(tmp_path: Path) -> None:
    """An evidence dump that failed must not become the failure being diagnosed."""
    blocker = tmp_path / "blocked"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")

    assert write_bundle(_bundle(), blocker / "under") == ()


def test_one_unwritable_file_does_not_cost_the_rest_of_the_bundle(
    tmp_path: Path,
) -> None:
    """``notes`` is written as a file, so ``notes/detail`` cannot then be one —
    an artifact key assembled from two names that happen to nest. The report and
    the first artifact still land, which is the whole reason this reports rather
    than raises."""
    written = write_bundle(
        EvidenceBundle(
            label="x", artifacts={"notes": "first", "notes/detail": "second"}
        ),
        tmp_path,
    )

    assert (tmp_path / "notes").read_text(encoding="utf-8") == "first"
    assert [path.name for path in written] == ["report.json", "notes"]


def test_a_reading_that_is_neither_container_nor_scalar_is_still_kept(
    tmp_path: Path,
) -> None:
    """An exception, an enum, a dataclass all reach the readings mapping. A
    bundle that dropped them would be quieter and less useful, so they are
    rendered and then sanitised like everything else."""
    bundle = EvidenceBundle(
        label="x", readings={"cause": ValueError("auth failed: password=hunter2")}
    )

    rendered = redact(bundle).readings["cause"]

    assert isinstance(rendered, str)
    assert "ValueError" in rendered
    assert "hunter2" not in rendered


# ---------------------------------------------------------------------------
# Pod collection
# ---------------------------------------------------------------------------


class _Reader:
    """Just the two verbs the collector uses, plus a record of the calls."""

    def __init__(self, pods: list[PodState], *, failing: str = "") -> None:
        self._pods = pods
        self._failing = failing
        self.reads: list[tuple[str, str, bool]] = []

    async def pods(self, namespace: str, selector: str) -> list[PodState]:
        if self._failing == "pods":
            raise RuntimeError("cluster unreachable")
        return self._pods

    async def container_log(
        self,
        namespace: str,
        pod: str,
        container: str,
        *,
        previous: bool = False,
        tail_lines: int | None = -1,
        **_kwargs: Any,
    ) -> str:
        self.reads.append((pod, container, previous))
        if self._failing == container:
            raise RuntimeError("no such container")
        return f"line for {pod}/{container}{'/previous' if previous else ''}"


def _pod(name: str, containers: dict[str, int]) -> PodState:
    return PodState(
        name=name,
        namespace="ns",
        phase=PodPhase.RUNNING,
        ready=False,
        restarts=sum(containers.values()),
        node="node-1",
        containers=containers,
    )


async def test_a_restarted_container_contributes_its_previous_output() -> None:
    """Where a crash loop's actual cause is, and the one thing a merged stream
    cannot express."""
    reader = _Reader([_pod("worker-0", {"main": 2, "sidecar": 0})])

    bundle = await collect_pod_evidence("ns", reader=reader)  # type: ignore[arg-type]

    assert set(bundle.logs) == {
        "worker-0/main",
        "worker-0/sidecar",
        "worker-0/main/previous",
    }


async def test_an_unreadable_listing_yields_an_empty_bundle_not_an_exception() -> None:
    """The one thing here allowed to fail open: this is read after the verdict,
    so a collector that raised would turn a diagnosable failure into an
    undiagnosable one."""
    reader = _Reader([], failing="pods")

    bundle = await collect_pod_evidence("ns", reader=reader)  # type: ignore[arg-type]

    assert bundle.logs == {}
    assert bundle.label == "pods in ns"


async def test_one_unreadable_container_costs_only_that_container() -> None:
    reader = _Reader([_pod("worker-0", {"main": 0, "broken": 0})], failing="broken")

    bundle = await collect_pod_evidence("ns", reader=reader)  # type: ignore[arg-type]

    assert bundle.logs["worker-0/broken"] == ()
    assert bundle.logs["worker-0/main"] == ("line for worker-0/main",)


async def test_pod_state_is_recorded_beside_the_logs() -> None:
    """A pod that is Running with a failing readiness probe is the exact shape a
    "worker is up" assertion must not accept, so the bundle has to say so."""
    reader = _Reader([_pod("worker-0", {"main": 3})])

    bundle = await collect_pod_evidence("ns", reader=reader)  # type: ignore[arg-type]

    assert bundle.readings["worker-0"] == {
        "phase": "Running",
        "ready": False,
        "restarts": 3,
        "node": "node-1",
    }
