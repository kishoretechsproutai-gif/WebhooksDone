from django.urls import path
from .views import inventory_reorder_report,get_slow_movers,get_risk_alerts,get_need_reordering
from .views import CompanyDashboardMetricsView,get_collections_list,get_collection_details
from .views import masterdatahub

urlpatterns = [
    path('inventory/reorder/report/', inventory_reorder_report, name='inventory_reorder_report'), #Forecast and SlowMovers
    path('inventory/slowmovers/', get_slow_movers, name='inventory_slowmovers_report'), #SlowMovers Data
    path('inventory/riskalert/', get_risk_alerts, name='inventory_risk_alerts'), #Risk Alerts
    path('inventory/Reorder/', get_need_reordering,name='inventory_need_reordering'), #Need Reordering
    path('dashboard/overview/',CompanyDashboardMetricsView ,name='company_dashboard_overview'), #Company Dashboard Overview
    path('collections/',get_collections_list, name='get_collections'), #Collections
    path("collections/<int:collection_id>/", get_collection_details, name="get_collection_details"),
    path('masterdatahub/',masterdatahub, name='master_data_hub_summary'), #Master Data Hub Summary
]
