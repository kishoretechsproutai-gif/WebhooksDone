import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Trooba2.settings")

app = Celery("Trooba2")

app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()



# Use string path to task instead of importing the function
app.conf.beat_schedule = {
    "inventory-prediction-sunday": {
        "task": "InventoryManagement.views.process_inventory_for_tenant",  # string, no import
        "schedule": crontab(hour=14, minute=0, day_of_week=0),
        "args": (),  # args optional; tenant loop inside task
    }
}