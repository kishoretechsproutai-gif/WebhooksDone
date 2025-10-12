from django.urls import path
from . import views

urlpatterns = [
    path("debug-sku-payload/", views.forecast_single_sku, name="debug_single_sku_payload"), #Testing
    path("ManualAdminForeCast/",views.manual_forecast, name="ManualAdminForeCast"), #AdminManualForeCast
    
    path("TestingInventoryValuation/",views.calculate_inventory_value_from_shopify, name="TestingInventoryValuation"), #TestingInventoryValuation
    path("TestingMetricsDemo/metrics/", views.demo_calculate_metrics,name="TestingMetricsDemo"), #TestingMetricsDemo

]
