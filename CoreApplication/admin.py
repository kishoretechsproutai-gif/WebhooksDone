
# Register your models here.
# CoreApplication/admin.py

from django.contrib import admin
from .models import Prompt

@admin.register(Prompt)
class PromptAdmin(admin.ModelAdmin):
    list_display = ("id", "company",  "short_prompt")
    list_filter = ("company",)
    search_fields = ("prompt",  "company__email")  # search by company email
    ordering = ("-id",)

    def short_prompt(self, obj):
        return obj.prompt[:50] if obj.prompt else "-"
    short_prompt.short_description = "Prompt"
