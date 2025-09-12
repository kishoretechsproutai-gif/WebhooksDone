from django.contrib import admin
from .models import InventoryPrediction

@admin.register(InventoryPrediction)
class InventoryPredictionAdmin(admin.ModelAdmin):
    list_display = ('product_name', 'sku', 'category', 'price', 'stock', 'on_order', 'reorder', 'week_start_date')
    search_fields = ('product_name', 'sku', 'category')
    list_filter = ('week_start_date', 'category')

