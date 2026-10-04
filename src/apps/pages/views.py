"""Public pages: what an unauthenticated visitor may read without an account.

Deliberately not part of `apps.accounts`. Accounts owns a bounded context
(identity and the approval gate in front of it); the home page owns none, and
folding a cacheable public GET into the same app as the sign-in form would put
the two on the same authorization story for no gain.
"""

from __future__ import annotations

from django.views.decorators.cache import never_cache
from django.views.generic import TemplateView


class HomeView(TemplateView):
    template_name = "pages/home.html"


# `never_cache` rather than a short max-age: the masthead renders the signed-in
# navigation for an authenticated visitor, so any shared cache entry for `/`
# could hand one visitor the chrome belonging to somebody else's session.
home = never_cache(HomeView.as_view())