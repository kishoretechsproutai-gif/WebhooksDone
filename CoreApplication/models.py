from django.db import models
from django.contrib.auth.hashers import make_password, check_password
from decimal import Decimal


class CompanyUser(models.Model):
    company = models.CharField(max_length=255, null=True, blank=True)
    email = models.EmailField(max_length=255, unique=True, null=True, blank=True)
    password = models.CharField(max_length=255, null=True, blank=True)

    shopify_access_token = models.TextField(blank=True, null=True)
    shopify_store_url = models.TextField(blank=True, null=True)
    webhook_secret = models.CharField(max_length=255, null=True, blank=True)

    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password)

    def __str__(self):
        return f"{self.company} ({self.email})"


class Customer(models.Model):
    shopify_id = models.BigIntegerField(unique=True, null=True, blank=True)
    company = models.ForeignKey("CompanyUser", on_delete=models.CASCADE, related_name="customers")

    email = models.EmailField(max_length=255, null=True, blank=True)
    first_name = models.CharField(max_length=255, null=True, blank=True)
    last_name = models.CharField(max_length=255, null=True, blank=True)
    phone = models.CharField(max_length=255, null=True, blank=True)

    created_at = models.DateTimeField(null=True, blank=True)  
    updated_at = models.DateTimeField(null=True, blank=True)

    city = models.CharField(max_length=255, null=True, blank=True)
    region = models.CharField(max_length=255, null=True, blank=True)   # Added region
    country = models.CharField(max_length=255, null=True, blank=True)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"{self.first_name or ''} {self.last_name or ''}".strip() or self.email


class Location(models.Model):
    shopify_id = models.BigIntegerField(unique=True, null=True, blank=True)
    company = models.ForeignKey("CompanyUser", on_delete=models.CASCADE, related_name="locations")

    name = models.CharField(max_length=255, null=True, blank=True)
    address = models.CharField(max_length=255, blank=True, null=True)
    city = models.CharField(max_length=255, blank=True, null=True)
    region = models.CharField(max_length=255, blank=True, null=True)   # Optional: for region awareness
    country = models.CharField(max_length=255, blank=True, null=True)
    postal_code = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return self.name or f"Location {self.shopify_id}"


class Product(models.Model):
    shopify_id = models.BigIntegerField(unique=True, null=True, blank=True)
    company = models.ForeignKey("CompanyUser", on_delete=models.CASCADE, related_name="products")

    title = models.CharField(max_length=255, null=True, blank=True)
    vendor = models.CharField(max_length=255, blank=True, null=True)
    product_type = models.CharField(max_length=255, blank=True, null=True)
    tags = models.TextField(blank=True, null=True)

    created_at = models.DateTimeField(null=True, blank=True)  
    updated_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=255, blank=True, null=True)

    def __str__(self):
        return self.title or f"Product {self.shopify_id}"


class ProductVariant(models.Model):
    shopify_id = models.BigIntegerField(unique=True, null=True, blank=True)
    company = models.ForeignKey("CompanyUser", on_delete=models.CASCADE, related_name="variants")
    product_id = models.BigIntegerField(null=True, blank=True)  # Shopify product ID only

    title = models.CharField(max_length=255, null=True, blank=True)
    sku = models.CharField(max_length=255, blank=True, null=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    compare_at_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    inventory_quantity = models.IntegerField(null=True, blank=True)

    created_at = models.DateTimeField(null=True, blank=True)  
    updated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.title} ({self.sku})" if self.title else f"Variant {self.shopify_id}"


class Order(models.Model):
    shopify_id = models.BigIntegerField(unique=True, null=True, blank=True)
    company = models.ForeignKey("CompanyUser", on_delete=models.CASCADE, related_name="orders")
    customer_id = models.BigIntegerField(null=True, blank=True)  # Shopify customer ID
    order_number = models.CharField(max_length=255, null=True, blank=True)
    order_date = models.DateTimeField(null=True, blank=True)
    fulfillment_status = models.CharField(max_length=255, null=True, blank=True)
    financial_status = models.CharField(max_length=255, null=True, blank=True)
    currency = models.CharField(max_length=255, default="USD")

    total_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    subtotal_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total_tax = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    total_discount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    created_at = models.CharField(max_length=255, null=True, blank=True)  
    updated_at = models.CharField(max_length=255, null=True, blank=True)

    region = models.CharField(max_length=255, null=True, blank=True)   # Added region

    def __str__(self):
        return f"Order {self.order_number or self.shopify_id}"


class OrderLineItem(models.Model):
    shopify_line_item_id = models.BigIntegerField(unique=True, null=True, blank=True)
    company = models.ForeignKey("CompanyUser", on_delete=models.CASCADE, related_name="line_items")
    order_id = models.BigIntegerField(null=True, blank=True)  # Shopify order ID
    product_id = models.BigIntegerField(null=True, blank=True)  # Shopify product ID
    variant_id = models.BigIntegerField(null=True, blank=True)  # Shopify variant ID
    quantity = models.IntegerField(null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    discount_allocated = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    total = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    def __str__(self):
        return f"LineItem {self.shopify_line_item_id} (Order {self.order_id})"


class Prompt(models.Model):
    company = models.TextField(null=True, blank=True)
    prompt = models.TextField(null=True, blank=True)
    generated_prompt = models.TextField(null=True, blank=True)

    def __str__(self):
        return self.prompt[:50] if self.prompt else "Prompt"

# Promotional Data modelfrom django.db import models
from decimal import Decimal

class PromotionalData(models.Model):
    # Multi-tenant identification
    user_id = models.IntegerField()

    # Mandatory campaign details
    campaign_name = models.CharField(max_length=255)
    ad_group_name = models.CharField(max_length=255)
    
    # Date of data
    date = models.DateField()
    
    # Core Google Ads metrics
    clicks = models.PositiveIntegerField()
    impressions = models.PositiveIntegerField()
    cost = models.DecimalField(max_digits=12, decimal_places=2)
    conversions = models.PositiveIntegerField()
    conversion_value = models.DecimalField(max_digits=12, decimal_places=2)
    
    # Derived metrics
    ctr = models.DecimalField(max_digits=5, decimal_places=2)  # Click Through Rate (%)
    cpc = models.DecimalField(max_digits=8, decimal_places=2)  # Cost per click
    roas = models.DecimalField(max_digits=8, decimal_places=2) # Return on ad spend
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Promotional Data"
        verbose_name_plural = "Promotional Data"
        ordering = ['-date']
    
    def __str__(self):
        return f"User {self.user_id} - {self.campaign_name} ({self.date})"



# Collections Table
from django.db import models

class Collection(models.Model):
    company_id = models.IntegerField()  # No foreign key, just an integer for multi-tenancy
    shopify_id = models.BigIntegerField(unique=True)
    title = models.CharField(max_length=255)
    handle = models.CharField(max_length=255)
    updated_at = models.DateTimeField()
    image_src = models.URLField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.title


class CollectionItem(models.Model):
    collection = models.ForeignKey(Collection, on_delete=models.CASCADE, related_name="items")
    product_id = models.BigIntegerField()
    image_src = models.URLField(blank=True, null=True)  # Added image field here
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Product {self.product_id} in {self.collection.title}"
