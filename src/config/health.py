"""Liveness and readiness probes.

`tasks.md` specifies both, with a specific distinction that is easy to get wrong:

* **livez** -- is the process alive? Must NOT touch dependencies. If it checked
  the database, a brief database blip would make the orchestrator kill healthy
  web workers, turning a recoverable dependency outage into a full restart.
* **readyz** -- can this instance actually serve traffic? Checks the three things
  every request path needs: database, cache/broker, object store.

Consequence of that split: Redis being down makes the instance **not ready**
(removed from the load balancer) but does **not** make it dead. Nothing is
restarted, because nothing is broken.

LDAP and Keycloak are deliberately absent from `readyz`. `architecture.md`
requires they report `DEGRADED`, never `DOWN` -- a directory outage must not pull
every web worker out of rotation.
"""
import logging

from django.conf import settings
from django.db import connections
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


@require_GET
def livez(request):
    """Process liveness. Deliberately dependency-free."""
    return JsonResponse({"status": "UP"})


@require_GET
def readyz(request):
    """Readiness: database, cache/broker and object store."""
    checks = {}
    ok = True

    # Database -- `default` only. The audit/reporting aliases are optional
    # dependencies; failing readiness because a replica is down would take the
    # whole instance out for a reporting-only feature.
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "UP"
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        logger.warning("readyz: database check failed: %s", exc)
        checks["database"] = f"DOWN: {type(exc).__name__}"
        ok = False

    # Cache -- also the Celery broker. A broker we cannot reach means queued
    # media work is not being accepted.
    try:
        from django.core.cache import cache

        cache.set("readyz", "ok", timeout=10)
        if cache.get("readyz") == "ok":
            checks["cache"] = "UP"
        else:
            checks["cache"] = "DOWN: read-after-write failed"
            ok = False
    except Exception as exc:  # noqa: BLE001
        logger.warning("readyz: cache check failed: %s", exc)
        checks["cache"] = f"DOWN: {type(exc).__name__}"
        ok = False

    # Object store -- HEAD on the configured bucket. SeaweedFS answers 403 to an
    # anonymous request; with valid credentials a missing bucket is what we want
    # to hear about.
    try:
        import boto3

        client = boto3.client(
            "s3",
            endpoint_url=settings.AWS_S3_ENDPOINT_URL,
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_S3_REGION_NAME,
        )
        client.head_bucket(Bucket=settings.AWS_STORAGE_BUCKET_NAME)
        checks["object_store"] = "UP"
    except Exception as exc:  # noqa: BLE001
        logger.warning("readyz: object store check failed: %s", exc)
        checks["object_store"] = f"DOWN: {type(exc).__name__}"
        ok = False

    return JsonResponse(
        {"status": "UP" if ok else "DOWN", "checks": checks},
        status=200 if ok else 503,
    )
