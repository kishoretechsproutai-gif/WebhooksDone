import os
import json
import requests
from collections import defaultdict, OrderedDict
from datetime import timedelta

from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from django.utils import timezone
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder

from celery import shared_task
from cryptography.fernet import Fernet

from CoreApplication.models import (
    CompanyUser, ProductVariant, Product, OrderLineItem, Order,
    Collection, CollectionItem, PromotionalData, PurchaseOrder, Prompt
)
from InventoryManagement.models import InventoryPrediction
from CoreApplication.views import get_user_from_token

import tiktoken

# Gemini AI setup
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"


def get_shopify_credentials(company):
    print(f"[INFO] Fetching Shopify credentials for company_id={company.id}")
    if not company.shopify_access_token or not company.shopify_store_url:
        raise ValueError(f"No Shopify credentials found for company_id={company.id}")
    fernet = Fernet(settings.ENCRYPTION_KEY)
    decrypted_token = fernet.decrypt(company.shopify_access_token.encode()).decode()
    decrypted_url = fernet.decrypt(company.shopify_store_url.encode()).decode()
    print(f"[INFO] Shopify credentials decrypted for company_id={company.id}")
    return {"shopify_access_token": decrypted_token, "shopify_store_url": decrypted_url}


def get_shopify_inventory_item_id(variant, company):
    print(f"[INFO] Fetching Shopify inventory_item_id for SKU={variant.sku}")
    creds = get_shopify_credentials(company)
    base_url = creds['shopify_store_url']
    if not base_url.startswith(("http://", "https://")):
        base_url = "https://" + base_url
    url = f"{base_url}/admin/api/2025-04/products/{variant.product_id}/variants/{variant.shopify_id}.json"
    headers = {"X-Shopify-Access-Token": creds['shopify_access_token']}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code == 200:
        inventory_item_id = r.json().get("variant", {}).get("inventory_item_id")
        print(f"[INFO] Found inventory_item_id={inventory_item_id} for SKU={variant.sku}")
        return inventory_item_id
    print(f"[WARN] Failed to fetch inventory_item_id for SKU={variant.sku}, status={r.status_code}")
    return None


def get_shopify_stock(variant, company):
    print(f"[INFO] Fetching Shopify stock for SKU={variant.sku}")
    try:
        creds = get_shopify_credentials(company)
        inventory_item_id = get_shopify_inventory_item_id(variant, company)
        if not inventory_item_id:
            print(f"[WARN] No inventory_item_id, returning stock=0 for SKU={variant.sku}")
            return 0
        base_url = creds['shopify_store_url']
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url
        url = f"{base_url}/admin/api/2023-07/inventory_levels.json?inventory_item_ids={inventory_item_id}"
        headers = {"X-Shopify-Access-Token": creds['shopify_access_token']}
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            levels = r.json().get("inventory_levels", [])
            if levels:
                stock = levels[0].get("available", 0)
                print(f"[INFO] Shopify stock for SKU={variant.sku}: {stock}")
                return stock
        print(f"[WARN] Failed to fetch stock for SKU={variant.sku}, status={r.status_code}")
    except Exception as e:
        print(f"[ERROR] Shopify stock fetch failed for SKU={variant.sku}: {e}")
    return 0


def prepare_sku_data(company_id, variant):
    print(f"[INFO] Preparing SKU data for SKU={variant.sku}")
    product = Product.objects.filter(company_id=company_id, shopify_id=variant.product_id).first()
    today = timezone.now().date()
    one_year_ago = today - timedelta(days=365)
    last_7_days = today - timedelta(days=7)
    last_14_days = today - timedelta(days=14)
    last_30_days = today - timedelta(days=30)

    print(f"[INFO] Fetching order line items for SKU={variant.sku}")
    line_items = OrderLineItem.objects.filter(company_id=company_id, variant_id=variant.shopify_id)
    order_ids = line_items.values_list('order_id', flat=True)
    orders = Order.objects.filter(company_id=company_id, shopify_id__in=order_ids)
    order_date_map = {o.shopify_id: o.order_date for o in orders if o.order_date}

    order_history = []
    historical_sales = defaultdict(int)
    sales_last_7 = sales_last_14 = sales_last_30 = 0

    for li in line_items:
        order_date = order_date_map.get(li.order_id)
        if order_date:
            date_only = order_date.date()
            qty = li.quantity or 0
            if date_only >= one_year_ago:
                order_history.append({"date": date_only.isoformat(), "quantity": qty})
                if date_only >= last_7_days: sales_last_7 += qty
                if date_only >= last_14_days: sales_last_14 += qty
                if date_only >= last_30_days: sales_last_30 += qty
            else:
                month_year = date_only.strftime("%Y %b")
                historical_sales[month_year] += qty

    print(f"[INFO] Sales last 7/14/30 days for SKU={variant.sku}: {sales_last_7}/{sales_last_14}/{sales_last_30}")
    order_history.sort(key=lambda x: x["date"], reverse=True)
    historical_sales = OrderedDict(sorted(historical_sales.items(), key=lambda x: timezone.datetime.strptime(x[0], "%Y %b"), reverse=True))

    print(f"[INFO] Fetching collections and promotions for SKU={variant.sku}")
    collection_items = CollectionItem.objects.filter(product_id=variant.product_id)
    collection_ids = collection_items.values_list('collection_id', flat=True)
    collections = Collection.objects.filter(company_id=company_id, shopify_id__in=collection_ids)
    collection_list = [{"title": c.title, "handle": c.handle} for c in collections]

    promos = PromotionalData.objects.filter(user_id=company_id, variant_id=variant.shopify_id)
    promo_list = [{
        "image_url": p.image_url,
        "title": p.title,
        "variant_id": p.variant_id,
        "price": float(p.price or 0),
        "date": p.date.isoformat() if p.date else None,
        "clicks": p.clicks,
        "impressions": p.impressions,
        "ctr": float(p.ctr or 0),
        "currency_code": p.currency_code,
        "avg_cpc": float(p.avg_cpc or 0),
        "cost": float(p.cost or 0),
        "conversions": p.conversions,
        "conversion_value": float(p.conversion_value or 0),
        "conv_value_per_cost": float(p.conv_value_per_cost or 0),
        "cost_per_conversion": float(p.cost_per_conversion or 0),
        "conversion_rate": float(p.conversion_rate or 0),
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None
    } for p in promos]

    pos = PurchaseOrder.objects.filter(company_id=company_id, sku_id=variant.sku)
    on_order_qty = sum(p.quantity_ordered or 0 for p in pos)
    po_list = [{"purchase_order_id": p.purchase_order_id, "supplier_name": p.supplier_name,
                "order_date": p.order_date, "delivery_date": p.delivery_date,
                "quantity_ordered": p.quantity_ordered} for p in pos]

    stock = get_shopify_stock(variant, CompanyUser.objects.get(id=company_id))
    print(f"[INFO] Real-time stock for SKU={variant.sku}: {stock}")

    prompt_obj = Prompt.objects.filter(id=str(company_id)).first()
    prompt_text = prompt_obj.prompt if prompt_obj else f"""
You are an inventory forecasting assistant for an online fashion jewelry brand in India using Shopify.
Forecast the next 7, 14, and 30 days of sales for SKU {variant.sku}.
Return strict JSON with keys: SKU, forecast_7, forecast_14, forecast_30, trend, confidence_7, confidence_14, confidence_30, algorithm_used, reason.
Always return forecast, even if 0 or 1.
"""
    print(f"[INFO] Prompt prepared for SKU={variant.sku}")

    data_to_send = {
        "SKU": variant.sku,
        "product": {
            "title": product.title if product else "",
            "product_type": product.product_type if product else "",
            "price": float(variant.price or 0),
            "compare_at_price": float(variant.compare_at_price or 0),
            "created_at": variant.created_at.isoformat() if variant.created_at else "",
            "tags": product.tags if product else "",
            "location": "India"
        },
        "sales_last_7_days": sales_last_7,
        "sales_last_14_days": sales_last_14,
        "sales_last_30_days": sales_last_30,
        "days_out_of_stock_last_30_days": 0,
        "on_order": on_order_qty,
        "order_history": order_history,
        "historical_sales": historical_sales,
        "collections": collection_list,
        "promotional_data": promo_list,
        "purchase_orders": po_list,
        "real_time_stock": stock
    }

    print(f"[INFO] SKU data prepared for SKU={variant.sku}: {json.dumps(data_to_send)[:200]}...")
    return data_to_send, prompt_text


def call_gemini_forecast(sku_data, prompt_text):
    print(f"[INFO] Calling Gemini AI for SKU={sku_data['SKU']}")
    try:
        payload = {"contents": [{"parts": [{"text": prompt_text + "\n" + json.dumps(sku_data)}]}]}
        headers = {"Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY}
        response = requests.post(GEMINI_URL, headers=headers, json=payload, timeout=30)
        if response.status_code == 200:
            data = response.json()
            text_output = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
            first = text_output.find("{")
            last = text_output.rfind("}")
            text_output = text_output[first:last+1] if first != -1 and last != -1 else "{}"
            forecast = json.loads(text_output)
            print(f"[INFO] Gemini AI forecast received for SKU={sku_data['SKU']}: {forecast}")
        else:
            print(f"[WARN] Gemini AI returned status={response.status_code} for SKU={sku_data['SKU']}")
            forecast = {}
    except Exception as e:
        print(f"[ERROR] Gemini AI call failed for SKU={sku_data['SKU']}: {e}")
        forecast = {}

    # Default fallback values
    forecast.setdefault("forecast_7", 0)
    forecast.setdefault("forecast_14", 0)
    forecast.setdefault("forecast_30", 0)
    forecast.setdefault("trend", "constant")
    forecast.setdefault("confidence_7", 70)
    forecast.setdefault("confidence_14", 70)
    forecast.setdefault("confidence_30", 70)
    forecast.setdefault("algorithm_used", "Fallback-Conservative")
    forecast.setdefault("reason", "Insufficient data or conservative fallback applied.")
    return forecast


@shared_task(bind=True)
def process_inventory_for_tenant(self, company_id):
    print(f"[INFO] Starting inventory processing for company_id={company_id}")
    variants = ProductVariant.objects.filter(company_id=company_id)
    for idx, variant in enumerate(variants, start=1):
        print(f"[INFO] Processing SKU {variant.sku} ({idx}/{len(variants)})")
        sku_data, prompt_text = prepare_sku_data(company_id, variant)
        forecast = call_gemini_forecast(sku_data, prompt_text)
        InventoryPrediction.objects.update_or_create(
            company_id=company_id,
            sku=variant.sku,
            defaults={
                "product_name": sku_data['product']['title'],
                "category": sku_data['product'].get('product_type', ''),
                "price": sku_data['product']['price'],
                "trend": forecast.get("trend"),
                "FC7": forecast.get("forecast_7"),
                "FC30": forecast.get("forecast_30"),
                "last7": sku_data.get("sales_last_7_days", 0),
                "last30": sku_data.get("sales_last_30_days", 0),
                "stock": sku_data.get("real_time_stock", 0),
                "on_order": sku_data.get("on_order", 0),
                "reorder": int(forecast.get("reorder", 0)),
                "action_item": forecast.get("action"),
                "reason": forecast.get("reason"),
                "week_start_date": timezone.now(),
            }
        )
        print(f"[INFO] InventoryPrediction updated for SKU={variant.sku}")
    print(f"[INFO] Completed inventory processing for company_id={company_id}")


@csrf_exempt
def TriggerInventoryPredictionView(request):
    print("[INFO] TriggerInventoryPredictionView called")
    tenants = CompanyUser.objects.all()
    results = [{"company_id": tenant.id, "task_id": process_inventory_for_tenant.delay(tenant.id).id} for tenant in tenants]
    print(f"[INFO] Tasks triggered: {results}")
    return JsonResponse({"tasks_triggered": results}, status=200)


def Predictions(request):
    print("[INFO] Predictions view called")
    user, error_response = get_user_from_token(request)
    if error_response:
        print("[WARN] User authentication failed")
        return error_response
    predictions = InventoryPrediction.objects.filter(company_id=user.id)
    data = [{
        "company_id": p.company_id,
        "sku": p.sku,
        "product_name": p.product_name,
        "category": p.category,
        "price": p.price,
        "trend": p.trend,
        "FC7": p.FC7,
        "FC30": p.FC30,
        "last7": p.last7,
        "last30": p.last30,
        "stock": p.stock,
        "on_order": p.on_order,
        "reorder": p.reorder,
        "reason": p.reason,
        "week_start_date": p.week_start_date,
    } for p in predictions]
    print(f"[INFO] Returning {len(data)} predictions")
    return JsonResponse({"predictions": data}, encoder=DjangoJSONEncoder, safe=False)


@csrf_exempt
def TestSingleSKUForecast(request):
    company_id = 2
    sku = "US-FNG0514XC2"
    print(f"[INFO] TestSingleSKUForecast called for SKU={sku}")
    try:
        variant = ProductVariant.objects.get(company_id=company_id, sku=sku)
    except ProductVariant.DoesNotExist:
        print(f"[WARN] Variant {sku} not found")
        return JsonResponse({
            "SKU": sku,
            "forecast_7": 1,
            "forecast_14": 1,
            "forecast_30": 2,
            "trend": "constant",
            "confidence_7": 70,
            "confidence_14": 70,
            "confidence_30": 70,
            "algorithm_used": "Fallback-Conservative",
            "reason": "Variant not found",
            "promotional_data": []
        }, safe=False)

    sku_data, prompt_text = prepare_sku_data(company_id, variant)
    print(f"[INFO] Full input prepared for Gemini for SKU={sku}")
    full_input = prompt_text + "\n" + json.dumps(sku_data)
    enc = tiktoken.get_encoding("cl100k_base")
    token_count = len(enc.encode(full_input))
    payload = {"contents": [{"parts": [{"text": full_input}]}]}
    print(f"[INFO] Sending payload to Gemini, token_count={token_count}")
    forecast = call_gemini_forecast(sku_data, prompt_text)

    return JsonResponse({
        "token_count": token_count,
        "payload_sent_to_gemini": payload,
        "forecast_response": forecast,
        "promotional_data": sku_data.get("promotional_data", [])
    }, safe=False)
