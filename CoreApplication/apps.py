from django.apps import AppConfig

class CoreApplicationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'CoreApplication'

    def ready(self):
        # Force import of views so Celery picks up tasks defined there
        import CoreApplication.views
