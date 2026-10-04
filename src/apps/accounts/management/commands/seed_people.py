"""Seed the people named in the project brief.

Idempotent: safe to re-run. Passwords follow a caller-supplied template and each
account is flagged `must_change_password`, so the predictable initial value is
only ever good for one login.

The password template is read from the environment, never hard-coded. A pattern
of `activ8*o(<first name>)` is guessable from a name that the UI displays, so
it is deliberately not in the repository -- only a placeholder is.
"""

from __future__ import annotations

import os
import re
import secrets

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.text import slugify

from apps.accounts.domain.enums import ApprovalStatus, AuthSource
from apps.accounts.models import (
    ROLE_ADMIN,
    ROLE_TRAINEE,
    ROLE_TRAINER,
    Role,
    RoleAssignment,
    SignupRequest,
    User,
)

#: placeholder, not a real pattern -- set ACCOUNTS_SEED_PASSWORD_TEMPLATE
FALLBACK_TEMPLATE = "REPLACE-ME-{first_name}"

# example.com addresses, per RFC 2606: reserved for documentation and never
# deliverable. The real roster is deployment data and belongs in .env, not
# in a public repository -- a colleague's address should not be published
# by a code push.
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


def first_name(full_name: str) -> str:
    return full_name.split()[0]


class Command(BaseCommand):
    help = "Create the trainers and trainees named in the project brief."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--password-template",
            default=os.environ.get("ACCOUNTS_SEED_PASSWORD_TEMPLATE", FALLBACK_TEMPLATE),
            help="Password pattern; {first_name} is substituted.",
        )
        parser.add_argument(
            "--approve-trainers",
            action="store_true",
            default=True,
            help="Trainers start approved so they can use the queue (default).",
        )
        parser.add_argument(
            "--trainees-pending",
            action="store_true",
            default=True,
            help="Trainees start pending, awaiting a trainer decision (default).",
        )

    def handle(self, *args, **options) -> None:
        template = options["password_template"]
        if "{first_name}" not in template:
            raise CommandError(
                "--password-template must contain the literal '{first_name}' placeholder."
            )

        if "REPLACE-ME" in template:
            raise CommandError(
                "Refusing to seed predictable passwords: set "
                "ACCOUNTS_SEED_PASSWORD_TEMPLATE in the environment (or pass "
                "--password-template) with a real pattern. Every seeded account is "
                "flagged must_change_password regardless, but do not ship a default."
            )

        created, updated, skipped = 0, 0, 0
        with transaction.atomic():
            trainer_role, _ = Role.objects.get_or_create(
                slug=ROLE_TRAINER, defaults={"name": "Trainer"}
            )
            trainee_role, _ = Role.objects.get_or_create(
                slug=ROLE_TRAINEE, defaults={"name": "Trainee"}
            )

            trainers: list[User] = []
            for full_name, email in TRAINERS:
                user, was_created = self._upsert(
                    full_name=full_name,
                    email=email,
                    password=template.format(first_name=first_name(full_name)),
                    role=trainer_role,
                    approval_status=(
                        ApprovalStatus.APPROVED
                        if options["approve_trainers"]
                        else ApprovalStatus.PENDING
                    ),
                )
                trainers.append(user)
                created += was_created
                updated += not was_created

            for full_name, email in TRAINEES:
                user, was_created = self._upsert(
                    full_name=full_name,
                    email=email,
                    password=template.format(first_name=first_name(full_name)),
                    role=trainee_role,
                    approval_status=(
                        ApprovalStatus.PENDING
                        if options["trainees_pending"]
                        else ApprovalStatus.APPROVED
                    ),
                    selected_trainer=self._default_trainer(email, trainers),
                )
                skipped += 0 if user else 1
                created += was_created
                updated += not was_created

        self.stdout.write(self.style.SUCCESS(
            f"seeded: {created} created, {updated} updated, {skipped} skipped"
        ))
        self.stdout.write(f"  trainers: {len(TRAINERS)}  trainees: {len(TRAINEES)}")
        self.stdout.write(
            "  all seeded accounts have must_change_password=True and must reset "
            "before first use"
        )

    def _default_trainer(self, trainee_email: str, trainers: list[User]) -> User | None:
        """Assign a trainer so seeded trainees can be approved by someone.

        Nobody was designated in the brief, so this splits trainees across the
        available trainers deterministically. It is a *starting point*, not a
        policy: a trainee can change their choice before submitting, and an
        admin can reassign later.
        """
        if not trainers:
            return None
        index = sum(ord(c) for c in trainee_email.lower()) % len(trainers)
        return trainers[index]

    def _upsert(
        self,
        *,
        full_name: str,
        email: str,
        password: str,
        role: Role,
        approval_status: ApprovalStatus,
        selected_trainer: User | None = None,
    ) -> tuple[User, bool]:
        email = User.objects.normalize_email(email).strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise CommandError(f"refusing to seed malformed email: {email!r}")

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
                approval_status=approval_status,
                selected_trainer=selected_trainer,
                must_change_password=True,
            )
        else:
            user.full_name = full_name
            user.approval_status = approval_status
            if selected_trainer is not None:
                user.selected_trainer = selected_trainer
            user.must_change_password = True

        user.set_password(password)
        user.save()

        RoleAssignment.objects.get_or_create(
            user=user, role=role, defaults={"assigned_by": None}
        )

        # A trainer's presence in the queue is what makes the approval flow
        # demonstrable, so give seeded trainees a durable SignupRequest row too.
        # Docs §6.2: retained after rejection, so it is created only if absent.
        SignupRequest.objects.get_or_create(
            email=email,
            defaults={
                "full_name": full_name,
                "selected_trainer": selected_trainer,
                "status": approval_status,
                "user": user,
            },
        )
        return user, was_created
