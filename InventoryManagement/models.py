from django.db import models
from CoreApplication.models import CompanyUser, ProductVariant



class InventoryPrediction(models.Model):
    company = models.ForeignKey(CompanyUser, on_delete=models.CASCADE, default=1)
    sku = models.CharField(max_length=255)
    product_name = models.CharField(max_length=255)
    category = models.CharField(max_length=255, blank=True, null=True)
    price = models.FloatField()
    img = models.URLField(blank=True, null=True)
    trend = models.CharField(max_length=20, blank=True, null=True)  # Use CharField for "upward"/"downward"/"constant"
    FC7 = models.JSONField(blank=True, null=True)
    FC30 = models.JSONField(blank=True, null=True)
    stock = models.IntegerField(default=0)
    on_order = models.IntegerField(default=0)
    reorder = models.IntegerField(default=0)
    reason = models.TextField(blank=True, null=True)
    action_item = models.TextField(blank=True, null=True)
    week_start_date = models.DateField()
    last7 = models.IntegerField(default=0)   # Added
    last30 = models.IntegerField(default=0)  # Added
    created_at = models.DateTimeField(auto_now_add=True)
    def __str__(self):
        return f"{self.product_name} ({self.sku.sku}) - {self.week_start_date}"