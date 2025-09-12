from django.urls import path
from .views import TriggerInventoryPredictionView
urlpatterns = [
     path('trigger-inventory/', TriggerInventoryPredictionView, name='trigger-inventory'),
    ]
