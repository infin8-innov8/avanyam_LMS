"""Seed the people named in the project brief.

Idempotent: safe to re-run.

**Roles are granted only to approved accounts.** A pending account holds no
RoleAssignment, because signup grants none and the invariant is that an account
becomes useful exactly when it becomes approved. The old version of this command
gave every seeded account a role *and* set `must_change_password`, which meant the
seed data described a state the application cannot produce.

**No account is issued a predictable password.** `must_change_password` is gone
(D43) and nothing replaced it, because there is no forced change to satisfy: a
shared placeholder was a credential several people knew. So this command takes
one explicitly-supplied password and refuses to invent a pattern from a name. The
password comes from the environment, never from this file.

Trained/pending split:

* trainers are created **approved** with the trainer role -- otherwise nobody can
  demonstrate the queue;
* trainees are created **pending** with no role, and their SignupRequest carries
  the role they asked for.
"""

from __future__ import annotations

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.domain.enums import ApprovalStatus, AuthSource, RequestedRole
from apps.accounts.models import (
    ROLE_TRAINEE,
    ROLE_TRAINER,
    Role,
    RoleAssignment,
    SignupRequest,
    User,
)

#: example.com addresses, per RFC 2606: reserved for documentation and never
#: deliverable. The real roster is deployment data and belongs in .env, not
#: in a public repository -- a colleague's address should not be published
#: by a code push.
TRAINERS = [
    ("Trainer One", "trainer1@example.com"),
    ("Trainer Two", "trainer2@example.com"),
]

TRAINEES = [
    ("Trainee One", "trainee1@example.com"),
    ("Trainee Two", "trainee2@example.com"),
    ("Trainee Three", "trainee3@example.com"),
    ("Trainee Four", "trainee4@example.com"),
    ("Trainee Five", "trainee5@example.com"),
    ("Trainee Six", "trainee6@example.com"),
]


class Command(BaseCommand):
    help = "Create the trainers and trainees named in the project brief."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--password",
            default=os.environ.get("ACCOUNTS_SEED_PASSWORD", ""),
            help=(
                "One password for every seeded account. Reads "
                "ACCOUNTS_SEED_PASSWORD from the environment by default."
            ),
        )
        parser.add_argument(
            "--approve-trainees",
            action="store_true",
            help=(
                "Approve the trainees too and grant them the trainee role. Off by "
                "default: leaving them pending is what makes the queue demonstrable."
            ),
        )

    def handle(self, *args, **options) -> None:
        password = options["password"]
        if not password:
            raise CommandError(
                "Refusing to seed without a password: set ACCOUNTS_SEED_PASSWORD in "
                "the environment or pass --password. There is no default. Every "
                "seeded account shares this one value, so use it for a demo "
                "database only -- 'manage.py createadmin' is the per-person path."
            )

        approve_trainees = options["approve_trainees"]
        created, updated = 0, 0

        with transaction.atomic():
            trainer_role = self._role(ROLE_TRAINER, "Trainer")
            trainee_role = self._role(ROLE_TRAINEE, "Trainee")

            for full_name, email in TRAINERS:
                _, was_created = self._upsert(
                    full_name=full_name,
                    email=email,
                    password=password,
                    approval_status=ApprovalStatus.APPROVED,
                    # Approved, so the role goes on now. `assigned_by=None` because
                    # nobody performed this approval -- it is seed data, not a
                    # decision, and inventing an actor would put a name in the
                    # audit trail that never acted.
                    role=trainer_role,
                    requested_role=RequestedRole.TRAINER,
                )
                created += was_created
                updated += not was_created

            for full_name, email in TRAINEES:
                trainee_status = (
                    ApprovalStatus.APPROVED
                    if approve_trainees
                    else ApprovalStatus.PENDING
                )
                _, was_created = self._upsert(
                    full_name=full_name,
                    email=email,
                    password=password,
                    approval_status=trainee_status,
                    # None while pending. Passing the role unconditionally would
                    # hand a pending account a capability, which is the invariant
                    # this command exists to stop breaking.
                    role=trainee_role if approve_trainees else None,
                    requested_role=RequestedRole.TRAINEE,
                )
                created += was_created
                updated += not was_created

        self.stdout.write(
            self.style.SUCCESS(f"seeded: {created} created, {updated} updated")
        )
        self.stdout.write(f"  trainers: {len(TRAINERS)}  trainees: {len(TRAINEES)}")
        if approve_trainees:
            self.stdout.write("  trainees are approved and hold the trainee role")
        else:
            self.stdout.write(
                "  trainees are pending, hold no role, and cannot sign in until an "
                "admin approves them"
            )
        self.stdout.write(
            self.style.WARNING(
                "  every seeded account shares one password -- this is demo data"
            )
        )

    @staticmethod
    def _role(slug: str, name: str) -> Role:
        row, _ = Role.objects.get_or_create(slug=slug, defaults={"name": name})
        return row

    def _upsert(
        self,
        *,
        full_name: str,
        email: str,
        password: str,
        approval_status: ApprovalStatus,
        role: Role | None,
        requested_role: RequestedRole,
    ) -> tuple[User, bool]:
        email = User.objects.normalize_email(email).strip()

        user = User.objects.filter(email__iexact=email).first()
        was_created = user is None

        if user is None:
            user = User(
                email=email,
                full_name=full_name,
                # These accounts authenticate against the directory/IdP in the
                # target topology; `signup` records that they arrived through the
                # portal rather than being break-glass local admins.
                auth_source=AuthSource.SIGNUP,
            )
        user.full_name = full_name
        user.approval_status = approval_status
        user.set_password(password)
        user.save()

        if role is not None:
            RoleAssignment.objects.get_or_create(
                user=user, role=role, defaults={"assigned_by": None}
            )

        # A pending applicant's row is what puts them in the queue, so it is
        # created only if absent: `SignupRequest` is the durable record of a
        # decision and must not be reset by a re-run of seed data.
        SignupRequest.objects.get_or_create(
            email=email,
            defaults={
                "full_name": full_name,
                "requested_role": requested_role,
                "status": approval_status,
                "user": user,
            },
        )
        return user, was_created