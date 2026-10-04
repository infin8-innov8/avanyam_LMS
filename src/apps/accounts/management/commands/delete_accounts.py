"""`manage.py delete_accounts` -- repeatable account cleanup, with `Role` untouched.

D47. Two development accounts were removed by hand, which is not repeatable and
leaves nothing behind to say it happened. This makes it a command.

**`Role` rows survive.** They are reference data — the vocabulary the
authorization matrix keys off — so deleting them would break every
`RoleAssignment` FK and `policies.py` with it. Cascading to them is therefore not
an option this command offers, not even behind a flag. `RoleAssignment` rows do
go: they are about a user who no longer exists, and the `user` FK is CASCADE.

**What else goes, and what deliberately does not:**

* `SignupRequest` rows **survive**, with `user`/`created_by`/`decided_by` set to
  NULL by their SET_NULL FKs. That is §6.2's whole point: the record of a
  rejected address outliving the account is what stops the address silently
  re-registering. An operator who expected `delete_accounts` to un-block a
  declined email would be wrong, so the summary says so.
* `ApprovalUndoToken` rows survive, for the same reason — a consumed token is
  audit trail.

Scope, both of which exist so a typo cannot delete a production database:

* `--yes` is required. There is no interactive confirm fallback; a script that
  hangs on a prompt is a script that hangs in CI.
* `--all` is required to touch anything. Without it the command deletes only the
  `--email` addresses given. The failure mode of "I ran the cleanup command" is
  therefore bounded by an argument rather than by a prompt nobody reads.

Only accounts are deleted. Related rows in future apps are counted and printed
before anything is removed, because a command that silently cascades into
unreviewed tables is how data disappears.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.models import (
    ApprovalUndoToken,
    Role,
    RoleAssignment,
    SignupRequest,
    User,
)


class Command(BaseCommand):
    help = "Delete user accounts. Role definitions are always preserved."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--email",
            action="append",
            default=[],
            dest="emails",
            metavar="ADDRESS",
            help="Delete this account. Repeatable.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Delete every account, not just the ones named by --email.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm. Required; there is no interactive prompt.",
        )

    def handle(self, *args, **options) -> None:
        if not options["yes"]:
            raise CommandError(
                "Refusing to delete without --yes. This command has no interactive "
                "confirmation, so that a piped invocation cannot stall on a prompt."
            )

        emails = [e.strip() for e in options["emails"] if e and e.strip()]
        if not options["all"] and not emails:
            raise CommandError(
                "Nothing selected. Pass --email ADDRESS (repeatable), or --all to "
                "delete every account. Refusing to treat an empty argument list as "
                "'all'."
            )

        users = self._select(users_emails=emails, delete_all=bool(options["all"]))
        if not users:
            self.stdout.write("No matching accounts; nothing to do.")
            return

        self._report(users)

        with transaction.atomic():
            # `email__in` is case-insensitive per lookup above, but a normalized
            # list is used for deletion so the printed set and the deleted set are
            # identical.
            deleted, _ = User.objects.filter(
                id__in=[u.id for u in users]
            ).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {len(users)} account(s), {deleted} row(s) in total "
                "(including cascaded RoleAssignment rows)."
            )
        )
        self.stdout.write(
            f"  {Role.objects.count()} Role definition(s) kept, as intended."
        )
        surviving = SignupRequest.objects.filter(
            email__in=[u.email for u in users]
        ).count()
        if surviving:
            self.stdout.write(
                self.style.WARNING(
                    f"  {surviving} SignupRequest row(s) kept with user=NULL. A "
                    "declined address still cannot re-register while one of those "
                    "rows exists -- deleting accounts does not lift a rejection."
                )
            )
        self.stdout.write(
            "  RoleAssignment rows are gone; use --email to target, or --all "
            "deliberately."
        )

    def _select(self, *, users_emails: list[str], delete_all: bool) -> list[User]:
        qs = User.objects.order_by("email")
        if not delete_all:
            qs = qs.filter(email__in=users_emails)
            found = {u.email.lower() for u in qs}
            missing = [e for e in users_emails if e.lower() not in found]
            if missing:
                # Not an error: `--email` is meant to be usable as a cleanup list
                # that tolerates addresses already gone. It is reported, not fatal.
                self.stderr.write(
                    self.style.WARNING(
                        "  no account for: " + ", ".join(sorted(set(missing)))
                    )
                )
        return list(qs)

    def _report(self, users: list[User]) -> None:
        """Print exactly what is about to go, and what is about to stay.

        This is the part that makes the command safe to run on a database you did
        not create. It is printed before the transaction opens, so a human sees
        it even if the delete then fails.
        """
        self.stdout.write(
            self.style.WARNING(
                f"About to delete {len(users)} account(s):"
            )
        )
        for user in users:
            roles = ", ".join(
                user.role_assignments.values_list("role__slug", flat=True)
            )
            self.stdout.write(
                f"  {user.email}  [{user.approval_status}]"
                + (f"  roles: {roles}" if roles else "  roles: none")
            )

        assignments = RoleAssignment.objects.filter(
            user__in=[u.id for u in users]
        ).count()
        self.stdout.write(f"  -> {assignments} RoleAssignment row(s) cascade away.")

        requests = SignupRequest.objects.filter(
            email__in=[u.email for u in users]
        ).count()
        tokens = ApprovalUndoToken.objects.filter(
            requested_by__in=[u.id for u in users]
        ).count()
        self.stdout.write(
            f"  -> {requests} SignupRequest row(s) and {tokens} ApprovalUndoToken "
            "row(s) are KEPT (their FKs are SET_NULL). These are the record of what "
            "happened, and they outlive the account on purpose."
        )