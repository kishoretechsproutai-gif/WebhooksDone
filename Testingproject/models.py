from django.db import models
from django.utils import timezone
from CoreApplication.models import CompanyUser

class SKUForecastHistory(models.Model):
    company = models.ForeignKey(CompanyUser, on_delete=models.CASCADE)
    sku = models.CharField(max_length=255)
    month = models.DateField()  # Month for which prediction is made
    predicted_sales_30 = models.IntegerField()
    actual_sales_30 = models.IntegerField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    error = models.IntegerField(null=True, blank=True)
    error_reason = models.TextField(null=True, blank=True)
    predicted_sales_60 = models.IntegerField(null=True, blank=True)
    predicted_sales_90 = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    live_inventory = models.IntegerField(null=True, blank=True)
    class Meta:
        ordering = ['month']

    def __str__(self):
        return f"{self.sku} - {self.month.strftime('%Y-%m')}"



from django.db import models
from CoreApplication.models import CompanyUser

class InventoryValuation(models.Model):
    company = models.ForeignKey(CompanyUser, on_delete=models.CASCADE, related_name="inventory_valuations")
    inventory_value = models.DecimalField(max_digits=20, decimal_places=2, default=0.0)
    month = models.DateField()  # you can store the first day of the month, e.g., 2025-10-01
    currency=models.CharField(max_length=10, default='INR')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Inventory Valuation"
        verbose_name_plural = "Inventory Valuations"
        ordering = ['-month']

    def __str__(self):
        return f"{self.company.company} - {self.month.strftime('%Y-%m')} : {self.inventory_value}"


# model for metrics storage [ Dashboard ]
class SKUForecastMetrics(models.Model):
    company = models.ForeignKey(CompanyUser, on_delete=models.CASCADE)
    sku = models.CharField(max_length=255)
    category = models.CharField(max_length=255, null=True, blank=True)
    month = models.DateField()

    forecast_accuracy = models.FloatField(null=True, blank=True)
    forecast_bias = models.FloatField(null=True, blank=True)
    days_of_inventory = models.FloatField(null=True, blank=True)
    sell_through_rate = models.FloatField(null=True, blank=True)
    inventory_turnover = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('company', 'sku', 'month')
