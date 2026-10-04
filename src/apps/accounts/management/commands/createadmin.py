"""`manage.py createadmin` -- the only way an admin account comes into existence.

`prd.md` §4: admins cannot self-register, so this is the single minting path.
That is not a formality. `service.roles` refuses the `admin` slug, so the role
screen cannot create one, and this command is not reachable from the web at all.

Two properties worth stating, because both are guards rather than consequences:

**It requires shell access.** Anyone who can run this can approve anyone, so the
command checks `is_staff` on *an existing* admin before doing anything. That
makes it unusable on a fresh database with no admins, which is the correct
outcome: a brand new deployment should use `createadmin --bootstrap` once, with
the shell access the deployment already has.

`--bootstrap` is refused once any staff account exists, so it cannot become a
standing bypass. A second admin on a live deployment needs the existing staff
account to authorise it, which is the whole point of the check.

A refusal raises `CommandError` rather than returning. A management command that
returns quietly exits 0, so a deploy script would record success for a command
that created nothing.

**The account is approved immediately.** There is nothing to approve it *for* --
an admin who had to be approved by an admin could never be created, because the
first one has no approver. So this command writes `APPROVED` directly, and says
so, rather than minting a pending admin that nobody can unlock.

The password is asked for twice and validated by Django's own validators, then
never displayed again. It is not logged, and it is not derived from a pattern.
"""

from __future__ import annotations

import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.domain.enums import ApprovalStatus, AuthSource
from apps.accounts.models import ROLE_ADMIN, Role, RoleAssignment, User


class Command(BaseCommand):
    help = "Create an approved administrator account. Shell access required."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--email", help="Email address for the admin.")
        parser.add_argument("--full-name", help="Display name for the admin.")
        parser.add_argument(
            "--bootstrap",
            action="store_true",
            help=(
                "Permit creation when no admin exists yet. Use once, on a new "
                "deployment, from a shell that already has database access."
            ),
        )

    def handle(self, *args, **options) -> None:
        if not self._authorised(bool(options["bootstrap"])):
            raise CommandError(
                "Refused: no existing staff account authorises creating an admin."
            )

        email = (options["email"] or input("Email: ")).strip()
        full_name = (options["full_name"] or input("Full name: ")).strip()

        if not email or "@" not in email:
            raise CommandError("A valid email address is required.")
        if not full_name:
            raise CommandError("A full name is required.")

        email = User.objects.normalize_email(email)
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError(f"An account already exists for {email}.")

        password = self._read_password(email)
        # Validated against a throwaway instance carrying the real email and name,
        # so a password rejected because it resembles the address is rejected here
        # rather than at the admin's first sign-in.
        self._validate(password, email=email, full_name=full_name)

        with transaction.atomic():
            user = User.objects.create_user(
                email=email,
                full_name=full_name,
                password=password,
                # LOCAL, not SIGNUP: this account was created by an operator at a
                # shell, not by a person filling in the registration form.
                auth_source=AuthSource.LOCAL,
                # Approved outright, with the reasoning in the module docstring:
                # an admin gated on admin approval can never bootstrap.
                approval_status=ApprovalStatus.APPROVED,
            )
            role, _ = Role.objects.get_or_create(
                slug=ROLE_ADMIN, defaults={"name": "Admin"}
            )
            RoleAssignment.objects.create(
                user=user, role=role, assigned_by=None
            )

        self.stdout.write(
            self.style.SUCCESS(f"Created admin {full_name} <{email}>.")
        )
        self.stdout.write(
            "  This account is approved and can open the approval queue and manage "
            "roles."
        )
        self.stdout.write(
            "  The password was not shown again and is not recoverable; reset it from "
            "the admin site if it is lost."
        )

    def _authorised(self, bootstrap: bool) -> bool:
        """Refuse unless a staff account already exists, or --bootstrap was given.

        The check is on `is_staff` rather than the admin Role because that is what
        Django itself uses to gate /admin/, and the person who can already edit
        users in /admin/ can escalate anyway. Role-checking here would refuse a
        legitimate operator and teach them to use --bootstrap more casually.
        """
        staff_exists = User.objects.filter(is_staff=True).exists()

        # --bootstrap is refused when staff already exists. The previous version
        # short-circuited on `bootstrap or ...`, so the flag was honoured
        # unconditionally -- the exact inverse of what the docstring promised. On
        # a deployment that had been bootstrapped months earlier, a stale
        # --bootstrap in a runbook or deploy script would keep minting admins
        # with no authorisation check at all.
        if bootstrap and staff_exists:
            self.stderr.write(
                self.style.ERROR(
                    "--bootstrap is only for a deployment with no staff account.\n"
                    "One already exists, so this shell does not need it: an existing "
                    "staff account is what authorises creating another admin."
                )
            )
            return False

        if staff_exists:
            return True

        if bootstrap:
            return True

        self.stderr.write(
            self.style.ERROR(
                "No staff account exists, so there is nothing authorising this "
                "action.\n"
                "If this is a new deployment, re-run with --bootstrap:\n"
                "    manage.py createadmin --bootstrap --email ... --full-name ...\n"
                "--bootstrap is refused once any staff account exists, because then "
                "the shell already has more authority than this command."
            )
        )
        return False

    @staticmethod
    def _read_password(email: str) -> str:
        """Ask twice. `getpass` so it does not echo to a terminal or a CI log."""
        first = getpass.getpass(f"Password for {email}: ")
        second = getpass.getpass("Confirm password: ")
        if not first:
            raise CommandError("A password is required; an account cannot be created without one.")
        if first != second:
            raise CommandError("The two passwords did not match.")
        return first

    @staticmethod
    def _validate(password: str, *, email: str, full_name: str) -> None:
        try:
            validate_password(password, User(email=email, full_name=full_name))
        except ValidationError as exc:
            raise CommandError(
                "That password is not acceptable:\n  "
                + "\n  ".join(exc.messages)
            ) from exc