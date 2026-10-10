from django.db import migrations

# Kept as literals so the migration stays self-contained: importing app code
# here would couple schema evolution to whatever the app looks like today.
ANALYST_GROUP = "analyst"
ADMINISTRATOR_GROUP = "administrator"

ROLE_GROUPS = [ANALYST_GROUP, ADMINISTRATOR_GROUP]


def create_role_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for name in ROLE_GROUPS:
        Group.objects.get_or_create(name=name)


def remove_role_groups(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=ROLE_GROUPS).delete()


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(create_role_groups, remove_role_groups),
    ]
