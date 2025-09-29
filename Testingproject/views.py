# views.py
import os
import json
import requests
from datetime import date
from dateutil.relativedelta import relativedelta
from django.utils import timezone
from django.http import JsonResponse
from collections import defaultdict
from CoreApplication.models import ProductVariant, OrderLineItem, Order, CompanyUser
from Testingproject.models import SKUForecastHistory

# ------------------ Gemini API setup ------------------ #
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"

def call_gemini(prompt_text: str):
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": GEMINI_API_KEY,
    }
    payload = {"contents": [{"parts": [{"text": prompt_text}]}]}
    response = requests.post(GEMINI_ENDPOINT, headers=headers, data=json.dumps(payload))
    
    if response.status_code == 200:
        try:
            result = response.json()
            return result.get("candidates", [])[0]["content"]["parts"][0]["text"]
        except Exception as e:
            return f"Error parsing Gemini response: {str(e)}"
    else:
        return f"Error {response.status_code}: {response.text}"

def clean_gemini_json(response_text):
    cleaned = response_text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1])
    return cleaned

def analyze_error(sku_obj, target_month, predicted_sales, actual_sales):
    prompt_text = f"""
You are analyzing sales predictions for a fashion jewellery SKU.

SKU: {sku_obj.sku} ({sku_obj.title})
Month: {target_month.strftime('%Y-%m')}
Predicted Sales: {predicted_sales}
Actual Sales: {actual_sales}

Explain why the predicted sales were different from the actual sales and give an approximate error percentage.
Output strict JSON in this format:
{{
  "error_reason": "<short explanation of discrepancy>",
  "error_value": <integer error percentage>
}}
"""
    response_text = call_gemini(prompt_text)
    cleaned_response = clean_gemini_json(response_text)
    try:
        gemini_json = json.loads(cleaned_response)
        error_reason = gemini_json.get("error_reason", "")
        error_value = gemini_json.get("error_value", 0)
    except Exception:
        error_reason = cleaned_response
        error_value = 0
    return error_reason, error_value

# ------------------ Forecast for Top 5 SKUs ------------------ #
def forecast_top5_skus(request):
    company_id = 2  # hardcoded tenant
    company = CompanyUser.objects.get(id=company_id)

    # --- Step 1: Get July + August orders ---
    july_start = date(2025, 7, 1)
    august_start = date(2025, 8, 1)
    sept_start = date(2025, 9, 1)

    july_orders = Order.objects.filter(company_id=company_id, order_date__gte=july_start, order_date__lt=august_start)
    august_orders = Order.objects.filter(company_id=company_id, order_date__gte=august_start, order_date__lt=sept_start)

    july_lineitems = OrderLineItem.objects.filter(company_id=company_id, order_id__in=july_orders.values_list("shopify_id", flat=True))
    august_lineitems = OrderLineItem.objects.filter(company_id=company_id, order_id__in=august_orders.values_list("shopify_id", flat=True))

    # --- Step 2: Aggregate total quantities per variant ---
    variant_sales = defaultdict(int)
    for li in list(july_lineitems) + list(august_lineitems):
        if li.variant_id:
            variant_sales[li.variant_id] += li.quantity or 0

    # --- Step 3: Get top 5 SKUs ---
    top5_variant_ids = sorted(variant_sales, key=variant_sales.get, reverse=True)[:5]

    results_all_skus = []

    # --- Step 4: Loop over top 5 SKUs ---
    for variant_id in top5_variant_ids:
        sku_obj = ProductVariant.objects.get(shopify_id=variant_id)
        sku = sku_obj.sku

        past_errors = []  # keep track of previous months for error analysis
        results_all = []

        # --- Step 5: Month loop Jan → Sep 2025 ---
        start_month = date(2025, 1, 1)
        end_month = date(2025, 9, 1)
        current_month = start_month

        while current_month <= end_month:
            # Last 6 months monthly sales
            six_months_ago = current_month - relativedelta(months=6)
            monthly_totals = { (six_months_ago + relativedelta(months=i)).strftime("%Y-%m"):0 for i in range(6) }

            orders_last6 = Order.objects.filter(company_id=company_id, order_date__gte=six_months_ago, order_date__lt=current_month)
            line_items_last6 = OrderLineItem.objects.filter(company_id=company_id, variant_id=sku_obj.shopify_id, order_id__in=orders_last6.values_list("shopify_id", flat=True))
            for li in line_items_last6:
                try:
                    order_date = orders_last6.get(shopify_id=li.order_id).order_date.date()
                    month_key = order_date.strftime("%Y-%m")
                    monthly_totals[month_key] = monthly_totals.get(month_key,0) + (li.quantity or 0)
                except: continue

            # Last 2 months daily sales
            two_months_ago = current_month - relativedelta(months=2)
            daily_totals = {}
            orders_last2 = Order.objects.filter(company_id=company_id, order_date__gte=two_months_ago, order_date__lt=current_month)
            line_items_last2 = OrderLineItem.objects.filter(company_id=company_id, variant_id=sku_obj.shopify_id, order_id__in=orders_last2.values_list("shopify_id", flat=True))
            for li in line_items_last2:
                try:
                    order_date = orders_last2.get(shopify_id=li.order_id).order_date.date()
                    daily_totals[order_date.strftime("%Y-%m-%d")] = daily_totals.get(order_date.strftime("%Y-%m-%d"),0) + (li.quantity or 0)
                except: continue

            # Gemini prediction prompt with past errors
            prompt_text = f"""
You are predicting sales for a fashion jewellery SKU in India. Sales volumes are usually small and highly variable, and depend on Indian festive seasons.

SKU: {sku} ({sku_obj.title})

Last 6 months (monthly totals):
"""
            for m,q in monthly_totals.items(): prompt_text += f"  {m}: {q}\n"
            prompt_text += "\nLast 2 months (daily totals):\n"
            for d,q in sorted(daily_totals.items()): prompt_text += f"  {d}: {q}\n"

            if past_errors:
                prompt_text += "\nPrevious months' predictions & errors:\n"
                for e in past_errors:
                    prompt_text += f"  {e['month']}: predicted {e['predicted_sales']}, actual {e['actual_sales']}, error {e['error_value']}%\n"

            prompt_text += f"""
Predict sales for {current_month.strftime('%Y-%m')}. 
Output ONLY strict JSON in this format:
{{
  "month": "{current_month.strftime('%Y-%m')}",
  "predicted_sales": <number>,
  "reason": "<short business reason for this prediction>"
}}
"""

            gemini_response = call_gemini(prompt_text)
            cleaned_response = clean_gemini_json(gemini_response)
            try:
                gemini_json = json.loads(cleaned_response)
                predicted_sales = gemini_json.get("predicted_sales",0)
                reason = gemini_json.get("reason","")
            except:
                predicted_sales = 0
                reason = cleaned_response

            # Get actual sales for the month
            month_start = current_month
            month_end = month_start + relativedelta(months=1)
            orders_this_month = Order.objects.filter(company_id=company_id, order_date__gte=month_start, order_date__lt=month_end)
            line_items_this_month = OrderLineItem.objects.filter(company_id=company_id, variant_id=sku_obj.shopify_id,
                                                                 order_id__in=orders_this_month.values_list("shopify_id", flat=True))
            actual_sales = sum([li.quantity or 0 for li in line_items_this_month])

            # Store prediction & actual sales
            forecast,_ = SKUForecastHistory.objects.update_or_create(
                company=company,
                sku=sku,
                month=current_month,
                defaults={"predicted_sales":predicted_sales,"reason":reason,"actual_sales":actual_sales}
            )

            # Error analysis
            error_reason, error_value = analyze_error(sku_obj, current_month, predicted_sales, actual_sales)
            forecast.error_reason = error_reason
            forecast.error = error_value
            forecast.save()

            # Append to past_errors for next month prompt
            past_errors.append({
                "month": current_month.strftime("%Y-%m"),
                "predicted_sales": predicted_sales,
                "actual_sales": actual_sales,
                "error_value": error_value
            })

            results_all.append({
                "sku": sku,
                "month": current_month.strftime("%Y-%m"),
                "predicted_sales": predicted_sales,
                "reason": reason,
                "actual_sales": actual_sales,
                "error_reason": error_reason,
                "error_value": error_value
            })

            current_month += relativedelta(months=1)

        results_all_skus.extend(results_all)

    return JsonResponse(results_all_skus, safe=False)



# ------------------ Updated prediction_chart_view ------------------ #
from django.shortcuts import render
def prediction_chart_view(request):
    company_id = 2  # hardcoded tenant
    sku_list = SKUForecastHistory.objects.values_list("sku", flat=True).distinct()  # ensures unique SKUs
    sku_list = sorted(set(sku_list))
    graph_data = {}
    for sku in sku_list:
        history = SKUForecastHistory.objects.filter(company_id=company_id, sku=sku).order_by("month")
        months = [h.month.strftime("%b %Y") for h in history]
        actual = [h.actual_sales for h in history]
        predicted = [h.predicted_sales for h in history]
        # Include reason, error, error_reason for tooltip
        details = [{"reason": h.reason or "", "error": h.error or 0, "error_reason": h.error_reason or ""} for h in history]

        graph_data[sku] = {
            "months": months,
            "actual": actual,
            "predicted": predicted,
            "details": details
        }

    return render(request, "Testingproject/prediction_chart.html", {
        "sku_list": sku_list,
        "graph_data": graph_data
    })
