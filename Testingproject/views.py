import os
import json
import requests
from datetime import date
from dateutil.relativedelta import relativedelta
from django.http import JsonResponse
from collections import defaultdict
from CoreApplication.models import (
    ProductVariant, Product, OrderLineItem, Order, CompanyUser, Prompt, PromotionalData
)
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
    try:
        response = requests.post(GEMINI_ENDPOINT, headers=headers, data=json.dumps(payload))
        if response.status_code == 200:
            result = response.json()
            return result.get("candidates", [])[0]["content"]["parts"][0]["text"]
        else:
            return f"Error {response.status_code}: {response.text}"
    except Exception as e:
        return f"Exception calling Gemini: {str(e)}"

def clean_gemini_json(response_text):
    cleaned = response_text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            cleaned = "\n".join(lines[1:-1])
    return cleaned

def count_tokens(text: str) -> int:
    """Rough estimate: 1 token ≈ 4 characters"""
    if not text:
        return 0
    return max(1, len(text) // 4)

def analyze_error(sku_obj, target_month, predicted_sales, actual_sales):
    prompt_text = f"""
You are analyzing sales predictions for a fashion jewellery SKU.

SKU: {sku_obj.sku} ({sku_obj.title})
Month: {target_month.strftime('%Y-%m')}
Predicted Sales: {predicted_sales}
Actual Sales: {actual_sales}

You predicted the sales. Explain why the predicted sales were different from the actual sales and give an approximate error percentage.
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

# ------------------ Forecast Top SKUs ------------------ #
def forecast_top5_skus(request):
    company_id = 2  # hardcoded tenant

    # Step 0: Fetch company
    try:
        company = CompanyUser.objects.get(id=company_id)
    except CompanyUser.DoesNotExist:
        return JsonResponse({"debug_step": "Fetch company", "error": f"Company ID {company_id} not found"}, status=404)

    # Step 1: Fetch base prompt
    try:
        base_prompt_obj = Prompt.objects.get(id=2)
        if not base_prompt_obj.prompt:
            return JsonResponse({"debug_step": "Fetch prompt", "error": "Prompt is empty"}, status=400)
    except Prompt.DoesNotExist:
        return JsonResponse({"debug_step": "Fetch prompt", "error": "Prompt ID 2 not found"}, status=404)

    base_prompt = base_prompt_obj.prompt

    # Step 2: Fetch Orders + LineItems for July + August 2025
    try:
        july_orders = Order.objects.filter(company=company, order_date__gte=date(2025,7,1), order_date__lt=date(2025,8,1))
        august_orders = Order.objects.filter(company=company, order_date__gte=date(2025,8,1), order_date__lt=date(2025,9,1))

        july_lineitems = OrderLineItem.objects.filter(company=company, order_id__in=july_orders.values_list("shopify_id", flat=True))
        august_lineitems = OrderLineItem.objects.filter(company=company, order_id__in=august_orders.values_list("shopify_id", flat=True))
    except Exception as e:
        return JsonResponse({"debug_step": "Fetch orders/lineitems", "error": str(e)}, status=500)

    # Step 3: Aggregate sales per variant
    variant_sales = defaultdict(int)
    for li in list(july_lineitems) + list(august_lineitems):
        if li.variant_id:
            variant_sales[li.variant_id] += li.quantity or 0

    if not variant_sales:
        return JsonResponse({"debug_step": "Aggregate sales", "error": "No variant sales found for July/August"}, status=404)

    top_variant_ids = sorted(variant_sales, key=variant_sales.get, reverse=True)[:50]

    # ------------------ Bulk fetch all data ------------------ #
    all_orders = Order.objects.filter(
        company=company,
        order_date__gte=date(2024,7,1),  # go back 6 months before Jan 2025
        order_date__lt=date(2025,10,1)
    ).only("shopify_id", "order_date")

    order_map = {o.shopify_id: o for o in all_orders}

    all_lineitems = OrderLineItem.objects.filter(
        company=company,
        variant_id__in=top_variant_ids,
        order_id__in=order_map.keys()
    ).only("variant_id", "order_id", "quantity")

    # Pre-compute sales per month/day
    monthly_sales_map = defaultdict(lambda: defaultdict(int))
    daily_sales_map = defaultdict(lambda: defaultdict(int))
    for li in all_lineitems:
        order_obj = order_map.get(li.order_id)
        if not order_obj:
            continue
        month_key = order_obj.order_date.strftime("%Y-%m")
        day_key = order_obj.order_date.strftime("%Y-%m-%d")
        monthly_sales_map[li.variant_id][month_key] += li.quantity or 0
        daily_sales_map[li.variant_id][day_key] += li.quantity or 0

    # Preload variants & products
    variants = {v.shopify_id: v for v in ProductVariant.objects.filter(company=company, shopify_id__in=top_variant_ids)}
    products = {p.shopify_id: p for p in Product.objects.filter(company=company, shopify_id__in=[v.product_id for v in variants.values()])}

    # ------------------ Bulk fetch promos ------------------ #
    all_promos = PromotionalData.objects.filter(
        user_id=company,
        variant_id__in=top_variant_ids
    ).order_by('-date')

    promos_by_variant = defaultdict(list)
    for promo in all_promos:
        promos_by_variant[promo.variant_id].append(promo)

    results_all_skus = []

    # Step 4: Loop over top SKUs
    for variant_id in top_variant_ids:
        sku_obj = variants.get(variant_id)
        if not sku_obj:
            continue

        sku = sku_obj.sku
        product_obj = products.get(sku_obj.product_id)

        product_data = {
            "title": product_obj.title if product_obj else None,
            "vendor": product_obj.vendor if product_obj else None,
            "product_type": product_obj.product_type if product_obj else None,
            "tags": product_obj.tags.split(",") if product_obj and product_obj.tags else [],
            "created_at": str(product_obj.created_at.date()) if product_obj and product_obj.created_at else None,
            "status": product_obj.status if product_obj else None
        }

        # ✅ Get promo data from preloaded dict
        promo_objs = promos_by_variant.get(sku_obj.shopify_id, [])[:90]
        promo_data_list, marketing_campaign_flag = [], False
        for promo in promo_objs:
            promo_data_list.append({
                "date": str(promo.date),
                "clicks": promo.clicks,
                "impressions": promo.impressions,
                "avg_cpc": float(promo.avg_cpc) if promo.avg_cpc else None,
                "cost": float(promo.cost) if promo.cost else None,
                "conversions": promo.conversions,
                "conversion_value": float(promo.conversion_value) if promo.conversion_value else None,
                "conv_value_per_cost": float(promo.conv_value_per_cost) if promo.conv_value_per_cost else None,
                "cost_per_conversion": float(promo.cost_per_conversion) if promo.cost_per_conversion else None,
                "conversion_rate": float(promo.conversion_rate) if promo.conversion_rate else None,
                "price": float(promo.price) if promo.price else None
            })
            if promo.clicks > 0 or promo.impressions > 0 or (promo.cost and promo.cost > 0):
                marketing_campaign_flag = True

        past_errors = []
        results_all = []
        current_month = date(2025,1,1)
        end_month = date(2025,9,1)

        while current_month <= end_month:
            # Build monthly + daily totals from precomputed maps
            six_months_ago = current_month - relativedelta(months=6)
            monthly_totals = { (six_months_ago + relativedelta(months=i)).strftime("%Y-%m"):
                               monthly_sales_map[variant_id].get((six_months_ago + relativedelta(months=i)).strftime("%Y-%m"), 0)
                               for i in range(6) }

            two_months_ago = current_month - relativedelta(months=2)
            daily_totals = { (two_months_ago + relativedelta(days=i)).strftime("%Y-%m-%d"):
                             daily_sales_map[variant_id].get((two_months_ago + relativedelta(days=i)).strftime("%Y-%m-%d"), 0)
                             for i in range((current_month - two_months_ago).days) }

            # ------------------ Generate Prompt ------------------ #
            data_for_prompt = {
                "sku": sku,
                "variant_title": sku_obj.title,
                "price": float(sku_obj.price) if sku_obj.price else None,
                "compare_at_price": float(sku_obj.compare_at_price) if sku_obj.compare_at_price else None,
                "cost": float(sku_obj.cost) if sku_obj.cost else None,
                "inventory_quantity": sku_obj.inventory_quantity,
                "created_at": str(sku_obj.created_at.date()) if sku_obj.created_at else None,
                "product": product_data,
                "monthly_sales_last_6_months": monthly_totals,
                "daily_sales_last_2_months": daily_totals,
                "previous_errors": past_errors,
                "promotional_data_last_3_months": promo_data_list,
                "marketing_campaign_flag": marketing_campaign_flag
            }

            generate_prompt_text = f"{base_prompt}\n\n{json.dumps(data_for_prompt, indent=2)}\nOutput a clear forecasting prompt in strict JSON format."
            generated_prompt_response = call_gemini(generate_prompt_text)
            generated_prompt_cleaned = clean_gemini_json(generated_prompt_response)

            try:
                base_prompt_obj.generated_prompt = generated_prompt_cleaned
                base_prompt_obj.save()
            except Exception as e:
                return JsonResponse({"debug_step": "Save generated_prompt", "error": str(e)}, status=500)

            final_prompt = f"{generated_prompt_cleaned}\n\n{json.dumps(data_for_prompt, indent=2)}\nOutput ONLY strict JSON in this format:\n{{\n  \"month\": \"{current_month.strftime('%Y-%m')}\",\n  \"predicted_sales\": <number>,\n  \"reason\": \"<short business reason>\"\n}}"

            gemini_response = call_gemini(final_prompt)
            cleaned_response = clean_gemini_json(gemini_response)

            input_tokens = count_tokens(final_prompt)
            output_tokens = count_tokens(cleaned_response)

            try:
                gemini_json = json.loads(cleaned_response)
                predicted_sales = gemini_json.get("predicted_sales",0)
                reason = gemini_json.get("reason","")
            except:
                predicted_sales = 0
                reason = cleaned_response

            actual_sales = monthly_sales_map[variant_id].get(current_month.strftime("%Y-%m"), 0)

            forecast, _ = SKUForecastHistory.objects.update_or_create(
                company=company,
                sku=sku,
                month=current_month,
                defaults={"predicted_sales": predicted_sales,"reason":reason,"actual_sales":actual_sales}
            )

            error_reason, error_value = analyze_error(sku_obj, current_month, predicted_sales, actual_sales)
            forecast.error_reason = error_reason
            forecast.error = error_value
            forecast.save()

            past_errors.append({
                "month": current_month.strftime("%Y-%m"),
                "predicted_sales": predicted_sales,
                "actual_sales": actual_sales,
                "error_value": error_value,
                "reason": reason,
                "error_reason": error_reason
            })

            results_all.append({
                "sku": sku,
                "month": current_month.strftime("%Y-%m"),
                "predicted_sales": predicted_sales,
                "reason": reason,
                "actual_sales": actual_sales,
                "error_reason": error_reason,
                "error_value": error_value,
                "data_sent": data_for_prompt,
                "generated_prompt": generated_prompt_cleaned,
                "final_prompt_sent": final_prompt,
                "tokens": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens
                }
            })

            current_month += relativedelta(months=1)

        results_all_skus.extend(results_all)

    return JsonResponse({"debug": "Completed successfully", "results": results_all_skus}, safe=False)



# Accuracy Metrics Calculation function
# Month-wise WMAPE, MAE, Bias Calculation



# views.py
from django.shortcuts import render
from Testingproject.models import SKUForecastHistory
from django.db.models import Sum, F
import json

def WMAPE_Calculation(request):
    company_id = 2  # replace with dynamic tenant if needed

    # Fetch monthly aggregated metrics dynamically from DB
    # Here assuming you have fields in SKUForecastHistory like error, predicted_sales, actual_sales
    # You can calculate WMAPE, MAE, Bias per month
    metrics_data = {}

    forecasts = SKUForecastHistory.objects.filter(company_id=company_id).order_by("month")
    months = sorted(forecasts.values_list("month", flat=True).distinct())

    for month in months:
        month_forecasts = forecasts.filter(month=month)
        actual_total = sum([f.actual_sales for f in month_forecasts])
        predicted_total = sum([f.predicted_sales for f in month_forecasts])
        errors = [abs(f.actual_sales - f.predicted_sales) for f in month_forecasts]

        # WMAPE = sum(|actual - predicted|) / sum(actual) * 100
        wmape = (sum(errors) / actual_total * 100) if actual_total else 0

        # MAE = mean absolute error
        mae = (sum(errors) / len(errors)) if errors else 0

        # Bias = sum(predicted - actual) / sum(actual) * 100
        bias = (sum([f.predicted_sales - f.actual_sales for f in month_forecasts]) / actual_total * 100) if actual_total else 0

        metrics_data[month.strftime("%Y-%m")] = {
            "WMAPE": round(wmape, 2),
            "MAE": round(mae, 2),
            "Bias": round(bias, 2)
        }

    context = {
        "metrics_json": json.dumps(metrics_data)
    }
    return render(request, "Testingproject/prediction_chart.html", context)




# views.py

from django.shortcuts import render
from collections import defaultdict
from statistics import median
from Testingproject.models import SKUForecastHistory

def median_metrics_chart_view(request):
    """
    Calculate median WMAPE, MAE, and Bias per month across all SKUs
    and render the chart template.
    """
    forecasts = SKUForecastHistory.objects.all().values(
        "month", "predicted_sales", "actual_sales"
    )

    month_sku_data = defaultdict(list)

    # Organize SKU-level errors per month
    for f in forecasts:
        month_str = f["month"].strftime("%Y-%m")
        predicted = f["predicted_sales"] or 0
        actual = f["actual_sales"] or 0
        abs_error = abs(actual - predicted)
        bias = predicted - actual
        mae = abs_error

        month_sku_data[month_str].append({
            "WMAPE": (abs_error / actual * 100) if actual else 0,
            "MAE": mae,
            "Bias": bias
        })

    # Calculate median metrics
    median_results = {}
    for month, data_list in month_sku_data.items():
        wmape_values = [d["WMAPE"] for d in data_list]
        mae_values = [d["MAE"] for d in data_list]
        bias_values = [d["Bias"] for d in data_list]

        median_results[month] = {
            "Median_WMAPE": round(median(wmape_values), 2) if wmape_values else None,
            "Median_MAE": round(median(mae_values), 2) if mae_values else None,
            "Median_Bias": round(median(bias_values), 2) if bias_values else None
        }

    # Render the HTML template with median results
    return render(request, "Testingproject/Median_charts.html", {
        "metrics_json": median_results
    })



# views.py
import json
from collections import defaultdict
from django.shortcuts import render
from Testingproject.models import SKUForecastHistory

def sku_predictions_per_month(request):
    """
    Render template with predictions per month for SKUs.
    """
    company_id = 2  # replace with dynamic tenant logic if needed
    forecasts = SKUForecastHistory.objects.filter(company_id=company_id).order_by("month", "sku")

    results = defaultdict(list)

    for f in forecasts:
        month_str = f.month.strftime("%Y-%m")
        results[month_str].append({
            "sku": f.sku,
            "predicted_sales": f.predicted_sales,
            "actual_sales": f.actual_sales,
            "error": f.error,
            "error_reason": f.error_reason,
            "reason": f.reason
        })

    # ✅ convert to normal dict + JSON string
    results_json = json.dumps(dict(results))

    return render(request, "Testingproject/Predictions_table.html", {
        "predictions_json": results_json
    })





# -------------------------------------------------------------------------------

