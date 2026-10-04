from __future__ import annotations

from django.urls import path

from apps.accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.PortalLoginView.as_view(), name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("signup/", views.signup, name="signup"),
    path("signup/applied/", views.applied, name="applied"),
    path("pending/", views.pending, name="pending"),
    path("password/", views.FirstPasswordChangeView.as_view(), name="password-change"),
    path("trainer/queue/", views.trainer_queue, name="trainer-queue"),
    path("trainer/queue/<uuid:request_pk>/", views.decide_request, name="decide"),
    path(
        "trainer/queue/<uuid:request_pk>/undo/code/",
        views.request_undo_code_view,
        name="undo-code",
    ),
    path(
        "trainer/queue/<uuid:request_pk>/undo/confirm/",
        views.undo_rejection_view,
        name="undo-confirm",
    ),
]
