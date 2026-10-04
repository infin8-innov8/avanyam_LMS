"""Force a password change for accounts issued a placeholder password.

Placed early in MIDDLEWARE so a seeded account cannot reach any view -- including
the logout endpoint -- until it has replaced the predictable password it was
issued. The allow-list is deliberately tiny: change-password, admin, static, and
the health probes (so an orchestrator does not get a 302 and report the service
as unhealthy).
"""

from __future__ import annotations

from django.conf import settings
from django.shortcuts import redirect
from django.urls import Resolver404, resolve, reverse

EXEMPT_NAMES: frozenset[str] = frozenset(
    {
        "accounts:password-change",
        "accounts:logout",
        "admin:logout",
    }
)


class MustChangePasswordMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if (
            user is not None
            and user.is_authenticated
            and getattr(user, "must_change_password", False)
            and not self._exempt(request)
        ):
            return redirect(f"{reverse('accounts:password-change')}?next={request.path}")
        return self.get_response(request)

    def _exempt(self, request) -> bool:
        if request.path.startswith(("/static/", "/media/", "/livez", "/readyz")):
            return True

        static_url = getattr(settings, "STATIC_URL", None)
        if static_url and request.path.startswith(static_url):
            return True

        # Resolve the path HERE rather than reading request.resolver_match.
        # This middleware runs in the request phase, which is *before* Django's
        # URLResolver runs, so request.resolver_match is always None at this
        # point. An earlier version returned False in that case, which meant
        # EXEMPT_NAMES never matched anything and logout was unreachable for
        # exactly the accounts this middleware exists to contain: a
        # placeholder-password user pressing "log out" was bounced straight
        # back to the password page, still authenticated, forever.
        try:
            match = resolve(request.path_info)
        except Resolver404:
            # Unknown path: not exempt. Bounce placeholder-password accounts away
            # from a 404 page that might carry content.
            return False
        return match.view_name in EXEMPT_NAMES
