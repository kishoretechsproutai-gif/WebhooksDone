
from django.db import models
from django.contrib.auth.hashers import make_password, check_password


class CompanyUser(models.Model):
    company = models.CharField(max_length=255)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=255)  # hashed

    shopify_access_token = models.TextField(blank=True)
    shopify_store_url = models.TextField(blank=True)

    def set_password(self, raw_password):
        self.password = make_password(raw_password)

    def check_password(self, raw_password):
        return check_password(raw_password, self.password)

    def __str__(self):
        return f"{self.company} ({self.email})"
