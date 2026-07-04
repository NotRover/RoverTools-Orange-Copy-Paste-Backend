"""Single source of truth for API and service versioning.

- ``API_VERSION`` — the API *contract* major version, e.g. ``"v1"``.  It appears
  in every route prefix (``/api/v1``, ``/internal/v1``) and in the
  ``X-API-Version`` response header.  Bump it (``v2``, …) only on a
  backwards-incompatible change to the request/response contract; run ``v1`` and
  ``v2`` side by side during a migration window.

- ``SERVICE_VERSION`` — the deployable build version (semver).  Surfaced as the
  OpenAPI ``version`` and is free to change on every release; it does NOT imply a
  contract break.

Versioning policy
-----------------
Product API endpoints are versioned: ``/api/v1/*`` (client-facing) and
``/internal/v1/*`` (admin/stats management).  Infrastructure probes are
intentionally left *unversioned* because load balancers and metrics scrapers
hardcode their paths and must not have to track a version on every bump:
``/internal/healthz`` (liveness/readiness) and ``/internal/metrics`` (Prometheus).
"""

API_VERSION = "v1"
SERVICE_VERSION = "2.0.0"

API_PREFIX = f"/api/{API_VERSION}"
INTERNAL_VERSIONED_PREFIX = f"/internal/{API_VERSION}"
