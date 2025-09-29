from django.urls import path
from . import views

urlpatterns = [
    path("forecast/run/", views.forecast_top5_skus, name="run_forecasting"),
    
    path("predictions/chart/", views.prediction_chart_view, name="prediction_chart"),

]
