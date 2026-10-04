"""URL map for the accounts context.

Two path changes came with Admin-only approval (D41), and both are deliberate
rather than cosmetic:

* `trainer/queue/` became `admin/queue/`. The old path was a lie the moment the
  queue stopped being per-trainer, and a stale bookmark landing on a 404 reads as
  a broken site rather than a withdrawn permission.
* `pending/` became `home/`. A pending account can no longer sign in at all, so
  there is no longer a "waiting for approval" page for a signed-in person to be
  held on; the landing page is just the landing page.

The old names are not aliased. A redirect from the old paths would keep them
alive in browser history and in bookmarks, which is the opposite of what this
change is for.
"""

from __future__ import annotations

from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", views.logout_view, name="logout"),
    # Two ways in, one form: yourself (anonymous) or someone else (staff).
    path("signup/", views.signup, name="signup"),
    path("create/", views.create_account, name="create-account"),
    path("signup/applied/", views.applied, name="applied"),
    path("home/", views.home, name="home"),
    path("password/", views.PasswordChangeViewForUser.as_view(), name="password-change"),
    # Admin-only. Every path under here is policy-gated inside the view, so a
    # guessed UUID gets the same refusal as a guessed URL.
    path("admin/queue/", views.admin_queue, name="admin-queue"),
    path("admin/queue/<uuid:request_pk>/", views.decide_request, name="decide"),
    path(
        "admin/queue/<uuid:request_pk>/undo/",
        views.undo_request_page,
        name="undo-request",
    ),
    path(
        "admin/queue/<uuid:request_pk>/undo/code/",
        views.request_undo_code_view,
        name="undo-code",
    ),
    path(
        "admin/queue/<uuid:request_pk>/undo/confirm/",
        views.undo_rejection_view,
        name="undo-confirm",
    ),
    path("admin/people/", views.role_management, name="role-management"),
    path(
        "admin/people/<uuid:user_pk>/role/",
        views.set_user_role,
        name="set-user-role",
    ),
]