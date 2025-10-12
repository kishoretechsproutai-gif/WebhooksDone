import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Trooba2.settings")

app = Celery("Trooba2")

app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()
app.conf.timezone = "Asia/Kolkata"


# Use string path to task instead of importing the function
app.conf.beat_schedule = {
    # Existing Sunday inventory task
    "inventory-prediction-sunday": {
        "task": "InventoryManagement.views.process_inventory_for_tenant",
        "schedule": crontab(hour=14, minute=0, day_of_week=0),
        "args": (),
    },

    # New monthly SKU forecast task for 28th
    "monthly-sku-forecast-28th": {
        "task": "CoreApplication.views.run_monthly_forecast",  # string path to your view function
        "schedule": crontab(hour=2, minute=0, day_of_month=28),
        "args": (),  # no args; the function handles looping through companies
    }
}
