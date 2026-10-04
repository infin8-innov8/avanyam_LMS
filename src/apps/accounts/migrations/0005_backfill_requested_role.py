"""Backfill `SignupRequest.requested_role` from the role each account was granted.

`0004` added `requested_role` with a `default='trainee'`, which is right for every
*new* application but wrong as a description of history. Two applications on this
host were approved as trainers, so the backfill would have labelled them `trainee`
-- a record of what was asked for that contradicts the role actually granted, in
the one table whose job is to be the audit trail (D51: do not render a row as
something it is not).

We cannot recover what those applicants originally typed, because before D41 they
nominated a *trainer* and never chose a role at all. The granted role is the
best available evidence, and it is a real role the account holds rather than a
guess about intent.

Only *approved* requests are touched, and only where the linked account currently
holds exactly one role. An approved application with no linked account, or with
several roles, is left at the default rather than guessed at -- and that residual
inaccuracy is recorded here rather than hidden:

* a missing link means the account was deleted (D47 keeps the request row), and
  there is nothing left to read a role from;
* multiple roles means the account changed after approval, so "what was requested"
  and "what they hold now" have genuinely diverged.

Deliberately not reversible in the usual sense: it is idempotent (it only writes
rows still at the default) and it corrects data rather than destroying it, so no
`reverse_code` is provided. Reversing would mean relabelling history incorrectly,
which is the state this migration exists to leave.
"""

from django.db import migrations


def backfill_requested_role(apps, schema_editor):
    SignupRequest = apps.get_model("accounts", "SignupRequest")
    RoleAssignment = apps.get_model("accounts", "RoleAssignment")

    for request in SignupRequest.objects.filter(
        status="approved", user__isnull=False
    ).iterator():
        roles = list(
            RoleAssignment.objects.filter(user_id=request.user_id).values_list(
                "role__slug", flat=True
            )
        )
        if len(roles) != 1:
            continue
        if roles[0] not in {"trainee", "trainer"}:
            continue
        request.requested_role = roles[0]
        request.save(update_fields=["requested_role"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_requested_role_drop_forced_password"),
    ]

    operations = [
        migrations.RunPython(backfill_requested_role, migrations.RunPython.noop),
    ]