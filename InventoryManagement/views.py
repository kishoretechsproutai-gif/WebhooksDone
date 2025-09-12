import os
import requests
from datetime import timedelta
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from celery import shared_task
from CoreApplication.models import (
    CompanyUser, ProductVariant, Product, OrderLineItem, Order,
    Collection, CollectionItem, PromotionalData, PurchaseOrder
)
from InventoryManagement.models import InventoryPrediction

# Gemini API config
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"

# -------------------------------
# Helper: fetch variants with full history for a tenant
# -------------------------------
def get_variants_full_history(company_id):
    today = timezone.now()
    last_7_days = today - timedelta(days=7)
    last_30_days = today - timedelta(days=30)

    print(f"[Tenant {company_id}] Fetching orders and variants...")
    orders = Order.objects.filter(company_id=company_id)
    order_ids = list(orders.values_list('shopify_id', flat=True))
    line_items = OrderLineItem.objects.filter(company_id=company_id, order_id__in=order_ids)

    variant_qty_map = {}
    for li in line_items:
        if li.variant_id:
            variant_qty_map[li.variant_id] = variant_qty_map.get(li.variant_id, 0) + (li.quantity or 0)

    variant_ids = sorted(variant_qty_map, key=lambda x: variant_qty_map[x], reverse=True)
    if not variant_ids:
        print(f"[Tenant {company_id}] No variants found.")
        return []

    # Map variant -> order history
    all_line_items = line_items.filter(variant_id__in=variant_ids)
    all_order_ids = list(all_line_items.values_list('order_id', flat=True))
    all_orders = Order.objects.filter(company_id=company_id, shopify_id__in=all_order_ids)

    variant_orders_map = {}
    for li in all_line_items:
        order = next((o for o in all_orders if o.shopify_id == li.order_id), None)
        if order:
            variant_orders_map.setdefault(li.variant_id, []).append({
                "date": order.order_date,
                "quantity": li.quantity or 0
            })

    variants = ProductVariant.objects.filter(company_id=company_id, shopify_id__in=variant_ids)
    products = Product.objects.filter(company_id=company_id, shopify_id__in=[v.product_id for v in variants])
    collections = Collection.objects.filter(company_id=company_id)
    collection_items = CollectionItem.objects.filter(product_id__in=[v.product_id for v in variants])
    promo_data = PromotionalData.objects.filter(user_id=company_id, variant_id__in=variant_ids)
    purchase_orders = PurchaseOrder.objects.filter(company_id=company_id, sku_id__in=[v.sku for v in variants])

    product_map = {p.shopify_id: p for p in products}
    collection_map = {c.shopify_id: c for c in collections}

    collection_items_map = {}
    for ci in collection_items:
        collection_items_map.setdefault(ci.product_id, []).append(ci)

    promo_map = {}
    for p in promo_data:
        promo_map.setdefault(p.variant_id, []).append(p)

    po_map = {}
    for po in purchase_orders:
        po_map.setdefault(po.sku_id, []).append(po)

    def model_to_dict(obj):
        data = obj.__dict__.copy()
        data.pop('_state', None)
        return data

    result = []
    for idx, v in enumerate(variants, start=1):
        order_data = variant_orders_map.get(v.shopify_id, [])
        last7_qty = sum(o["quantity"] for o in order_data if o["date"] >= last_7_days)
        last30_qty = sum(o["quantity"] for o in order_data if o["date"] >= last_30_days)

        variant_data = {
            "variant": model_to_dict(v),
            "product": model_to_dict(product_map.get(v.product_id)) if v.product_id in product_map else None,
            "total_quantity_sold": variant_qty_map.get(v.shopify_id, 0),
            "order_history": order_data,
            "last_7_days_quantity": last7_qty,
            "last_30_days_quantity": last30_qty,
            "collections": [
                model_to_dict(collection_map[ci.collection_id])
                for ci_list in collection_items_map.get(v.product_id, [])
                for ci in [ci_list]
                if ci.collection_id in collection_map
            ],
            "collection_items": [model_to_dict(ci) for ci in collection_items_map.get(v.product_id, [])],
            "promotional_data": [model_to_dict(p) for p in promo_map.get(v.shopify_id, [])],
            "purchase_orders": [model_to_dict(po) for po in po_map.get(v.sku, [])],
        }
        result.append(variant_data)
    print(f"[Tenant {company_id}] Fetched {len(result)} variants with full history.")
    return result

# -------------------------------
# Gemini API: strict JSON forecast
# -------------------------------
def get_gemini_forecast(variant_data, company_id):
    orders = variant_data.get("order_history", [])
    order_lines_text = "\n".join([f"{o['date']}: {o['quantity']}" for o in orders])

    prompt_text = f"""
You are an inventory forecasting engine.
Given the historical daily sales for SKU {variant_data['variant'].get('sku')} ({variant_data['variant'].get('title')}) 
and product {variant_data.get('product', {}).get('title', '')}:

{order_lines_text}

Predict total sales as integers for:
- next 7 days
- next 14 days
- next 30 days

Return strictly a single JSON object ONLY with keys:
forecast_7, forecast_14, forecast_30, reason
Do NOT include markdown, code fences, or extra text.
"""

    payload = {"contents": [{"parts": [{"text": prompt_text}]}]}
    headers = {"Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY}

    try:
        response = requests.post(GEMINI_API_URL, json=payload, headers=headers, timeout=30)
        resp_json = response.json()
        print(f"[Tenant {company_id}] Gemini API raw response for SKU {variant_data['variant'].get('sku')}: {resp_json}")

        if "candidates" in resp_json and resp_json["candidates"]:
            text = resp_json["candidates"][0]["content"]["parts"][0]["text"]
            text = text.strip("`").strip()
            first = text.find("{")
            last = text.rfind("}")
            if first != -1 and last != -1:
                text = text[first:last+1]
            import json
            forecast = json.loads(text)
            print(f"[Tenant {company_id}] Forecast received for SKU {variant_data['variant'].get('sku')}: {forecast}")
            return forecast
        else:
            print(f"[Tenant {company_id}] Gemini API returned no candidates for SKU {variant_data['variant'].get('sku')}")
            return {"forecast_7": 0, "forecast_14": 0, "forecast_30": 0, "reason": "No candidates in API response"}
    except Exception as e:
        print(f"[Tenant {company_id}] Error fetching Gemini forecast: {e}")
        return {"forecast_7": 0, "forecast_14": 0, "forecast_30": 0, "reason": str(e)}

# -------------------------------
# Celery Task: process a tenant
# -------------------------------
@shared_task(bind=True)
def process_inventory_for_tenant(self, company_id):
    print(f"===== Tenant {company_id} processing started =====")
    top_variants = get_variants_full_history(company_id=company_id)

    for idx, variant_data in enumerate(top_variants, start=1):
        sku = variant_data['variant'].get('sku')
        print(f"[Tenant {company_id}] Processing batch {idx}/{len(top_variants)}: SKU {sku}")
        forecast = get_gemini_forecast(variant_data, company_id)

        # Save prediction in DB including last7 and last30
        InventoryPrediction.objects.update_or_create(
            company_id=company_id,
            sku=sku,
            defaults={
                "product_name": variant_data['product']['title'] if variant_data['product'] else "",
                "category": variant_data['variant'].get('title'),
                "price": variant_data['variant'].get('price', 0),
                "trend": "constant",  # placeholder
                "FC7": forecast.get("forecast_7", 0),
                "FC30": forecast.get("forecast_30", 0),
                "last7": variant_data.get("last_7_days_quantity", 0),
                "last30": variant_data.get("last_30_days_quantity", 0),
                "stock": variant_data.get("last_30_days_quantity", 0),
                "on_order": 0,
                "reorder": 0,
                "reason": forecast.get("reason", ""),
                "week_start_date": timezone.now(),
            }
        )
        print(f"[Tenant {company_id}] SKU {sku} saved in DB with last7={variant_data.get('last_7_days_quantity', 0)} last30={variant_data.get('last_30_days_quantity', 0)}")

    print(f"===== Tenant {company_id} processing completed =====")

# -------------------------------
# Trigger view: run for all tenants (no authentication)
# -------------------------------
@csrf_exempt
def TriggerInventoryPredictionView(request):
    tenants = CompanyUser.objects.all()
    results = []

    for tenant in tenants:
        # Fire Celery task for each tenant
        task = process_inventory_for_tenant.delay(tenant.id)
        results.append({"company_id": tenant.id, "task_id": task.id})
        print(f"Triggered task for Tenant {tenant.id}, task_id {task.id}")

    return JsonResponse({"tasks_triggered": results}, status=200)
