"""Route the audit and reporting apps to their own databases.

`architecture.md` §14.3 gives each role one job, and the roles are separate
PostgreSQL logins with separate grants -- so routing has to be explicit. A model
in an app named `audit` or `reporting` goes to that database; everything else,
including `migrate`, stays on `default`.

`migrate` is a full alias of `default` rather than a separate target. Django's
migration executor writes `django_migrations` to whichever database it migrates,
and pointing that at a second physical database would give the two aliases
separate migration histories for one schema -- a silent, confusing failure. The
`avanyam_migrate` *role* is still what runs `migrate` (see the `migrate` alias in
`settings/base.py`); the router simply does not split the schema.
"""

_AUDIT_APPS = frozenset({"audit"})
_REPORTING_APPS = frozenset({"reporting"})


class AuditAndReportingRouter:
    def db_for_read(self, model, **hints):
        return self._db_for(model)

    def db_for_write(self, model, **hints):
        return self._db_for(model)

    def allow_relation(self, obj1, obj2, **hints):
        # Never allow a cross-database relation. If one is ever needed it has to
        # be a deliberate copy, not a lazy FK.
        return self._db_for(obj1) == self._db_for(obj2)

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == "audit":
            return app_label in _AUDIT_APPS
        if db == "reporting":
            return app_label in _REPORTING_APPS
        # The migrate/audit/reporting aliases must not grow tables of their own.
        return app_label not in (_AUDIT_APPS | _REPORTING_APPS)

    @staticmethod
    def _db_for(obj):
        label = getattr(obj, "_meta", None)
        label = label.app_label if label is not None else type(obj).__module__
        if label in _AUDIT_APPS:
            return "audit"
        if label in _REPORTING_APPS:
            return "reporting"
        return "default"
