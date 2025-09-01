import os
from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "Trooba2.settings")

app = Celery("Trooba2")

app.config_from_object("django.conf:settings", namespace="CELERY")

app.autodiscover_tasks()