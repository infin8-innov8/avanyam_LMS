"""Settings package.

Import the *environment* module, never this one:

    DJANGO_SETTINGS_MODULE=config.settings.dev     # this laptop
    DJANGO_SETTINGS_MODULE=config.settings.staging  # production-shaped
    DJANGO_SETTINGS_MODULE=config.settings.prod     # real deployment

``base`` holds everything common and is never selected directly -- it has no
``DEBUG`` and no ``ALLOWED_HOSTS`` of its own, so importing it by mistake fails
loudly rather than silently defaulting to development behaviour.
"""
