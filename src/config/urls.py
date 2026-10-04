"""Root URLconf.

`/` is the public front door: a visitor reads what the platform is before being
asked for an account. It used to redirect straight to the login page, which was
right while the product had one screen and wrong once there was something to
say about it. Signing in stays one click away, and `/accounts/login/` is
unchanged for anybody who already had it bookmarked.

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
    path("", include("apps.pages.urls")),
    path("favicon.ico", RedirectView.as_view(url="/static/favicon.ico", permanent=False)),
]
