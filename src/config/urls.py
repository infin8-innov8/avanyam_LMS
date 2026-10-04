"""Root URLconf.

`/` is the login page, not a marketing splash: the product is an internal LMS
and every visitor needs an account, so the root is where someone already
authenticated lands or, more often, where an anonymous one starts signing in.

Mount point note: `include()` inherits the *including* file's prefix, so
`path("accounts/", include("apps.accounts.urls"))` mounts that app's own
`login/` and `signup/` under `/accounts/`. Mounting it at `""` silently drops
the prefix and serves them at `/login/`, which then makes every
`{% url 'accounts:login' %}` in the templates render a path the site does not
serve. Declare the prefix here, in exactly one place.
"""
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from config import health

urlpatterns = [
    path("accounts/", include("apps.accounts.urls")),
    path("admin/", admin.site.urls),
    path("livez", health.livez, name="livez"),
    path("readyz", health.readyz, name="readyz"),
    # Kept for parity with the load-balancer contract even though `/livez` is
    # the documented name; a probe configured against the old path keeps working.
    path("healthz", health.livez, name="healthz"),
    path("", RedirectView.as_view(pattern_name="accounts:login", permanent=False)),
    path("favicon.ico", RedirectView.as_view(url="/static/favicon.ico", permanent=False)),
]
