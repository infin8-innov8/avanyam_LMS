"""Django project package.

Exports the Celery application so that *every* process -- web, worker, beat and
test -- resolves the same configured app. Without this, ``shared_task`` binds to
Celery's throwaway ``default`` app, which never reads ``CELERY_BROKER_URL`` and
the rest of the Django-namespaced settings; ``.delay()`` then publishes to the
wrong broker and ``CELERY_TASK_ALWAYS_EAGER`` is ignored.

Importing ``config.celery`` here is safe during Django's own bootstrap: the
module only builds the app and defers reading settings to
``config_from_object``, so no settings access happens before Django is ready.
"""
from config.celery import app as celery_app

__all__ = ("celery_app",)
