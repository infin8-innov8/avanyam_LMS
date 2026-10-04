"""Shared model conventions.

`architecture.md` §16.1 puts a `common/` app at the root of the app package, so
this module is the seed of it rather than a per-app copy of the same two
abstract base classes.
"""

from __future__ import annotations

import uuid

from django.db import models


class UUIDModel(models.Model):
    """Primary key is a client-generatable UUID, not a sequential integer.

    Sequential integer PKs leak row counts through 404s and make any future merge
    of two datasets require an ID remap. UUIDs let the signup form mint a row on
    the client and still be idempotent.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    """Adds created/updated timestamps.

    ``auto_now`` on update means a row that a bulk ``update()`` touches without
    touching ``updated_at`` is a bug -- prefer ``save()`` for these rows.
    """

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True
