import os
import json
import openai
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from django.http import JsonResponse
from collections import defaultdict
from django.views.decorators.csrf import csrf_exempt
from CoreApplication.models import (
    ProductVariant, Product, OrderLineItem, Order, CompanyUser, Prompt, PromotionalData
)
from Testingproject.models import SKUForecastHistory
from django.db.models import Sum
from celery import shared_task

# ---------------- OpenAI Setup ----------------
openai.api_key = os.getenv("OPENAI_API_KEY")

def call_openai(prompt_text):
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_text}],
            temperature=0
        )
        return response.choices[0].message.content
    except Exception as e:
        print("OpenAI API call failed:", e)
        return ""

def clean_openai_json(response_text):
    if not response_text or not response_text.strip():
        return {}
    response_text = response_text.strip()
    if response_text.startswith("```json"):
        response_text = response_text[7:]
    if response_text.startswith("```"):
        response_text = response_text[3:]
    if response_text.endswith("```"):
        response_text = response_text[:-3]
    response_text = response_text.strip()
    try:
        return json.loads(response_text)
    except Exception as e:
        print("JSON parsing failed:", e)
        print("Raw OpenAI response:", repr(response_text))
        return {}

def analyze_error(predicted, actual):
    if actual is None or actual == 0:
        return None, None
    error_value = abs(predicted - actual) / actual * 100
    error_reason = f"The prediction {'underestimated' if predicted < actual else 'overestimated'} the actual sales by {error_value:.1f}%."
    return error_reason, error_value

# ---------------- Core Forecast Function ----------------


def forecast_single_sku_for_variant(company, variant):
    """
    Forecast a single SKU for a given company and variant using AI-tailored prompts.
    Returns results list for that SKU.
    """
    sku = variant.sku

    # Fetch base prompt
    try:
        base_prompt_obj = Prompt.objects.get(id=2)
        base_prompt = base_prompt_obj.prompt
        if not base_prompt:
            print(f"Prompt ID 2 is empty")
            return []
    except Prompt.DoesNotExist:
        print(f"Prompt ID 2 not found")
        return []

    # Fetch live Shopify inventory
    live_inventory = 0
    try:
        from cryptography.fernet import Fernet
        from django.conf import settings
        import requests

        fernet = Fernet(settings.ENCRYPTION_KEY)
        shopify_token = fernet.decrypt(company.shopify_access_token.encode()).decode()
        shopify_url = fernet.decrypt(company.shopify_store_url.encode()).decode()

        headers = {"X-Shopify-Access-Token": shopify_token, "Content-Type": "application/json"}
        endpoint = f"https://{shopify_url}/admin/api/2025-07/variants/{variant.shopify_id}.json"
        response = requests.get(endpoint, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if "variant" in data:
            live_inventory = int(data["variant"].get("inventory_quantity", 0))
    except Exception as e:
        print(f"Failed to fetch Shopify inventory for SKU {sku}: {e}")

    # Last forecasted month
    last_forecast = SKUForecastHistory.objects.filter(company=company, sku=sku).order_by("-month").first()
    start_month = last_forecast.month if last_forecast else date(2025, 1, 1)
    end_month = date.today().replace(day=1)

    # Fetch orders and line items
    all_orders = Order.objects.filter(
        company=company,
        order_date__gte=date.today() - relativedelta(years=3),
        order_date__lt=date.today()
    ).only("shopify_id", "order_date")
    order_map = {o.shopify_id: o for o in all_orders}

    all_lineitems = OrderLineItem.objects.filter(
        company=company,
        variant_id=variant.shopify_id,
        order_id__in=order_map.keys()
    ).only("variant_id", "order_id", "quantity")

    monthly_sales_map = defaultdict(int)
    daily_sales_map = defaultdict(int)
    for li in all_lineitems:
        order_obj = order_map.get(li.order_id)
        if not order_obj:
            continue
        month_key = order_obj.order_date.strftime("%Y-%m")
        day_key = order_obj.order_date.strftime("%Y-%m-%d")
        monthly_sales_map[month_key] += li.quantity or 0
        daily_sales_map[day_key] += li.quantity or 0

    # Product info
    try:
        product_obj = Product.objects.get(company=company, shopify_id=variant.product_id)
        product_data = {
            "title": product_obj.title,
            "vendor": product_obj.vendor,
            "product_type": product_obj.product_type,
            "tags": product_obj.tags.split(",") if product_obj.tags else [],
            "created_at": str(product_obj.created_at.date()) if product_obj.created_at else None
        }
    except Product.DoesNotExist:
        product_data = {}

    # Promotions last 90 days
    promos = PromotionalData.objects.filter(user_id=company, variant_id=variant.shopify_id).order_by('-date')[:90]
    promo_data_list = []
    marketing_campaign_flag = False
    for promo in promos:
        promo_data_list.append({
            "date": str(promo.date),
            "clicks": promo.clicks,
            "impressions": promo.impressions,
            "avg_cpc": float(promo.avg_cpc) if promo.avg_cpc else None,
            "cost": float(promo.cost) if promo.cost else None,
            "conversions": promo.conversions,
            "conversion_value": float(promo.conversion_value) if promo.conversion_value else None
        })
        if promo.clicks > 0 or promo.impressions > 0 or (promo.cost and promo.cost > 0):
            marketing_campaign_flag = True

    # Past errors
    past_forecasts = SKUForecastHistory.objects.filter(company=company, sku=sku).order_by("month")
    past_errors = [{
        "month": f.month.strftime("%Y-%m"),
        "predicted_sales_30": f.predicted_sales_30,
        "predicted_sales_60": f.predicted_sales_60,
        "predicted_sales_90": f.predicted_sales_90,
        "actual_sales": f.actual_sales_30,
        "error_value": f.error,
        "error_reason": f.error_reason
    } for f in past_forecasts]

    results_all = []
    current_month = start_month

    while current_month <= end_month:
        # Last 6 months monthly sales
        monthly_totals = {
            (current_month - relativedelta(months=5-i)).strftime("%Y-%m"): monthly_sales_map.get(
                (current_month - relativedelta(months=5-i)).strftime("%Y-%m"), 0
            ) for i in range(6)
        }

        # Last 2 months daily sales
        daily_totals = {
            day_str: qty for day_str, qty in daily_sales_map.items()
            if current_month - relativedelta(months=2) <= datetime.strptime(day_str, "%Y-%m-%d").date() < current_month
        }

        # ---------------- Tailored AI Prompt ----------------
        data_for_prompt = {
            "sku": sku,
            "variant_title": variant.title,
            "price": float(variant.price) if variant.price else None,
            "compare_at_price": float(variant.compare_at_price) if variant.compare_at_price else None,
            "cost": float(variant.cost) if variant.cost else None,
            "created_at": str(variant.created_at.date()) if variant.created_at else None,
            "product": product_data,
            "monthly_sales_last_6_months": monthly_totals,
            "daily_sales_last_2_months": daily_totals,
            "previous_errors": past_errors[-5:],
            "promotional_data_last_3_months": promo_data_list,
            "marketing_campaign_flag": marketing_campaign_flag
        }

        prompt_for_generation = f"""
You are a prompt generator. Using this base prompt from DB (do NOT change instructions, only tailor to SKU):

{base_prompt}

And this SKU data:

{json.dumps(data_for_prompt, indent=2)}

Generate a tailored prompt that:
- Adjusts weightages for this SKU based on monthly/daily sales, trends, tags, and promotions
- Incorporates previous prediction errors
- Always results in strict JSON forecast for 30, 60, 90 days
Return only the text of the new prompt.
"""
        generated_prompt_text = call_openai(prompt_for_generation)

        # Forecast using the generated prompt
        forecast_input = f"""
{generated_prompt_text}

Here is the current month data:

{json.dumps(data_for_prompt, indent=2)}

Return **only valid JSON** with keys: predicted_sales_30, predicted_sales_60, predicted_sales_90, reason_30, reason_60, reason_90.
"""
        openai_response = call_openai(forecast_input)
        parsed_response = clean_openai_json(openai_response)
        if not parsed_response:
            openai_response = call_openai(forecast_input)
            parsed_response = clean_openai_json(openai_response)

        predicted_sales_30 = parsed_response.get("predicted_sales_30", 0)
        predicted_sales_60 = parsed_response.get("predicted_sales_60", 0)
        predicted_sales_90 = parsed_response.get("predicted_sales_90", 0)
        reason_30 = parsed_response.get("reason_30", "")
        reason_60 = parsed_response.get("reason_60", "")
        reason_90 = parsed_response.get("reason_90", "")

        combined_reason = f"30d: {reason_30}; 60d: {reason_60}; 90d: {reason_90}"

        # --- Update actual_sales: None for current month, otherwise fetch from monthly_sales_map ---
        if current_month >= date.today().replace(day=1):
            actual_sales_30 = None
        else:
            actual_sales_30 = monthly_sales_map.get(current_month.strftime("%Y-%m"), 0)

        # --- Calculate errors and metrics only if actual sales exist ---
        if actual_sales_30 is not None:
            error_reason, error_value = analyze_error(predicted_sales_30, actual_sales_30)
            calculate_metrics = True
        else:
            error_reason, error_value = None, None
            calculate_metrics = False

        forecast, _ = SKUForecastHistory.objects.update_or_create(
            company=company,
            sku=sku,
            month=current_month,
            defaults={
                "predicted_sales_30": predicted_sales_30,
                "predicted_sales_60": predicted_sales_60,
                "predicted_sales_90": predicted_sales_90,
                "actual_sales_30": actual_sales_30 ,
                "reason": combined_reason,
                "live_inventory": live_inventory,
                "error": error_value,
                "error_reason": error_reason
            }
        )

        if calculate_metrics:
            calculate_metrics_for_sku(company, sku, current_month)

        past_errors.append({
            "month": current_month.strftime("%Y-%m"),
            "predicted_sales_30": predicted_sales_30,
            "predicted_sales_60": predicted_sales_60,
            "predicted_sales_90": predicted_sales_90,
            "actual_sales": actual_sales_30,
            "reason_30": reason_30,
            "reason_60": reason_60,
            "reason_90": reason_90,
            "error_reason": error_reason,
            "error_value": error_value,
            "live_inventory": live_inventory
        })

        results_all.append({
            "month": current_month.strftime("%Y-%m"),
            "predicted_sales_30": predicted_sales_30,
            "predicted_sales_60": predicted_sales_60,
            "predicted_sales_90": predicted_sales_90,
            "actual_sales": actual_sales_30,
            "error": error_value,
            "error_reason": error_reason,
            "live_inventory": live_inventory
        })

        current_month += relativedelta(months=1)

    return results_all


# ---------------- Celery Task ----------------

from celery import shared_task
from datetime import date
from dateutil.relativedelta import relativedelta
from django.db.models import Sum
from CoreApplication.models import CompanyUser, OrderLineItem, ProductVariant
from .models import InventoryValuation

@shared_task
def run_monthly_forecast():
    print("Starting monthly forecast task for all companies...")

    companies = CompanyUser.objects.all()
    for company in companies:
        print(f"Processing company {company.id}...")

        two_months_ago = date.today() - relativedelta(months=2)

        # Fetch top 500 SKUs in last 2 months
        sku_sales = (
            OrderLineItem.objects.filter(
                company=company,
                order__order_date__gte=two_months_ago
            )
            .values("variant_id")
            .annotate(total_qty=Sum("quantity"))
            .order_by("-total_qty")[:500]
        )

        for sku_data in sku_sales:
            variant_id = sku_data["variant_id"]
            try:
                variant = ProductVariant.objects.get(company=company, shopify_id=variant_id)
                forecast_single_sku_for_variant(company, variant)
            except ProductVariant.DoesNotExist:
                print(f"Variant {variant_id} not found for company {company.id}")

        # ---------------- Inventory Valuation ---------------- #
        total_value = get_inventory_value_for_company(company)
        order = Order.objects.filter(company=company).first()
        currency = order.currency if order else "INR"
        if total_value is not None:
            InventoryValuation.objects.update_or_create(
                company=company,
                month=date.today().replace(day=1),
                defaults={
                    "inventory_value": total_value,
                          "currency": currency
                          }
            )
            print(f"Saved inventory value for company {company.id}: {total_value}")
        else:
            print(f"Inventory calculation failed for company {company.id}")

    print("Monthly forecast task completed.")



# ---------------- Manual Trigger ----------------

from datetime import date
from dateutil.relativedelta import relativedelta
from decimal import Decimal
from django.http import JsonResponse
from django.db.models import Sum
from CoreApplication.models import CompanyUser, Order, OrderLineItem, ProductVariant
from .models import InventoryValuation

def get_inventory_value_for_company(company):
    """
    Helper to calculate total inventory value directly from Shopify credentials of a company.
    Returns a Decimal value or None if credentials missing/failure.
    """
    from cryptography.fernet import Fernet
    import requests
    from django.conf import settings

    if not company.shopify_access_token or not company.shopify_store_url:
        return None

    try:
        fernet = Fernet(settings.ENCRYPTION_KEY)
        shopify_access_token = fernet.decrypt(company.shopify_access_token.encode()).decode()
        shopify_url = fernet.decrypt(company.shopify_store_url.encode()).decode()
    except:
        return None

    total_value = Decimal("0.0")
    try:
        products_endpoint = f"https://{shopify_url}/admin/api/2025-10/products.json?limit=250"
        headers = {"X-Shopify-Access-Token": shopify_access_token}
        next_page_url = products_endpoint

        while next_page_url:
            resp = requests.get(next_page_url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            products = data.get("products", [])

            for product in products:
                for variant in product.get("variants", []):
                    price = float(variant.get("price") or 0.0)
                    inventory_qty = max(variant.get("inventory_quantity", 0), 0)
                    total_value += Decimal(price * inventory_qty)

            # Pagination
            link_header = resp.headers.get("Link", "")
            next_page_url = None
            if 'rel="next"' in link_header:
                parts = link_header.split(",")
                for part in parts:
                    if 'rel="next"' in part:
                        next_page_url = part.split(";")[0].strip()[1:-1]  # remove < >
                        break

    except:
        return None

    return total_value


@csrf_exempt
def manual_forecast(request):
    results = []
    companies = CompanyUser.objects.all()

    for company in companies:
        two_months_ago = date.today() - relativedelta(months=2)

        # Fetch orders for the company in last 3 years
        all_orders = Order.objects.filter(
            company=company,
            order_date__gte=date.today() - relativedelta(years=3),
            order_date__lt=date.today()
        ).only("shopify_id", "order_date")
        order_map = {o.shopify_id: o for o in all_orders}

        # Fetch top SKUs in last 2 months
        sku_sales = (
            OrderLineItem.objects.filter(
                company=company,
                order_id__in=order_map.keys()
            )
            .values("variant_id")
            .annotate(total_qty=Sum("quantity"))
            .order_by("-total_qty")[:3]
        )

        for sku_data in sku_sales:
            variant_id = sku_data["variant_id"]
            try:
                variant = ProductVariant.objects.get(company=company, shopify_id=variant_id)
                # Forecast single SKU
                res = forecast_single_sku_for_variant(company, variant)
                results.extend(res)
            except ProductVariant.DoesNotExist:
                print(f"Variant {variant_id} not found for company {company.id}")
                continue

        # ---------------- Inventory Valuation ---------------- #
        total_value = get_inventory_value_for_company(company)
        order = Order.objects.filter(company=company).first()
        currency = order.currency if order else "INR"
        if total_value is not None:
            InventoryValuation.objects.update_or_create(
                company=company,
                month=date.today().replace(day=1),
                defaults={"inventory_value": total_value,
                          "currency": currency}
            )
            print(f"Saved inventory value for company {company.id}: {total_value}")
        else:
            print(f"Inventory calculation failed for company {company.id}")

    return JsonResponse({"status": "completed", "results_count": len(results)}, safe=False)


# Metric Calculation helper function for manual forecat function and celery function

# In the same views.py (or a utils.py if you prefer)
from Testingproject.models import SKUForecastMetrics

def calculate_metrics_for_sku(company, sku, month):
    """
    Calculates forecast metrics for a given SKU and month and stores it in SKUForecastMetrics.
    """
    from Testingproject.models import SKUForecastHistory

    forecast = SKUForecastHistory.objects.filter(company=company, sku=sku, month=month).first()
    if not forecast:
        return None

    actual = forecast.actual_sales_30 or 0
    predicted = forecast.predicted_sales_30 or 0
    inventory = forecast.live_inventory or 0

    # ---- Metric calculations ----
    forecast_accuracy = 100 - (abs(predicted - actual) / max(actual, 1)) * 100
    forecast_bias = ((predicted - actual) / max(actual, 1)) * 100
    days_of_inventory = (inventory / max(predicted, 1)) * 30
    sell_through_rate = (actual / (actual + inventory)) * 100 if (actual + inventory) > 0 else 0
    inventory_turnover = actual / max(inventory, 1)

    # ---- Get category ----
    from CoreApplication.models import ProductVariant, Product
    variant = ProductVariant.objects.filter(company=company, sku=sku).first()
    category = None
    if variant:
        product = Product.objects.filter(company=company, shopify_id=variant.product_id).first()
        category = product.product_type if product else None

    # ---- Store or update record ----
    metrics, created = SKUForecastMetrics.objects.update_or_create(
        company=company,
        sku=sku,
        month=month,
        defaults={
            "category": category,
            "forecast_accuracy": forecast_accuracy,
            "forecast_bias": forecast_bias,
            "days_of_inventory": days_of_inventory,
            "sell_through_rate": sell_through_rate,
            "inventory_turnover": inventory_turnover,
        },
    )

    return metrics



# TEsting one SKU prompt ----------------------------------------------------------------------------------

import os
import json
import openai
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from django.http import JsonResponse
from collections import defaultdict
from django.views.decorators.csrf import csrf_exempt
from CoreApplication.models import (
    ProductVariant, Product, OrderLineItem, Order, CompanyUser, Prompt, PromotionalData
)
from Testingproject.models import SKUForecastHistory

# Set your OpenAI key
openai.api_key = os.getenv("OPENAI_API_KEY")

def call_openai(prompt_text):
    try:
        response = openai.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt_text}],
            temperature=0
        )
        return response.choices[0].message.content
    except Exception as e:
        print("OpenAI API call failed:", e)
        return ""

def clean_openai_json(response_text):
    if not response_text or not response_text.strip():
        return {}
    import re
    match = re.search(r"\{.*\}", response_text, re.DOTALL)
    if not match:
        print("No JSON object found in response")
        print("Raw OpenAI response:", repr(response_text))
        return {}
    try:
        return json.loads(match.group(0))
    except Exception as e:
        print("JSON parsing failed:", e)
        print("Raw JSON:", repr(match.group(0)))
        return {}

def analyze_error(predicted, actual):
    if actual is None or actual == 0:
        return None, None
    error_value = abs(predicted - actual) / actual * 100
    error_reason = f"The prediction {'underestimated' if predicted < actual else 'overestimated'} the actual sales by {error_value:.1f}%."
    return error_reason, error_value

def count_tokens(text):
    return len(text.split())

@csrf_exempt
def forecast_single_sku(request):
    print("forecast_single_sku: started")
    company_id = 2  # Hardcoded tenant
    try:
        company = CompanyUser.objects.get(id=company_id)
        print("Company fetched:", company.id)
    except CompanyUser.DoesNotExist:
        return JsonResponse({"error": f"Company ID {company_id} not found"}, status=404)

    # Base prompt from DB
    try:
        base_prompt_obj = Prompt.objects.get(id=2)
        base_prompt = base_prompt_obj.prompt
        if not base_prompt:
            return JsonResponse({"error": "Prompt is empty"}, status=400)
    except Prompt.DoesNotExist:
        return JsonResponse({"error": "Prompt ID 2 not found"}, status=404)

    # Specific SKU
    specific_sku = "TNX0100XTE"
    variant_objs = ProductVariant.objects.filter(company=company, sku=specific_sku)
    if not variant_objs.exists():
        return JsonResponse({"error": f"SKU {specific_sku} not found"}, status=404)

    variant = variant_objs.first()
    sku = variant.sku

    # Fetch live Shopify inventory
    live_inventory = 0
    try:
        from cryptography.fernet import Fernet
        from django.conf import settings
        import requests

        fernet = Fernet(settings.ENCRYPTION_KEY)
        shopify_token = fernet.decrypt(company.shopify_access_token.encode()).decode()
        shopify_url = fernet.decrypt(company.shopify_store_url.encode()).decode()

        headers = {"X-Shopify-Access-Token": shopify_token, "Content-Type": "application/json"}
        endpoint = f"https://{shopify_url}/admin/api/2025-07/variants/{variant.shopify_id}.json"
        response = requests.get(endpoint, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        if "variant" in data:
            live_inventory = int(data["variant"].get("inventory_quantity", 0))
    except Exception as e:
        print(f"Failed to fetch Shopify inventory for SKU {sku}: {e}")

    # Fetch orders for last 3 years
    all_orders = Order.objects.filter(
        company=company,
        order_date__gte=date.today() - relativedelta(years=3),
        order_date__lt=date.today()
    ).only("shopify_id", "order_date")
    order_map = {o.shopify_id: o for o in all_orders}
    print("Total orders fetched:", len(order_map))

    # Fetch line items
    all_lineitems = OrderLineItem.objects.filter(
        company=company,
        variant_id=variant.shopify_id,
        order_id__in=order_map.keys()
    ).only("variant_id", "order_id", "quantity")
    print("Total line items fetched:", all_lineitems.count())

    # Aggregate monthly and daily sales
    monthly_sales_map = defaultdict(int)
    daily_sales_map = defaultdict(int)
    for li in all_lineitems:
        order_obj = order_map.get(li.order_id)
        if not order_obj:
            continue
        month_key = order_obj.order_date.strftime("%Y-%m")
        day_key = order_obj.order_date.strftime("%Y-%m-%d")
        monthly_sales_map[month_key] += li.quantity or 0
        daily_sales_map[day_key] += li.quantity or 0
    print("Aggregated monthly and daily sales")

    # Fetch product data
    try:
        product_obj = Product.objects.get(company=company, shopify_id=variant.product_id)
        product_data = {
            "title": product_obj.title,
            "vendor": product_obj.vendor,
            "product_type": product_obj.product_type,
            "tags": product_obj.tags.split(",") if product_obj.tags else [],
            "created_at": str(product_obj.created_at.date()) if product_obj.created_at else None
        }
    except Product.DoesNotExist:
        product_data = {}

    # Fetch promotions last 90 days
    promos = PromotionalData.objects.filter(user_id=company, variant_id=variant.shopify_id).order_by('-date')[:90]
    promo_data_list = []
    marketing_campaign_flag = False
    for promo in promos:
        promo_data_list.append({
            "date": str(promo.date),
            "clicks": promo.clicks,
            "impressions": promo.impressions,
            "avg_cpc": float(promo.avg_cpc) if promo.avg_cpc else None,
            "cost": float(promo.cost) if promo.cost else None,
            "conversions": promo.conversions,
            "conversion_value": float(promo.conversion_value) if promo.conversion_value else None
        })
        if promo.clicks > 0 or promo.impressions > 0 or (promo.cost and promo.cost > 0):
            marketing_campaign_flag = True

    past_errors = []

    # --------------------- Generate AI-based tailored prompt ---------------------
    current_month = date(2025, 1, 1)
    end_month = date.today()
    results_all = []

    while current_month <= end_month:
        # Last 6 months monthly sales
        monthly_totals = { 
            (current_month - relativedelta(months=5-i)).strftime("%Y-%m"): monthly_sales_map.get(
                (current_month - relativedelta(months=5-i)).strftime("%Y-%m"), 0
            ) for i in range(6)
        }

        # Last 2 months daily sales
        two_months_ago = current_month - relativedelta(months=2)
        daily_totals = { day_str: qty for day_str, qty in daily_sales_map.items() 
                         if two_months_ago <= datetime.strptime(day_str, "%Y-%m-%d").date() < current_month }

        # Construct tailored prompt
        data_for_prompt = {
            "sku": sku,
            "variant_title": variant.title,
            "price": float(variant.price) if variant.price else None,
            "compare_at_price": float(variant.compare_at_price) if variant.compare_at_price else None,
            "cost": float(variant.cost) if variant.cost else None,
            "created_at": str(variant.created_at.date()) if variant.created_at else None,
            "product": product_data,
            "monthly_sales_last_6_months": monthly_totals,
            "daily_sales_last_2_months": daily_totals,
            "previous_errors": past_errors,
            "promotional_data_last_3_months": promo_data_list,
            "marketing_campaign_flag": marketing_campaign_flag
        }

        prompt_for_generation = f"""
You are a prompt generator. Using this base prompt from DB (do NOT change instructions, only tailor to SKU):

{base_prompt}

And this SKU data:

{json.dumps(data_for_prompt, indent=2)}

Generate a tailored prompt that:
- Adjusts weightages for this SKU based on monthly/daily sales, trends, tags, and promotions
- Incorporates previous prediction errors
- Always results in strict JSON forecast for 30, 60, 90 days
Return only the text of the new prompt.
"""
        print(f"Generating new AI-based tailored prompt for {current_month.strftime('%Y-%m')}...")
        generated_prompt_text = call_openai(prompt_for_generation)
        Prompt.objects.filter(id=2).update(generated_prompt=generated_prompt_text)
        print("Generated prompt saved.")

        # Use generated prompt for forecasting
        forecast_input = f"""
{generated_prompt_text}

Here is the current month data:

{json.dumps(data_for_prompt, indent=2)}

Return **only valid JSON** with keys: predicted_sales_30, predicted_sales_60, predicted_sales_90, reason_30, reason_60, reason_90.
"""
        openai_response = call_openai(forecast_input)
        parsed_response = clean_openai_json(openai_response)

        # Retry if empty
        if not parsed_response:
            print("Retrying OpenAI forecast call...")
            openai_response = call_openai(forecast_input)
            parsed_response = clean_openai_json(openai_response)

        # Extract predictions & reasons
        predicted_sales_30 = parsed_response.get("predicted_sales_30", 0)
        predicted_sales_60 = parsed_response.get("predicted_sales_60", 0)
        predicted_sales_90 = parsed_response.get("predicted_sales_90", 0)
        reason_30 = parsed_response.get("reason_30", "")
        reason_60 = parsed_response.get("reason_60", "")
        reason_90 = parsed_response.get("reason_90", "")

        combined_reason = f"30 days reason: {reason_30}; 60 days reason: {reason_60}; 90 days reason: {reason_90}"
        actual_sales_30 = monthly_sales_map.get(current_month.strftime("%Y-%m"), 0)

        forecast, _ = SKUForecastHistory.objects.update_or_create(
            company=company,
            sku=sku,
            month=current_month,
            defaults={
                "predicted_sales_30": predicted_sales_30,
                "predicted_sales_60": predicted_sales_60,
                "predicted_sales_90": predicted_sales_90,
                "actual_sales_30": actual_sales_30,
                "reason": combined_reason,
                "live_inventory": live_inventory
            }
        )

        error_reason, error_value = analyze_error(predicted_sales_30, actual_sales_30)
        forecast.error = error_value
        forecast.error_reason = error_reason
        forecast.save()

        past_errors.append({
            "month": current_month.strftime("%Y-%m"),
            "predicted_sales_30": predicted_sales_30,
            "predicted_sales_60": predicted_sales_60,
            "predicted_sales_90": predicted_sales_90,
            "actual_sales": actual_sales_30,
            "reason_30": reason_30,
            "reason_60": reason_60,
            "reason_90": reason_90,
            "error_reason": error_reason,
            "error_value": error_value,
            "live_inventory": live_inventory
        })

        results_all.append({
            "sku": sku,
            "month": current_month.strftime("%Y-%m"),
            "predicted_sales_30": predicted_sales_30,
            "predicted_sales_60": predicted_sales_60,
            "predicted_sales_90": predicted_sales_90,
            "reason_30": reason_30,
            "reason_60": reason_60,
            "reason_90": reason_90,
            "actual_sales_30": actual_sales_30,
            "error_reason": error_reason,
            "error_value": error_value,
            "live_inventory": live_inventory
        })

        current_month += relativedelta(months=1)

    print(f"Forecast completed for SKU {sku}")
    return JsonResponse({"debug": "Completed successfully", "results": results_all}, safe=False)




# Inventory Value View ----------------------------------------------------------------------------------

# Testing inventory value calculation using Shopify live data

import requests
from decimal import Decimal
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from CoreApplication.views import get_user_from_token
from cryptography.fernet import Fernet
from django.conf import settings
from CoreApplication.models import Order

@csrf_exempt
def calculate_inventory_value_from_shopify(request):
    """
    Calculate total inventory value for a company using Shopify live data (bulk variant fetching).
    JWT token should be sent in Authorization header.
    Returns currency along with total value and SKU-level details.
    """
    if request.method != "GET":
        return JsonResponse({"error": "Only GET requests allowed."}, status=405)

    # ---------------- Auth ---------------- #
    user, error = get_user_from_token(request)
    if error:
        return error  # Already a JsonResponse if invalid

    if not user.shopify_access_token or not user.shopify_store_url:
        return JsonResponse({"error": "No Shopify credentials found"}, status=404)

    try:
        fernet = Fernet(settings.ENCRYPTION_KEY)
        shopify_access_token = fernet.decrypt(user.shopify_access_token.encode()).decode()
        shopify_url = fernet.decrypt(user.shopify_store_url.encode()).decode()
    except Exception as e:
        return JsonResponse({"error": f"Failed to decrypt credentials: {str(e)}"}, status=500)

    # ---------------- Determine currency ---------------- #
    order = Order.objects.filter(company=user).first()
    currency = order.currency if order else "USD"

    total_value = Decimal("0.0")
    details = []

    # ---------------- Fetch all products in bulk ---------------- #
    products_endpoint = f"https://{shopify_url}/admin/api/2025-10/products.json?limit=250"
    headers = {"X-Shopify-Access-Token": shopify_access_token}
    next_page_url = products_endpoint

    while next_page_url:
        try:
            resp = requests.get(next_page_url, headers=headers, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            products = data.get("products", [])

            for product in products:
                variants = product.get("variants", [])
                for variant in variants:
                    sku = variant.get("sku")
                    price = variant.get("price")
                    inventory_qty = variant.get("inventory_quantity", 0)

                    if not price:
                        price = "0.0"
                    price_float = float(price)

                    # ---------------- Only sum if inventory > 0 ---------------- #
                    if inventory_qty > 0:
                        value = price_float * inventory_qty
                        total_value += Decimal(value)
                    else:
                        value = 0
                        inventory_qty = max(inventory_qty, 0)  # optional: show negative inventory as 0

                    details.append({
                        "SKU": sku,
                        "price": price_float,
                        "inventory_qty": inventory_qty,
                        "value": value
                    })

            # Check if there is a next page (Shopify pagination via Link header)
            link_header = resp.headers.get("Link", "")
            next_page_url = None
            if 'rel="next"' in link_header:
                parts = link_header.split(",")
                for part in parts:
                    if 'rel="next"' in part:
                        next_page_url = part.split(";")[0].strip()[1:-1]  # remove < >
                        break

        except Exception as e:
            return JsonResponse({"error": f"Failed fetching products: {str(e)}"}, status=500)

    return JsonResponse({
        "total_inventory_value": float(total_value),
        "currency": currency,
        "details": details
    }, safe=False)



# Testing Metric calculation----------------------------------------------------------------------------

from datetime import date
from dateutil.relativedelta import relativedelta
from django.http import JsonResponse
from django.db.models import Max
from CoreApplication.models import ProductVariant, Product, CompanyUser
from Testingproject.models import SKUForecastHistory, SKUForecastMetrics


def calculate_and_store_metrics(company, sku, month):
    forecast = SKUForecastHistory.objects.filter(company=company, sku=sku, month=month).first()
    if not forecast:
        return None

    actual = forecast.actual_sales_30 or 0
    predicted = forecast.predicted_sales_30 or 0
    inventory = forecast.live_inventory or 0

    # ---- Metric calculations ----
    forecast_accuracy = 100 - (abs(predicted - actual) / max(actual, 1)) * 100
    forecast_bias = ((predicted - actual) / max(actual, 1)) * 100
    days_of_inventory = (inventory / max(predicted, 1)) * 30
    sell_through_rate = (actual / (actual + inventory)) * 100 if (actual + inventory) > 0 else 0
    inventory_turnover = actual / max(inventory, 1)

    # ---- Get category ----
    variant = ProductVariant.objects.filter(company=company, sku=sku).first()
    category = None
    if variant:
        product = Product.objects.filter(company=company, shopify_id=variant.product_id).first()
        category = product.product_type if product else None

    # ---- Store or update record ----
    metrics, created = SKUForecastMetrics.objects.update_or_create(
        company=company,
        sku=sku,
        month=month,
        defaults={
            "category": category,
            "forecast_accuracy": forecast_accuracy,
            "forecast_bias": forecast_bias,
            "days_of_inventory": days_of_inventory,
            "sell_through_rate": sell_through_rate,
            "inventory_turnover": inventory_turnover,
        },
    )

    return {
        "SKU": sku,
        "Month": month.strftime("%Y-%m"),
        "Forecast Accuracy (%)": round(forecast_accuracy, 2),
        "Forecast Bias (%)": round(forecast_bias, 2),
        "Days of Inventory": round(days_of_inventory, 2),
        "Sell Through Rate (%)": round(sell_through_rate, 2),
        "Inventory Turnover": round(inventory_turnover, 2),
        "Category": category,
        "Created": created,
    }


def demo_calculate_metrics(request):
    """
    Temporary demo API to test metric calculation for one company (ID=2).
    Calculates metrics for all SKUs that have forecast data for the previous month.
    """

    # --- Hardcoded company ---
    company = CompanyUser.objects.filter(id=2).first()
    if not company:
        return JsonResponse({"error": "Company ID 2 not found"}, status=404)

    # --- Compute previous month ---
    previous_month = date.today().replace(day=1) - relativedelta(months=1)
    target_month_str = previous_month.strftime("%Y-%m")

    # --- Get forecast data for that month ---
    forecasts = SKUForecastHistory.objects.filter(company=company, month=previous_month)
    if not forecasts.exists():
        return JsonResponse({
            "error": f"No forecast data found for {target_month_str}"
        }, status=404)

    # --- Process SKUs ---
    results = []
    for f in forecasts:
        data = calculate_and_store_metrics(company, f.sku, previous_month)
        if data:
            results.append(data)

    return JsonResponse({
        "company": getattr(company, 'company_name', str(company)),
        "month": target_month_str,
        "total_skus_processed": len(results),
        "metrics": results,
    }, safe=False)
# -------------------------------------------------------------------------------------