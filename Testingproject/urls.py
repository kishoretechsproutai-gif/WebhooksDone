from django.urls import path
from . import views

urlpatterns = [
    path("forecast/run/", views.forecast_top5_skus, name="run_forecasting"),
    path("WMAPE/",views.WMAPE_Calculation, name="WMAPE_Calculation"),
    path("Median_WMAPE/",views.median_metrics_chart_view, name="Median_WMAPE_Calculation"),
    path("Monthwise_prdictions/",views.sku_predictions_per_month, name="Monthwise_prdictions"),

]
