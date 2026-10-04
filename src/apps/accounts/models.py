"""Accounts models: identity, roles, and the signup/approval queue.

Scope note -- `architecture.md` §3 assigns more to `accounts` (IdP mirror,
group->role mapping, notification preferences) and splits profile data into a
separate `people` context. This module implements the slice needed to run the
signup -> trainer-approval -> login loop; the remaining pieces stay out rather
than being half-modelled.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.accounts.domain.enums import (
    ACTIVE_STATUSES,
    BLOCKING_STATUSES,
    ApprovalStatus,
    AuthSource,
    InvalidTransition,
    assert_transition,
)
from apps.common.models import TimeStampedModel, UUIDModel

ROLE_ADMIN = "admin"
ROLE_TRAINER = "trainer"
ROLE_TRAINEE = "trainee"


class Role(UUIDModel, TimeStampedModel):
    """A grantable role.

    Roles live in our database, not in settings and not in Keycloak (§5 notes
    Keycloak holds *role mappings*, while authorization is enforced here). A
    boolean `is_trainer` column would push the authorization matrix into a
    hundred `if user.is_trainer` branches; this does not.
    """

    slug = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=64)

    class Meta:
        ordering = ("slug",)

    def __str__(self) -> str:
        return self.slug


class UserManager(BaseUserManager["User"]):
    """Email is the login identifier; there is no separate username.

    `create_user` deliberately does not create a User from a signup -- signup
    goes through `service.signup` so the SignupRequest audit row and the
    notification cannot be skipped.

    The convenience querysets live on the manager rather than coming from
    `UserQuerySet.from_queryset()`. A dynamically-generated manager cannot be
    serialized into a migration (`ValueError: Could not find manager
    UserManagerFromUserQuerySet`), which only surfaces once the model has
    concrete fields -- so the class is written out.
    """

    use_in_migrations = True

    def approved(self):
        return self.get_queryset().filter(approval_status=ApprovalStatus.APPROVED)

    def active(self):
        """Approved *and* actually able to sign in."""
        return self.approved().filter(is_active=True)

    def with_role(self, slug: str):
        return self.get_queryset().filter(role_assignments__role__slug=slug)

    def _create(self, email: str, password: str | None, **extra):
        if not email:
            raise ValueError("email is required")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        user.set_password(password)
        user.full_clean(exclude=["password"])
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        extra.setdefault("approval_status", ApprovalStatus.PENDING)
        extra.setdefault("auth_source", AuthSource.LOCAL)
        return self._create(email, password, **extra)

    def create_superuser(self, email: str, password: str, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("approval_status", ApprovalStatus.APPROVED)
        extra.setdefault("auth_source", AuthSource.LOCAL)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("superuser must have is_staff and is_superuser True")
        return self._create(email, password, **extra)


class User(UUIDModel, AbstractBaseUser, PermissionsMixin, TimeStampedModel):
    """A person in the LMS.

    `approval_status` is the gate: a pending account authenticates (so the
    person can be told what is happening) but every authorisation decision
    consults `policies.py`, which treats non-approved as inert.
    """

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255)

    auth_source = models.CharField(
        max_length=8,
        choices=[(s.value, s.value) for s in AuthSource],
        default=AuthSource.SIGNUP,
        help_text="How this identity was established. `local` is break-glass admin only.",
    )
    approval_status = models.CharField(
        max_length=10,
        choices=[(s.value, s.value) for s in ApprovalStatus],
        default=ApprovalStatus.PENDING,
        db_index=True,
    )
    decided_by = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="decided_users",
    )
    decided_at = models.DateTimeField(null=True, blank=True)

    #: Trainee picked this trainer as their approver. Only this trainer is
    #: notified and only this trainer may approve (see `policies.can_approve`).
    selected_trainer = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="trainees",
    )

    #: IdP mirror (§3: `accounts` holds the IdP mirror).
    oidc_subject = models.CharField(max_length=255, blank=True, default="")
    ldap_dn = models.CharField(max_length=512, blank=True, default="")

    #: Seeded accounts get a predictable password; force a real one before the
    #: account is useful.
    must_change_password = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)
    #: Staff is "can log into the Django admin", NOT "is an LMS trainer".
    #: `Role` records the LMS role; this flag only gates /admin/. Conflating the
    #: two is how an ordinary trainer ends up with admin-site access.
    is_staff = models.BooleanField(
        default=False,
        help_text="Grants access to the Django admin site. Not the LMS role.",
    )
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = ["full_name"]

    class Meta:
        ordering = ("full_name", "email")
        indexes = [models.Index(fields=["approval_status", "auth_source"])]

    def __str__(self) -> str:
        return f"{self.full_name} <{self.email}>"

    # -- approval state ---------------------------------------------------

    @property
    def is_approved(self) -> bool:
        return self.approval_status in ACTIVE_STATUSES

    def move_to(self, target: ApprovalStatus, *, by: "User | None" = None) -> None:
        """Apply an approval transition, validating the state machine."""
        current = ApprovalStatus(self.approval_status)
        target = ApprovalStatus(target)
        assert_transition(current, target)
        if current is ApprovalStatus.PENDING and target is ApprovalStatus.APPROVED:
            self.decided_at = timezone.now()
            self.decided_by = by
        self.approval_status = target
        self.save(
            update_fields=["approval_status", "decided_at", "decided_by", "updated_at"]
        )

    # -- roles ------------------------------------------------------------

    def has_role(self, slug: str) -> bool:
        return self.role_assignments.filter(role__slug=slug).exists()

    @property
    def is_trainer(self) -> bool:
        """Convenience for templates and policy internals only.

        Views must call `policies.*`; §7 forbids scattering this check.
        """
        return self.has_role(ROLE_TRAINER) or self.has_role(ROLE_ADMIN)


class RoleAssignment(UUIDModel, TimeStampedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="role_assignments")
    role = models.ForeignKey(Role, on_delete=models.CASCADE, related_name="assignments")
    assigned_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="roles_granted",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user", "role"], name="uniq_role_assignment"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user_id}:{self.role_id}"


class SignupRequest(UUIDModel, TimeStampedModel):
    """An application for access, and the durable record of the decision.

    §6.2: rows are retained after rejection and survive User deletion so a
    rejected email cannot silently re-register. Deleting the User therefore does
    not delete this row -- the two are deliberately independent FKs.
    """

    full_name = models.CharField(max_length=255)
    email = models.EmailField()
    selected_trainer = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="signup_requests",
    )
    status = models.CharField(
        max_length=10,
        choices=[(s.value, s.value) for s in ApprovalStatus],
        default=ApprovalStatus.PENDING,
        db_index=True,
    )
    decision_note = models.TextField(blank=True, default="")
    decided_by = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="signup_decisions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    #: Set when a decision promotes this request into a real, approved User.
    user = models.OneToOneField(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="signup_request",
    )

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            # One *open* application per email. This is what makes a rejected
            # address non-re-registerable without a separate blocklist.
            #
            # It has to be partial. It used to be an unconditional unique on
            # `email`, which made the row itself the blocklist -- and therefore
            # made "decline, but let them apply again" impossible to express,
            # since any second application for the address would collide.
            #
            # Rows in REDIRECTED are excluded on purpose: that decline exists to
            # let the address apply again, usually against a different trainer.
            # `service.signup.submit_application` is the only writer and it
            # refuses when a BLOCKING_STATUSES row exists, so relaxing the
            # constraint here does not open a second path around that check --
            # but note that the application-level check is now load-bearing, and
            # `test_a_redirected_address_may_apply_again_but_a_rejected_one_may_not`
            # is what keeps it honest.
            models.UniqueConstraint(
                fields=["email"],
                condition=~models.Q(status=ApprovalStatus.REDIRECTED),
                name="uniq_signup_email_open",
            ),
        ]
        indexes = [models.Index(fields=["selected_trainer", "status"])]

    def __str__(self) -> str:
        return f"{self.email} ({self.status})"

    @classmethod
    def blocking_for_email(cls, email: str) -> "SignupRequest | None":
        """The application currently preventing this address from applying again.

        Load-bearing now that `uniq_signup_email_open` excludes REDIRECTED rows:
        the database no longer stops a second application, so this check is the
        thing that does. Every path that would let a declined person back in --
        re-registration and the undo -- has to go through it.

        Most recent first, so a redirected-then-reapplied address reports the
        newer live application rather than the old redirect.
        """
        return (
            cls.objects.select_related("user", "selected_trainer", "decided_by")
            .filter(email__iexact=email, status__in=BLOCKING_STATUSES)
            .first()
        )

    @property
    def waiting_days(self) -> int:
        """Whole days this application has been sitting undecided.

        Measured to `decided_at` once there is one, otherwise to now, so the
        number keeps meaning "how long has this been waiting for a person" rather
        than freezing at the moment of the decision. 0 for anything already
        decided today, which is what a trainer expects to see.
        """
        end = self.decided_at or timezone.now()
        if end < self.created_at:
            # A decision timestamped before submission means edited or seeded
            # data. Reporting a negative wait would be worse than reporting none.
            return 0
        return (end - self.created_at).days

    def transition(self, target: ApprovalStatus, *, by: User | None = None) -> None:
        assert_transition(ApprovalStatus(self.status), ApprovalStatus(target))
        self.status = ApprovalStatus(target)
        self.decided_at = timezone.now()
        self.decided_by = by
        self.save(
            update_fields=["status", "decided_at", "decided_by", "updated_at"]
        )


#: How long an undo code stays redeemable, and how many wrong guesses it allows.
#:
#: Both live here rather than in `service.undo` because `ApprovalUndoToken.is_usable`
#: has to agree with the service about what "usable" means.
APPROVAL_UNDO_LIFETIME = timedelta(minutes=10)
APPROVAL_UNDO_MAX_ATTEMPTS = 5
#: Derived, not configured separately, so the lifetime and the number quoted to a
#: trainer in the UI and in their email cannot drift apart.
APPROVAL_UNDO_LIFETIME_MINUTES = int(APPROVAL_UNDO_LIFETIME.total_seconds() // 60)


class ApprovalUndoToken(TimeStampedModel):
    """A one-time code proving a trainer really is who they claim before a
    rejection is reversed.

    Why this exists: ``rejected`` is a strong statement about a person, and it was
    originally final. Reversing it therefore has to be harder than clicking a
    button -- a code mailed to the trainer's own address, single use,
    short-lived, and attempt-capped.

    Deliberately holds no plaintext code. ``code_hash`` is an HMAC under
    ``SECRET_KEY``, so a database disclosure does not hand an attacker a set of
    valid six-digit codes. The comparison happens in `service.undo`.

    Rows are never deleted on use. ``consumed_at`` is the audit trail, and the
    request it points at records what was undone and on whose authority.
    """

    request = models.ForeignKey(
        SignupRequest,
        on_delete=models.CASCADE,
        related_name="undo_tokens",
    )
    requested_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        related_name="requested_undo_tokens",
    )
    code_hash = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    attempts = models.PositiveSmallIntegerField(default=0)
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["request", "expires_at"])]

    def __str__(self) -> str:
        state = "consumed" if self.consumed_at else "open"
        return f"undo #{self.pk} for request {self.request_id} ({state})"

    @property
    def is_usable(self) -> bool:
        """Unconsumed, unexpired and under the attempt cap.

        Re-checked under a row lock inside the service: this property decides what
        to *tell* the user, not whether to act.
        """
        return (
            self.consumed_at is None
            and self.expires_at > timezone.now()
            and self.attempts < APPROVAL_UNDO_MAX_ATTEMPTS
        )


__all__ = [
    "APPROVAL_UNDO_LIFETIME",
    "APPROVAL_UNDO_LIFETIME_MINUTES",
    "APPROVAL_UNDO_MAX_ATTEMPTS",
    "ApprovalUndoToken",
    "InvalidTransition",
    "Role",
    "RoleAssignment",
    "ROLE_ADMIN",
    "ROLE_TRAINEE",
    "ROLE_TRAINER",
    "SignupRequest",
    "User",
    "UserManager",
]
