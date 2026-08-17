"""Uniform access to the integration source credentials, however provisioned.

Every connector's integration suite needs the same thing: the host / username /
password (or client-id / secret / role-arn / …) of the source it extracts from.
Those credentials reach the test runner one of two ways, both wired in the SDK's
``tests-reusable.yaml`` (the integration and e2e jobs export them identically):

* a static ``E2E_SOURCE_ENV_JSON`` the repo composes from its own secrets, or
* a live DataForge fetch (``fetch_dataforge_source.py``), OIDC-authenticated in
  CI against a curated instance.

Both export the source's fields as ``E2E_<DATASOURCE>_<FIELD>`` environment
variables — but the field *names* are source-shaped (``E2E_POSTGRES_HOST`` for a
JDBC source, ``E2E_POWERBI_CLIENT_SECRET`` for a SaaS one), so there is no single
fixed env contract 74 connectors can each hard-code. What IS uniform is the
breadcrumb the fetch also writes: ``E2E_SOURCE_RAW_JSON`` (every scalar field as
one JSON object) and ``E2E_SOURCE_DATASOURCE`` (the datasource name).

:class:`DataForgeSource` reads both — the uniform blob and the flat
``E2E_<DATASOURCE>_*`` vars — so every connector consumes its source the same
way: a case- and separator-insensitive field bag plus an availability probe,
instead of each repo re-implementing its own ``os.environ`` plumbing and its own
``basic_available()`` predicate. The connector still maps the fields onto its own
connection/config shape (that part is irreducibly per-connector); only the
*reading* is unified.

When no source resolved — the fetch was disabled, or it fell back to nothing —
:attr:`~DataForgeSource.available` is ``False`` and the suite skips. That is the
skip-not-fail contract the integration tier relies on: an absent source is a
skipped scenario, never a hard failure on missing infrastructure.

Example::

    src = DataForgeSource.from_env("postgres")
    if not src.require("host", "username", "password"):
        pytest.skip("no postgres integration source available")
    creds = {
        "host": src.get("host"),
        "port": int(src.get("port", default="5432")),
        "username": src.get("username", "user"),
        "password": src.get("password"),
    }
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Mapping


def _normalise(key: str) -> str:
    """Fold a field name to its comparison form: lowercase, alphanumerics only.

    So ``E2E_POSTGRES_HOST``'s field ``HOST``, a JSON key ``host`` and a lookup
    for ``Host`` all resolve to the same entry, and separator differences
    (``iam_role_arn`` vs ``iamRoleArn``) never split one field into two.
    """
    return re.sub(r"[^a-z0-9]", "", key.lower())


@dataclass(frozen=True)
class DataForgeSource:
    """The integration source's credential fields, read uniformly from env.

    ``datasource`` is the DataForge datasource name — supplied by the connector
    or taken from the ``E2E_SOURCE_DATASOURCE`` breadcrumb — or ``""`` when
    unknown. ``fields`` maps each field's normalised name to its value; use
    :meth:`get` / :meth:`require` rather than indexing it directly, so
    separator and case differences (``iam_role_arn`` vs ``iamRoleArn`` vs
    ``IAM-ROLE-ARN``) resolve the same way in every connector.
    """

    datasource: str
    fields: Mapping[str, str]

    @property
    def available(self) -> bool:
        """True when at least one source field resolved.

        A cheap presence check; :meth:`require` is the one to gate a scenario on,
        because it names the fields the connector actually needs.
        """
        return bool(self.fields)

    def get(self, *names: str, default: str | None = None) -> str | None:
        """First non-empty field among ``names`` (case/separator-insensitive).

        Pass aliases most-specific first — ``get("basic_auth_host", "host")`` —
        to prefer an auth-mode-specific field but fall back to the canonical one
        the DataForge fetch exports.
        """
        for name in names:
            value = self.fields.get(_normalise(name))
            if value:
                return value
        return default

    def require(self, *names: str) -> bool:
        """True iff every name resolves to a non-empty value.

        The availability predicate every connector's ``skipif`` should call, in
        place of a hand-rolled ``bool(HOST and USER and PASSWORD)``. With no
        arguments it returns ``False`` — a bare ``require()`` names no fields,
        so it must not read as "source present".
        """
        return bool(names) and all(self.get(name) for name in names)

    def as_dict(self) -> dict[str, str]:
        """A copy of the field bag, keyed by normalised field name."""
        return dict(self.fields)

    @classmethod
    def from_env(
        cls,
        datasource: str = "",
        *,
        env_prefix: str = "",
        environ: Mapping[str, str] | None = None,
    ) -> "DataForgeSource":
        """Build from the process environment (or an explicit ``environ`` map).

        ``datasource`` is the connector's DataForge datasource name (``postgres``,
        ``powerbi``, …). It is optional: when omitted, the ``E2E_SOURCE_DATASOURCE``
        breadcrumb the fetch writes is used instead. Supplying it lets the flat
        ``E2E_<DATASOURCE>_*`` vars be read even on the static path, where no
        breadcrumb is written.

        ``env_prefix`` overrides the flat-var prefix when the fetch exported
        under a custom ``dataforge-output-prefix`` (``metabase`` exported as
        ``E2E_META_BASE_HOST``): pass the same value (``"meta_base"`` — any
        spelling; it is folded the same way) or the flat pass derives
        ``E2E_METABASE_`` and never matches. A future ``E2E_SOURCE_PREFIX``
        breadcrumb exported beside ``E2E_SOURCE_DATASOURCE`` would let the
        reader discover the alias itself; until then the caller must say it.
        """
        env = os.environ if environ is None else environ
        resolved_ds = (datasource or env.get("E2E_SOURCE_DATASOURCE") or "").strip()
        # The explicit prefix wins over the datasource-derived one; it is folded
        # through the same derivation so casing/separators never matter.
        prefix_source = (env_prefix or "").strip() or resolved_ds
        fields: dict[str, str] = {}

        # 1) The uniform blob, present only when the DataForge fetch ran. It is
        #    the authoritative field map, so it is read first and wins over the
        #    flat vars below on any collision — a blob field RESERVES its
        #    normalised key even when empty/None, so a flat var can never
        #    backfill a field the fetch explicitly reported as empty. The blob
        #    is SCALARS-ONLY: the fetch puts structured fields in a separate
        #    E2E_SOURCE_EXTRA_JSON (not read here), and a non-scalar value in a
        #    hand-written blob would otherwise be stored as its Python repr —
        #    a string that looks like data but isn't. It still reserves its
        #    key, so a flat var can't backfill it with a divergent scalar.
        raw = env.get("E2E_SOURCE_RAW_JSON")
        blob_keys: set[str] = set()
        if raw:
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                for key, value in parsed.items():
                    normalised = _normalise(str(key))
                    if not normalised:
                        continue
                    blob_keys.add(normalised)
                    if (
                        value is not None
                        and not isinstance(value, (dict, list))
                        and str(value).strip() != ""
                    ):
                        fields[normalised] = str(value)

        # 2) The flat ``E2E_<PREFIX>_<FIELD>`` vars — written by BOTH the static
        #    E2E_SOURCE_ENV_JSON export and the fetch — so the static path (which
        #    writes no blob) still resolves. Scoped to the datasource prefix, so
        #    unrelated E2E_* vars (tenant creds, feature flags) never leak into
        #    the source bag. Values are stripped so a whitespace-only var reads
        #    as absent, matching the blob path's empty handling. Keys the blob
        #    already claimed are skipped, keeping the blob authoritative.
        #    A datasource that is only separators (or empty) would derive the
        #    bare "E2E_" prefix and match every E2E_* var in the environment —
        #    tenant creds, flags, everything — so the flat pass requires a real
        #    datasource-specific prefix.
        #
        #    Known limitation (accepted, needs an export-contract change): the
        #    prefix match also admits a SIBLING datasource's vars whose name
        #    shares this prefix — ``E2E_POSTGRES_READONLY_HOST`` folds into the
        #    ``postgres`` bag as ``readonlyhost``. A reader-side guard cannot
        #    fix this, because the export contract (``_env_name`` in
        #    ``fetch_dataforge_source.py``) folds a source's OWN multi-word
        #    fields the same way (``iam_role_arn`` → ``E2E_POSTGRES_IAM_ROLE_ARN``),
        #    making a sibling var indistinguishable from an own-field on the
        #    flat path. The durable fix is a delimiter the datasource segment
        #    can never produce (e.g. ``__`` before FIELD) applied at the export
        #    site, with the reader splitting on it. Until then the loose match
        #    is kept deliberately: a leak copies a sibling's non-secret field
        #    names AND values into a test bag, while dropping keys would empty
        #    the bag on the static path and green the merge gate on untested code.
        prefix = ""
        if prefix_source:
            derived = re.sub(r"[^A-Za-z0-9]", "_", prefix_source.upper())
            # Require at least one alphanumeric: a separator-only datasource
            # ("-", " — ") folds to bare underscores, which is no real scope.
            if not re.search(r"[A-Z0-9]", derived):
                derived = ""
            if derived:
                prefix = "E2E_" + derived + "_"
        if prefix:
            for key, value in env.items():
                if key.startswith(prefix):
                    normalised = _normalise(key[len(prefix) :])
                    # A var exactly equal to the prefix has no field name; a
                    # normalised-empty key would be stored but never readable.
                    if not normalised or normalised in blob_keys:
                        continue
                    stripped = value.strip() if value else ""
                    if stripped:
                        fields.setdefault(normalised, stripped)

        return cls(datasource=resolved_ds, fields=fields)
