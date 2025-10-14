from django.db.models import Sum
from Testingproject.models import SKUForecastMetrics, SKUForecastHistory
from CoreApplication.models import CompanyUser, Order, OrderLineItem, Product, ProductVariant
import statistics
from datetime import datetime, date
from django.conf import settings
from cryptography.fernet import Fernet
from datetime import date, datetime, timedelta
from django.db.models import Max, Sum
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from dateutil.relativedelta import relativedelta

from CoreApplication.models import PurchaseOrder, ProductVariant, Product
from Testingproject.models import InventoryValuation, SKUForecastHistory
from CoreApplication.views import get_user_from_token


import json
from datetime import date, datetime
from dateutil.relativedelta import relativedelta
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Sum, Max
from CoreApplication.models import ProductVariant, Product, PurchaseOrder
from Testingproject.models import InventoryValuation, SKUForecastHistory
from CoreApplication.views import get_user_from_token

# ---------------- Helper: SKU Normalizer ---------------- #


def normalize_sku(sku):
    """Normalize SKUs for consistent comparison."""
    if not sku:
        return ""
    return str(sku).strip().upper().replace(" ", "").replace("-", "").replace("_", "")


# ---------------- Helper: Safe date parser ---------------- #
def safe_parse_date(date_val):
    """Convert string or date/datetime to a date object."""
    if not date_val:
        return None

    # Already date
    if isinstance(date_val, date):
        return date_val

    # Datetime
    if isinstance(date_val, datetime):
        return date_val.date()

    # String in YYYY-MM-DD
    try:
        return datetime.strptime(str(date_val).strip(), "%Y-%m-%d").date()
    except Exception:
        pass

    # Fallback: dd.mm.yyyy
    try:
        return datetime.strptime(str(date_val).strip(), "%d.%m.%Y").date()
    except Exception:
        pass

    # Fallback: dd-mm-yyyy
    try:
        return datetime.strptime(str(date_val).strip(), "%d-%m-%Y").date()
    except Exception:
        pass

    print(f"⚠️ Could not parse date: '{date_val}'")
    return None


# ---------------- Main View ---------------- #
@csrf_exempt
def inventory_reorder_report(request):
    """Reorder and slow movers report with corrected OnOrder calculation."""
    user, error = get_user_from_token(request)
    if error:
        return error

    company = user
    today = date.today()

    # ---------------- Latest Forecasts ---------------- #
    latest_month = (
        SKUForecastHistory.objects.filter(company=company)
        .aggregate(latest_month=Max("month"))
        .get("latest_month")
    )
    if not latest_month:
        return JsonResponse({"error": "No forecasts found for this company."}, status=404)

    latest_forecasts = SKUForecastHistory.objects.filter(
        company=company, month=latest_month
    )
    forecast_skus = [normalize_sku(f.sku) for f in latest_forecasts]

    # ---------------- On-Order Calculation ---------------- #
    print("\n==== DEBUG: START ON-ORDER CHECK ====")
    print(f"Authenticated company.id = {company.id}")
    print(f"Today's Date = {today}")
    print(f"Forecast SKUs = {forecast_skus}")

    purchase_orders = PurchaseOrder.objects.filter(
        company_id=company.id, sku_id__in=[f.sku for f in latest_forecasts]
    )
    print(f"Found {purchase_orders.count()} POs for company_id={company.id}\n")

    on_orders = {}
    order_details = {}

    for po in purchase_orders:
        print(f"--- Checking PO ---")
        print(f"PO ID: {po.id}, PO Number: {po.purchase_order_id}")
        print(f"Company ID in DB: {po.company_id}")
        print(f"SKU: {po.sku_id}")
        print(
            f"Order Date: {po.order_date}, Delivery Date: {po.delivery_date}")

        delivery_date = safe_parse_date(po.delivery_date)
        print(f"Parsed Delivery Date: {delivery_date}")

        sku_norm = normalize_sku(po.sku_id)

        if not delivery_date:
            print("❌ Skipping — Invalid delivery date format\n")
            continue

        if delivery_date < today:
            print("❌ Skipping — Past delivery date\n")
            continue

        try:
            qty = int(float(str(po.quantity_ordered).strip()))
        except Exception:
            qty = 0

        on_orders[sku_norm] = on_orders.get(sku_norm, 0) + qty
        print(
            f"✅ Added {qty} units to SKU {sku_norm}, Total OnOrder now: {on_orders[sku_norm]}\n")

        order_details.setdefault(sku_norm, []).append({
            "purchase_order_id": po.purchase_order_id,
            "supplier_name": po.supplier_name,
            "order_date": str(po.order_date),
            "delivery_date": str(po.delivery_date),
            "quantity_ordered": qty,
        })

    print("---- DEBUG SUMMARY ----")
    print(f"Final OnOrder Map: {on_orders}")
    print("---- DEBUG END ON-ORDER CHECK ----\n")

    # ---------------- Build Forecast Data ---------------- #
    forecast_data = []
    for f in latest_forecasts:
        sku_norm = normalize_sku(f.sku)
        forecast_30 = f.predicted_sales_30 or 0
        forecast_60 = f.predicted_sales_60 or 0
        forecast_90 = f.predicted_sales_90 or 0
        live_inventory = f.live_inventory or 0
        on_order = on_orders.get(sku_norm, 0)
        reason = f.reason or ""

        inv_for_calc = live_inventory if live_inventory != -1 else 0
        reorder_qty = max((forecast_30 + forecast_60) -
                          (inv_for_calc + on_order), 0)

        total_available = inv_for_calc + on_order
        if total_available <= (forecast_30 / 3):
            action_item = "StockOut Risk"
        elif total_available <= (forecast_30 / 2):
            action_item = "Reorder Now"
        else:
            action_item = "Sufficient Stock"

        try:
            variant = ProductVariant.objects.get(company=company, sku=f.sku)
            price = float(variant.price) if variant.price else 0.0
            product_id = variant.product_id
            variant_title = variant.title or ""
            product = Product.objects.filter(
                company=company, shopify_id=product_id).first()
            category = product.product_type if product else None
            product_title = product.title if product else ""
        except ProductVariant.DoesNotExist:
            price = 0.0
            category = None
            product_title = ""
            variant_title = ""

        forecast_data.append({
            "SKU": f.sku,
            "Product": product_title,
            "Variant": variant_title,
            "Category": category,
            "Price": price,
            "Forecast_30": forecast_30,
            "Forecast_60": forecast_60,
            "Forecast_90": forecast_90,
            "Live_Inventory": live_inventory,
            "OnOrder": on_order,
            "Reorder_Quantity": reorder_qty,
            "Action_Item": action_item,
            "Reason": reason,
            "PurchaseOrders": order_details.get(sku_norm, []),
        })

    # ---------------- Slow Movers ---------------- #
    start_month = today.replace(day=1) - relativedelta(months=3)
    end_month = today.replace(day=1)
    sales_summary = (
        SKUForecastHistory.objects.filter(
            company=company, month__gte=start_month, month__lt=end_month)
        .values("sku")
        .annotate(total_sales=Sum("actual_sales_30"))
    )
    sold_map = {normalize_sku(s["sku"]): s["total_sales"]
                or 0 for s in sales_summary}
    slow_threshold = 3

    slow_movers = []
    for f in latest_forecasts:
        sku_norm = normalize_sku(f.sku)
        live_inventory = f.live_inventory or 0
        total_sold = sold_map.get(sku_norm, 0)
        if total_sold < slow_threshold:
            try:
                variant = ProductVariant.objects.get(
                    company=company, sku=f.sku)
                price = float(variant.price) if variant.price else 0.0
                product_id = variant.product_id
                variant_title = variant.title or ""
                product = Product.objects.filter(
                    company=company, shopify_id=product_id).first()
                category = product.product_type if product else None
                product_title = product.title if product else ""
            except ProductVariant.DoesNotExist:
                price = 0.0
                category = None
                product_title = ""
                variant_title = ""
            slow_movers.append({
                "SKU": f.sku,
                "Product": product_title,
                "Variant": variant_title,
                "Category": category,
                "Price": price,
                "Live_Inventory": live_inventory,
                "Sales_Last_3_Months": total_sold,
            })

    # ---------------- Summary ---------------- #
    slow_movers_count = len(slow_movers)
    risk_alerts_count = sum(
        1 for f in forecast_data if f["Action_Item"] == "StockOut Risk")
    reorder_needed_count = sum(1 for f in forecast_data if f["Action_Item"] in [
                               "Reorder Now", "StockOut Risk"])

    latest_inventory = InventoryValuation.objects.filter(
        company=company).order_by('-month').first()
    if latest_inventory:
        inventory_info = {
            "month": latest_inventory.month.strftime("%Y-%m"),
            "inventory_value": latest_inventory.inventory_value,
            "currency": latest_inventory.currency,
        }
    else:
        inventory_info = None

    # ---------------- Final Response ---------------- #
    return JsonResponse({
        "summary": {
            "slow_movers_count": slow_movers_count,
            "risk_alerts_count": risk_alerts_count,
            "reorder_needed_count": reorder_needed_count,
            "latest_inventory": inventory_info,
        },
        "forecasts": forecast_data,
        "slow_movers": slow_movers,
    }, safe=False)


@csrf_exempt
def get_slow_movers(request):
    """
    API to fetch only Slow Movers.
    - Auth via JWT token
    - Logic: SKUs with < 3 units sold in past 3 full months
    - Additionally: Includes last 12 months' month-wise sales with year
    """
    # ---------------- Auth ---------------- #
    user, error = get_user_from_token(request)
    if error:
        return error
    company = user

    today = date.today()
    current_month_start = today.replace(day=1)

    # 3-month window for slow mover logic
    start_month_3 = current_month_start - relativedelta(months=3)

    # 12-month window for detailed sales
    start_month_12 = current_month_start - relativedelta(months=12)

    # ---------------- Get past 3 months total sales ---------------- #
    sales_summary_3 = (
        SKUForecastHistory.objects.filter(
            company=company,
            month__gte=start_month_3,
            month__lt=current_month_start
        )
        .values("sku")
        .annotate(total_sales=Sum("actual_sales_30"))
    )
    sold_map_3 = {s["sku"]: s["total_sales"] or 0 for s in sales_summary_3}

    # ---------------- Get last 12 months month-wise sales ---------------- #
    month_sales = (
        SKUForecastHistory.objects.filter(
            company=company,
            month__gte=start_month_12,
            month__lt=current_month_start
        )
        .values("sku", "month")
        .annotate(month_sales=Sum("actual_sales_30"))
        .order_by("sku", "month")
    )
    monthwise_sales_map = {}
    for s in month_sales:
        sku = s["sku"]
        month_str = s["month"].strftime("%b %Y")  # e.g., "Oct 2024"
        sales_value = s["month_sales"] or 0
        if sku not in monthwise_sales_map:
            monthwise_sales_map[sku] = {}
        monthwise_sales_map[sku][month_str] = sales_value

    # ---------------- Identify Slow Movers ---------------- #
    slow_threshold = 3  # same as before
    slow_movers = []

    for sku, total_sold in sold_map_3.items():
        if total_sold < slow_threshold:
            try:
                variant = ProductVariant.objects.get(company=company, sku=sku)
                price = float(variant.price) if variant.price else 0.0
                live_inventory = variant.inventory_quantity or 0
                product_id = variant.product_id
                variant_title = variant.title or ""

                product = Product.objects.filter(
                    company=company, shopify_id=product_id).first()
                category = product.product_type if product else None
                product_title = product.title if product else ""
            except ProductVariant.DoesNotExist:
                price = 0.0
                live_inventory = 0
                category = None
                product_title = ""
                variant_title = ""

            slow_movers.append({
                "SKU": sku,
                "Product": product_title,
                "Variant": variant_title,
                "Category": category,
                "Price": price,
                "Live_Inventory": live_inventory,
                "Sales_Last_3_Months_Total": total_sold,
                "Sales_Last_12_Months": monthwise_sales_map.get(sku, {}),
            })

    # ---------------- Final Response ---------------- #
    slow_movers_count = len(slow_movers)
    return JsonResponse({
        "company_id": company.id,
        "slow_movers_count": slow_movers_count,
        "slow_movers": slow_movers,
    }, safe=False)


# Risk Alerts API function


def safe_parse_date(date_str):
    """Helper to safely parse dates in multiple formats."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except Exception:
            continue
    return None


@csrf_exempt
def get_risk_alerts(request):
    """
    API to fetch Risk Alerts (only StockOut Risk) for a company.
    - Auth via JWT token
    - Includes SKU, Product info, live inventory, on-order, forecast_30, forecast_60
    """
    # ---------------- Auth ---------------- #
    user, error = get_user_from_token(request)
    if error:
        return error
    company = user
    today = date.today()

    # ---------------- Latest Forecasts ---------------- #
    latest_month = (
        SKUForecastHistory.objects.filter(company=company)
        .aggregate(latest_month=Max("month"))
        .get("latest_month")
    )
    if not latest_month:
        return JsonResponse({"error": "No forecasts found for this company."}, status=404)

    latest_forecasts = SKUForecastHistory.objects.filter(
        company=company, month=latest_month)

    # ---------------- Build Risk Alerts ---------------- #
    risk_alerts = []
    for f in latest_forecasts:
        sku = f.sku
        forecast_30 = f.predicted_sales_30 or 0
        forecast_60 = f.predicted_sales_60 or 0
        live_inventory = f.live_inventory or 0

        # ---------------- On-Order Quantities ---------------- #
        on_order = 0
        purchase_orders = PurchaseOrder.objects.filter(
            company=company, sku_id=sku)
        for po in purchase_orders:
            delivery_date = safe_parse_date(po.delivery_date)
            if delivery_date and delivery_date >= today:
                on_order += po.quantity_ordered

        total_available = live_inventory + on_order

        # Only StockOut Risk SKUs
        if total_available > (forecast_30 / 3):
            continue

        action_item = "StockOut Risk"

        try:
            variant = ProductVariant.objects.get(company=company, sku=sku)
            price = float(variant.price) if variant.price else 0.0
            variant_title = variant.title or ""
            product = Product.objects.filter(
                company=company, shopify_id=variant.product_id).first()
            category = product.product_type if product else None
            product_title = product.title if product else ""
        except ProductVariant.DoesNotExist:
            price = 0.0
            variant_title = ""
            category = None
            product_title = ""

        risk_alerts.append({
            "SKU": sku,
            "Product": product_title,
            "Variant": variant_title,
            "Category": category,
            "Price": price,
            "Live_Inventory": live_inventory,
            "OnOrder": on_order,
            "Forecast_30": forecast_30,
            "Forecast_60": forecast_60,
            "Action_Item": action_item
        })

    # ---------------- Count summary ---------------- #
    stockout_count = len(risk_alerts)

    return JsonResponse({
        "company_id": company.id,
        "stockout_count": stockout_count,
        "risk_alerts": risk_alerts
    }, safe=False)


def safe_parse_date(date_str):
    """Helper to safely parse dates in multiple formats."""
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt).date()
        except Exception:
            continue
    return None


@csrf_exempt
def get_need_reordering(request):
    """
    API to fetch Need Reordering SKUs (Reorder Now / Sufficient Stock) for a company.
    - Auth via JWT token
    - Includes SKU, Product info, live inventory, on-order, forecast_30, forecast_60
    """
    # ---------------- Auth ---------------- #
    user, error = get_user_from_token(request)
    if error:
        return error
    company = user
    today = date.today()

    # ---------------- Latest Forecasts ---------------- #
    latest_month = (
        SKUForecastHistory.objects.filter(company=company)
        .aggregate(latest_month=Max("month"))
        .get("latest_month")
    )
    if not latest_month:
        return JsonResponse({"error": "No forecasts found for this company."}, status=404)

    latest_forecasts = SKUForecastHistory.objects.filter(
        company=company, month=latest_month)

    # ---------------- Build Need Reordering ---------------- #
    need_reordering = []
    for f in latest_forecasts:
        sku = f.sku
        forecast_30 = f.predicted_sales_30 or 0
        forecast_60 = f.predicted_sales_60 or 0
        live_inventory = f.live_inventory or 0

        # ---------------- On-Order Quantities ---------------- #
        on_order = 0
        purchase_orders = PurchaseOrder.objects.filter(
            company=company, sku_id=sku)
        for po in purchase_orders:
            delivery_date = safe_parse_date(po.delivery_date)
            if delivery_date and delivery_date >= today:
                on_order += po.quantity_ordered

        total_available = live_inventory + on_order

        # Skip StockOut Risk SKUs (these are handled in Risk Alerts)
        if total_available <= (forecast_30 / 3):
            continue

        # Only include SKUs that may need reordering
        if total_available <= forecast_30:
            action_item = "Reorder Now"
        else:
            action_item = "Sufficient Stock"

        try:
            variant = ProductVariant.objects.get(company=company, sku=sku)
            price = float(variant.price) if variant.price else 0.0
            variant_title = variant.title or ""
            product = Product.objects.filter(
                company=company, shopify_id=variant.product_id).first()
            category = product.product_type if product else None
            product_title = product.title if product else ""
        except ProductVariant.DoesNotExist:
            price = 0.0
            variant_title = ""
            category = None
            product_title = ""

        need_reordering.append({
            "SKU": sku,
            "Product": product_title,
            "Variant": variant_title,
            "Category": category,
            "Price": price,
            "Live_Inventory": live_inventory,
            "OnOrder": on_order,
            "Forecast_30": forecast_30,
            "Forecast_60": forecast_60,
            "Action_Item": action_item
        })

    # ---------------- Count summary ---------------- #
    reorder_count = len(need_reordering)

    return JsonResponse({
        "company_id": company.id,
        "reorder_count": reorder_count,
        "need_reordering": need_reordering
    }, safe=False)


@csrf_exempt
def CompanyDashboardMetricsView(request):
    # ---------------- Auth ---------------- #
    user, error = get_user_from_token(request)
    if error:
        return error  # Already a JsonResponse if invalid

    if not user.shopify_access_token or not user.shopify_store_url:
        return JsonResponse({"error": "No Shopify credentials found"}, status=404)

    try:
        fernet = Fernet(settings.ENCRYPTION_KEY)
        shopify_access_token = fernet.decrypt(
            user.shopify_access_token.encode()).decode()
        shopify_url = fernet.decrypt(user.shopify_store_url.encode()).decode()
    except Exception as e:
        return JsonResponse({"error": f"Failed to decrypt credentials: {str(e)}"}, status=500)

    # ---------------- Determine currency ---------------- #
    order = Order.objects.filter(company=user).first()
    currency = order.currency if order else "USD"

    # ---------------- Determine dashboard month ---------------- #
    month_str = request.GET.get("month")  # Expect format "YYYY-MM"
    if month_str:
        try:
            dashboard_month = datetime.strptime(
                month_str, "%Y-%m").date().replace(day=1)
        except ValueError:
            return JsonResponse({"error": "Invalid month format. Use YYYY-MM."}, status=400)
    else:
        # Default: pick last available month
        latest_metric_entry = SKUForecastMetrics.objects.filter(
            company=user).order_by('-month').first()
        if not latest_metric_entry:
            return JsonResponse({
                "message": "No SKU metrics found for this company.",
                "company": getattr(user, "company_name", str(user))
            }, status=404)
        dashboard_month = latest_metric_entry.month

    # ---------------- Fetch metrics for dashboard_month ---------------- #
    metrics = SKUForecastMetrics.objects.filter(
        company=user, month=dashboard_month)

    acc_values, bias_values, doi_values, str_values, it_values = [], [], [], [], []

    for m in metrics:
        if m.forecast_accuracy is not None and -100 <= m.forecast_accuracy <= 100:
            acc_values.append(m.forecast_accuracy)
        if m.forecast_bias is not None and -100 <= m.forecast_bias <= 100:
            bias_values.append(m.forecast_bias)
        if m.days_of_inventory is not None and 0 <= m.days_of_inventory <= 365:
            doi_values.append(m.days_of_inventory)
        if m.sell_through_rate is not None and 0 <= m.sell_through_rate <= 100:
            str_values.append(m.sell_through_rate)
        if m.inventory_turnover is not None and 0 <= m.inventory_turnover <= 50:
            it_values.append(m.inventory_turnover)

    def safe_median(values):
        return round(statistics.median(values), 2) if values else None

    def safe_average(values):
        return round(sum(values) / len(values), 2) if values else None

    # ---------------- Orders & Line Items for selected month ---------------- #
    start_month = dashboard_month.replace(day=1)
    if dashboard_month.month == 12:
        end_month = dashboard_month.replace(
            year=dashboard_month.year + 1, month=1, day=1)
    else:
        end_month = dashboard_month.replace(
            month=dashboard_month.month + 1, day=1)

    orders = Order.objects.filter(
        company=user,
        order_date__gte=start_month,
        order_date__lt=end_month
    )
    order_ids = list(orders.values_list("shopify_id", flat=True))
    line_items = OrderLineItem.objects.filter(
        company=user, order_id__in=order_ids)

    top_categories_data = []
    category_wise_performance = []

    if line_items.exists():
        product_ids = [
            item.product_id for item in line_items if item.product_id]
        products = Product.objects.filter(
            company=user, shopify_id__in=product_ids)
        product_categories = {
            p.shopify_id: p.product_type or "Unknown" for p in products}

        category_units = {}
        category_revenue = {}
        category_skus = {}

        for item in line_items:
            category = product_categories.get(item.product_id, "Unknown")
            category_units[category] = category_units.get(
                category, 0) + (item.quantity or 0)
            category_revenue[category] = category_revenue.get(
                category, 0) + (item.total or 0)
            if category not in category_skus:
                category_skus[category] = set()
            category_skus[category].add(str(item.variant_id))

        # Top 5 categories by units sold
        top_categories = sorted(category_units.items(),
                                key=lambda x: x[1], reverse=True)[:5]

        for category, units in top_categories:
            skus = category_skus.get(category, [])
            sku_metrics = metrics.filter(sku__in=skus)
            forecast_acc = safe_average([
                m.forecast_accuracy for m in sku_metrics
                if m.forecast_accuracy is not None and -100 <= m.forecast_accuracy <= 100
            ])
            sell_through = safe_average([
                m.sell_through_rate for m in sku_metrics
                if m.sell_through_rate is not None and 0 <= m.sell_through_rate <= 100
            ])
            category_wise_performance.append({
                "category": category,
                "units_sold": units,
                "revenue": round(category_revenue.get(category, 0), 2),
                "forecast_accuracy": forecast_acc,
                "sell_through_rate": sell_through,
                "currency": currency
            })
            top_categories_data.append(
                {"category": category, "units_sold": units})

    # ---------------- Summary counts: slow movers, risk, reorder ---------------- #
    latest_forecasts = SKUForecastHistory.objects.filter(
        company=user, month=dashboard_month)
    slow_threshold = 3  # units sold in last 3 months
    start_slow_month = dashboard_month - relativedelta(months=3)
    sales_summary = SKUForecastHistory.objects.filter(
        company=user,
        month__gte=start_slow_month,
        month__lt=dashboard_month
    ).values("sku").annotate(total_sales=Sum("actual_sales_30"))
    sold_map = {s["sku"]: s["total_sales"] or 0 for s in sales_summary}

    slow_movers_count = sum(
        1 for f in latest_forecasts if sold_map.get(f.sku, 0) < slow_threshold)
    risk_alerts_count = sum(1 for f in latest_forecasts if (
        f.live_inventory or 0) <= (f.predicted_sales_30 or 0)/3)
    reorder_needed_count = sum(1 for f in latest_forecasts if (
        f.live_inventory or 0) <= (f.predicted_sales_30 or 0)/2)

    # ---------------- Final JSON ---------------- #
    dashboard_data = {
        "company": getattr(user, "company_name", str(user)),
        "currency": currency,
        "month": dashboard_month.strftime("%Y-%m"),
        "forecast_accuracy": safe_median([m.forecast_accuracy for m in metrics]),
        "forecast_bias": safe_median([m.forecast_bias for m in metrics]),
        "days_of_inventory": safe_average([m.days_of_inventory for m in metrics]),
        "sell_through_rate": safe_average([m.sell_through_rate for m in metrics]),
        "inventory_turnover": safe_average([m.inventory_turnover for m in metrics]),
        "sku_count_considered": {
            "forecast_accuracy": len([m for m in metrics if m.forecast_accuracy is not None]),
            "forecast_bias": len([m for m in metrics if m.forecast_bias is not None]),
            "days_of_inventory": len([m for m in metrics if m.days_of_inventory is not None]),
            "sell_through_rate": len([m for m in metrics if m.sell_through_rate is not None]),
            "inventory_turnover": len([m for m in metrics if m.inventory_turnover is not None]),
        },
        "top_selling_categories": top_categories_data,
        "category_wise_performance": category_wise_performance,
        "summary_counts": {
            "slow_movers_count": slow_movers_count,
            "risk_alerts_count": risk_alerts_count,
            "reorder_needed_count": reorder_needed_count
        }
    }

    return JsonResponse(dashboard_data, status=200, safe=False)
