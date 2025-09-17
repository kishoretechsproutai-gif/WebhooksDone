from django.urls import path
from .views import TriggerInventoryPredictionView
from .views import Predictions
from .views import TestSingleSKUForecast

urlpatterns = [
     path('trigger-inventory/', TriggerInventoryPredictionView, name='trigger-inventory'),
     path('Predictions/', Predictions, name='Predictions'),
     path('testingonesku/',TestSingleSKUForecast),
    ]
