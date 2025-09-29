from django.db import models
from django.utils import timezone
from CoreApplication.models import CompanyUser

class SKUForecastHistory(models.Model):
    company = models.ForeignKey(CompanyUser, on_delete=models.CASCADE)
    sku = models.CharField(max_length=255)
    month = models.DateField()  # Month for which prediction is made
    predicted_sales = models.IntegerField()
    actual_sales = models.IntegerField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    error = models.IntegerField(null=True, blank=True)
    error_reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['month']

    def __str__(self):
        return f"{self.sku} - {self.month.strftime('%Y-%m')}"
