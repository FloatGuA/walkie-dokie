from django.db import migrations, models
from django.db.models import F


def copy_party_group_from_existing_name(apps, schema_editor):
    for model_name in ("BoqImportSpec", "BoqItemRecord", "BoqSummaryRecord"):
        model = apps.get_model("contract_intelligence", model_name)
        model.objects.update(party_a_group=F("party_a_name"))


class Migration(migrations.Migration):

    dependencies = [
        (
            "contract_intelligence",
            "0007_boqimportspec_boqimportrun_boqitemrecord_and_more",
        ),
    ]

    operations = [
        migrations.AlterField(
            model_name="boqimportspec",
            name="profile",
            field=models.CharField(
                choices=[
                    ("crland_general_v1", "华润通用工程清单 v1"),
                    (
                        "crland_lighting_xls_v1",
                        "华润商业泛光照明 XLS v1",
                    ),
                ],
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="boqimportspec",
            name="party_a_group",
            field=models.CharField(default="", max_length=500),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="boqitemrecord",
            name="party_a_group",
            field=models.CharField(default="", max_length=500),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="boqsummaryrecord",
            name="party_a_group",
            field=models.CharField(default="", max_length=500),
            preserve_default=False,
        ),
        migrations.RunPython(
            copy_party_group_from_existing_name,
            migrations.RunPython.noop,
        ),
        migrations.AddIndex(
            model_name="boqitemrecord",
            index=models.Index(
                fields=["party_a_group", "status", "kind"],
                name="contract_boq_party_st_kind",
            ),
        ),
        migrations.AddIndex(
            model_name="boqsummaryrecord",
            index=models.Index(
                fields=["party_a_group", "status"],
                name="contract_boq_sum_party_st",
            ),
        ),
    ]
