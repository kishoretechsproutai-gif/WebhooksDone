from datetime import datetime, date
import json
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from dateutil.relativedelta import relativedelta
from django.db.models import Sum, Max
import statistics
from cryptography.fernet import Fernet
from django.conf import settings

from CoreApplication.models import (
    CompanyUser,
    Product,
    ProductVariant,
    PurchaseOrder,
    Order,
    OrderLineItem,
)
from Testingproject.models import (
    SKUForecastHistory,
    InventoryValuation,
    SKUForecastMetrics,
)
from CoreApplication.views import get_user_from_token


# ---------------- Helper: SKU Normalizer ---------------- #
def normalize_sku(sku):
    if not sku:
        return ""
    return str(sku).strip().upper().replace(" ", "").replace("-", "").replace("_", "")


# ---------------- Helper: Safe date parser ---------------- #
def safe_parse_date(date_val):
    """
    Parse string, datetime, or date object to a date object safely.
    Returns None if parsing fails.
    """
    print(f"[DEBUG] safe_parse_date called with: {repr(date_val)} ({type(date_val)})")

    if date_val is None:
        return None

    # Already a date object (but not datetime)
    if isinstance(date_val, date) and not isinstance(date_val, datetime):
        return date_val

    # If it's a datetime object
    if isinstance(date_val, datetime):
        return date_val.date()

    # Try parsing as string
    str_val = str(date_val).strip()
    for fmt in [
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d-%m-%Y",
        "%m/%d/%Y",
        "%B %d, %Y",
        "%d/%m/%Y",
        "%Y-%m-%dT%H:%M:%S"
    ]:
        try:
            return datetime.strptime(str_val, fmt).date()
        except:
            continue

    print(f"⚠️ Could not parse date: '{date_val}'")
    return None



# ---------------- Main Function ---------------- #


from django.views.decorators.csrf import csrf_exempt
from django.http import JsonResponse
from datetime import date
from dateutil.relativedelta import relativedelta
from django.db.models import Sum, Max


@csrf_exempt
def inventory_reorder_report(request):
    user, error = get_user_from_token(request)
    if error:
        return error

    company = user
    today = date.today()

    # ---------------- Latest Forecasts ---------------- #
    latest_month = SKUForecastHistory.objects.filter(company=company).aggregate(
        latest_month=Max("month")
    ).get("latest_month")
    if not latest_month:
        return JsonResponse({"error": "No forecasts found for this company."}, status=404)

    latest_forecasts = SKUForecastHistory.objects.filter(company=company, month=latest_month)
    forecast_skus = [normalize_sku(f.sku) for f in latest_forecasts]

    # ---------------- On-Order Calculation ---------------- #
    purchase_orders = PurchaseOrder.objects.filter(
        company_id=company.id, sku_id__in=[f.sku for f in latest_forecasts]
    )

    on_orders = {}
    order_details = {}

    for po in purchase_orders:
        delivery_date = safe_parse_date(po.delivery_date)
        if not delivery_date or delivery_date < today:
            continue

        try:
            qty = int(float(str(po.quantity_ordered).strip()))
        except Exception:
            qty = 0

        sku_norm = normalize_sku(po.sku_id)
        on_orders[sku_norm] = on_orders.get(sku_norm, 0) + qty
        order_details.setdefault(sku_norm, []).append({
            "purchase_order_id": po.purchase_order_id,
            "supplier_name": po.supplier_name,
            "order_date": str(po.order_date),
            "delivery_date": str(po.delivery_date),
            "quantity_ordered": qty,
        })

    # ---------------- Forecast Data ---------------- #
    forecast_data = []
    for f in latest_forecasts:
        sku_norm = normalize_sku(f.sku)
        forecast_30 = f.predicted_sales_30 or 0
        forecast_60 = f.predicted_sales_60 or 0
        forecast_90 = f.predicted_sales_90 or 0
        live_inventory = max(f.live_inventory or 0, 0)  # NEGATIVE STOCK EDGE CASE
        on_order = on_orders.get(sku_norm, 0)
        reason = f.reason or ""

        total_available = live_inventory + on_order

        # ---------------- Action Item Logic ---------------- #
        if forecast_30 == 0:
            action_item = "Sufficient Stock" if total_available > 0 else "StockOut Risk"
        elif total_available < forecast_30:
            action_item = "StockOut Risk"
        else:
            action_item = "Sufficient Stock"

        reorder_qty = max(forecast_30 + forecast_60 - total_available, 0)

        try:
            variant = ProductVariant.objects.get(company=company, sku=f.sku)
            price = float(variant.price) if variant.price else 0.0
            product_id = variant.product_id
            variant_title = variant.title or ""
            product = Product.objects.filter(company=company, shopify_id=product_id).first()
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
    current_month_start = today.replace(day=1)
    start_month_3 = current_month_start - relativedelta(months=3)
    start_month_12 = current_month_start - relativedelta(months=12)

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
        month_str = s["month"].strftime("%b %Y")
        sales_value = s["month_sales"] or 0
        if sku not in monthwise_sales_map:
            monthwise_sales_map[sku] = {}
        monthwise_sales_map[sku][month_str] = sales_value

    slow_threshold = 3
    slow_movers = []
    for sku, total_sold in sold_map_3.items():
        if total_sold < slow_threshold:
            try:
                variant = ProductVariant.objects.get(company=company, sku=sku)
                price = float(variant.price) if variant.price else 0.0
                live_inventory = max(variant.inventory_quantity or 0, 0)  # NEGATIVE STOCK EDGE CASE
                product_id = variant.product_id
                variant_title = variant.title or ""
                product = Product.objects.filter(company=company, shopify_id=product_id).first()
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

    # ---------------- Summary ---------------- #
    slow_movers_count = len(slow_movers)
    sufficient_stock_count = sum(1 for f in forecast_data if f["Action_Item"] == "Sufficient Stock")
    reorder_needed_count = sum(1 for f in forecast_data if f["Action_Item"] == "StockOut Risk")

    latest_inventory = InventoryValuation.objects.filter(company=company).order_by('-month').first()
    if latest_inventory:
        inventory_info = {
            "month": latest_inventory.month.strftime("%Y-%m"),
            "inventory_value": latest_inventory.inventory_value,
            "currency": latest_inventory.currency,
        }
    else:
        inventory_info = None

    return JsonResponse({
        "summary": {
            "slow_movers_count": slow_movers_count,
            "sufficient_stock_count": sufficient_stock_count,
            "stockout_risk_count": reorder_needed_count,
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


# def safe_parse_date(date_str):
#     """Helper to safely parse dates in multiple formats."""
#     if not date_str:
#         return None
#     for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
#         try:
#             return datetime.strptime(date_str, fmt).date()
#         except Exception:
#             continue
#     return None


from datetime import date
from django.db.models import Max
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from CoreApplication.models import Product, ProductVariant, PurchaseOrder
from Testingproject.models import SKUForecastHistory
from CoreApplication.views import get_user_from_token

def safe_parse_date(value):
    from datetime import datetime
    if not value:
        return None
    try:
        if isinstance(value, str):
            return datetime.strptime(value, "%Y-%m-%d").date()
        return value
    except Exception:
        return None

from datetime import date
from django.db.models import Max
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from CoreApplication.models import Product, ProductVariant, PurchaseOrder
from Testingproject.models import SKUForecastHistory
from CoreApplication.views import get_user_from_token

def safe_parse_date(value):
    from datetime import datetime
    if not value:
        return None
    try:
        if isinstance(value, str):
            return datetime.strptime(value, "%Y-%m-%d").date()
        return value
    except Exception:
        return None

@csrf_exempt
def get_risk_alerts(request):
    """
    API to fetch StockOut Risk Alerts for a company.
    Shows SKUs where (live_inventory + on_order) < forecast_30.
    - Auth via JWT token
    - Includes SKU, Product info, live inventory, on-order, forecast_30, forecast_60
    - Adds 'reason' and purchase order details
    """
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

    latest_forecasts = SKUForecastHistory.objects.filter(company=company, month=latest_month)

    # ---------------- Build Risk Alerts ---------------- #
    risk_alerts = []

    for f in latest_forecasts:
        sku = f.sku
        forecast_30 = f.predicted_sales_30 or 0
        forecast_60 = f.predicted_sales_60 or 0
        live_inventory = f.live_inventory or 0
        reason = f.reason or ""

        # ---------------- On-Order Quantities ---------------- #
        on_order = 0
        purchase_order_details = []

        purchase_orders = PurchaseOrder.objects.filter(company=company, sku_id=sku)
        for po in purchase_orders:
            delivery_date = safe_parse_date(po.delivery_date)
            if delivery_date and delivery_date >= today:
                try:
                    qty = int(float(str(po.quantity_ordered).strip()))
                except Exception:
                    qty = 0
                on_order += qty
                purchase_order_details.append({
                    "purchase_order_id": po.purchase_order_id,
                    "supplier_name": po.supplier_name,
                    "order_date": str(po.order_date),
                    "delivery_date": str(po.delivery_date),
                    "quantity_ordered": qty,
                })

        total_available = live_inventory + on_order

        # ---------------- Stockout Risk Condition ---------------- #
        if total_available >= forecast_30:
            continue  # skip if stock is enough

        action_item = "StockOut Risk"

        try:
            variant = ProductVariant.objects.get(company=company, sku=sku)
            price = float(variant.price) if variant.price else 0.0
            variant_title = variant.title or ""
            product = Product.objects.filter(
                company=company, shopify_id=variant.product_id
            ).first()
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
            "Action_Item": action_item,
            "Reason": reason,
            "PurchaseOrders": purchase_order_details
        })

    stockout_count = len(risk_alerts)

    return JsonResponse({
        "company_id": company.id,
        "stockout_count": stockout_count,
        "risk_alerts": risk_alerts
    }, safe=False)





# def safe_parse_date(date_str):
#     """Helper to safely parse dates in multiple formats."""
#     if not date_str:
#         return None
#     for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
#         try:
#             return datetime.strptime(date_str, fmt).date()
#         except Exception:
#             continue
#     return None


@csrf_exempt
def get_need_reordering(request):
    """
    API to fetch SKUs with Sufficient Stock for a company.
    - Auth via JWT token
    - Includes SKU, Product info, live inventory, on-order, forecast_30, forecast_60
    - Adds 'Reason' and purchase order details
    """
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

    latest_forecasts = SKUForecastHistory.objects.filter(company=company, month=latest_month)

    # ---------------- Build Sufficient Stock List ---------------- #
    sufficient_stock = []

    all_purchase_orders = PurchaseOrder.objects.filter(company=company)

    for f in latest_forecasts:
        sku = f.sku
        sku_norm = normalize_sku(sku)
        forecast_30 = f.predicted_sales_30 or 0
        forecast_60 = f.predicted_sales_60 or 0
        live_inventory = f.live_inventory or 0
        reason = f.reason or ""

        # ---------------- On-Order Quantities (with normalized SKU) ---------------- #
        on_order = 0
        purchase_order_details = []

        for po in all_purchase_orders:
            if normalize_sku(po.sku_id) != sku_norm:
                continue
            delivery_date = safe_parse_date(po.delivery_date)
            if delivery_date and delivery_date >= today:
                try:
                    qty = int(float(str(po.quantity_ordered).strip()))
                except Exception:
                    qty = 0
                on_order += qty
                purchase_order_details.append({
                    "purchase_order_id": po.purchase_order_id,
                    "supplier_name": po.supplier_name,
                    "order_date": str(po.order_date),
                    "delivery_date": str(po.delivery_date),
                    "quantity_ordered": qty,
                })

        total_available = live_inventory + on_order

        # ---------------- Include Sufficient Stock (Updated Logic) ---------------- #
        if forecast_30 == 0:
            if total_available > 0:
                action_item = "Sufficient Stock"
            else:
                continue  # skip SKU with 0 forecast and 0 stock
        elif total_available >= forecast_30:  # include equality
            action_item = "Sufficient Stock"
        else:
            continue  # skip anything below forecast_30

        # ---------------- Product / Variant Info ---------------- #
        try:
            variant = ProductVariant.objects.get(company=company, sku=sku)
            price = float(variant.price) if variant.price else 0.0
            variant_title = variant.title or ""
            product = Product.objects.filter(company=company, shopify_id=variant.product_id).first()
            category = product.product_type if product else None
            product_title = product.title if product else ""
        except ProductVariant.DoesNotExist:
            price = 0.0
            variant_title = ""
            category = None
            product_title = ""

        sufficient_stock.append({
            "SKU": sku,
            "Product": product_title,
            "Variant": variant_title,
            "Category": category,
            "Price": price,
            "Live_Inventory": live_inventory,
            "OnOrder": on_order,
            "Forecast_30": forecast_30,
            "Forecast_60": forecast_60,
            "Action_Item": action_item,
            "Reason": reason,
            "PurchaseOrders": purchase_order_details
        })

    sufficient_count = len(sufficient_stock)

    return JsonResponse({
        "company_id": company.id,
        "sufficient_count": sufficient_count,
        "need_reordering": sufficient_stock
    }, safe=False)






import statistics
from collections import OrderedDict
from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from cryptography.fernet import Fernet
from django.conf import settings
from django.db.models import Sum, Max
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

from CoreApplication.models import (
    CompanyUser, Order, OrderLineItem, Product, ProductVariant, PurchaseOrder
)
from Testingproject.models import SKUForecastMetrics, SKUForecastHistory
from CoreApplication.views import get_user_from_token


# def safe_parse_date(value):
#     from datetime import datetime
#     if not value:
#         return None
#     try:
#         if isinstance(value, str):
#             return datetime.strptime(value, "%Y-%m-%d").date()
#         return value
#     except Exception:
#         return None

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from datetime import datetime, date
from collections import OrderedDict
from dateutil.relativedelta import relativedelta
from cryptography.fernet import Fernet
import statistics


from CoreApplication.views import get_user_from_token
from django.conf import settings
from django.db.models import Sum



@csrf_exempt
def CompanyDashboardMetricsView(request):
    # ---------------- Auth ---------------- #
    user, error = get_user_from_token(request)
    if error:
        return error

    # ---------------- Determine dashboard month ---------------- #
    month_str = request.GET.get("month")  # Expect format "YYYY-MM"
    if month_str:
        try:
            dashboard_month = datetime.strptime(month_str, "%Y-%m").date().replace(day=1)
        except ValueError:
            return JsonResponse({"error": "Invalid month format. Use YYYY-MM."}, status=400)
    else:
        latest_metric_entry = SKUForecastMetrics.objects.filter(company=user).order_by('-month').first()
        if not latest_metric_entry:
            return JsonResponse({
                "message": "No SKU metrics found for this company.",
                "company": getattr(user, "company_name", str(user))
            }, status=404)
        dashboard_month = latest_metric_entry.month

    # ---------------- Fetch metrics ---------------- #
    metrics = SKUForecastMetrics.objects.filter(company=user, month=dashboard_month)
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

    # ---------------- Latest Forecasts & On-Order ---------------- #
    latest_forecasts = SKUForecastHistory.objects.filter(company=user, month=dashboard_month)
    forecast_skus = [normalize_sku(f.sku) for f in latest_forecasts]
    purchase_orders = PurchaseOrder.objects.filter(
        company_id=user.id, sku_id__in=[f.sku for f in latest_forecasts]
    )
    today = date.today()

    # On-Order Calculation
    on_orders = {}
    for po in purchase_orders:
        delivery_date = safe_parse_date(po.delivery_date)
        if not delivery_date or delivery_date < today:
            continue
        try:
            qty = int(float(str(po.quantity_ordered).strip()))
        except Exception:
            qty = 0
        sku_norm = normalize_sku(po.sku_id)
        on_orders[sku_norm] = on_orders.get(sku_norm, 0) + qty

    # Forecast Data and Action Item Logic
    forecast_data_for_counts = []
    for f in latest_forecasts:
        sku_norm = normalize_sku(f.sku)
        forecast_30 = f.predicted_sales_30 or 0
        live_inventory = max(f.live_inventory or 0, 0)
        on_order = on_orders.get(sku_norm, 0)
        total_available = live_inventory + on_order

        if forecast_30 == 0:
            action_item = "Sufficient Stock" if total_available > 0 else "StockOut Risk"
        elif total_available < forecast_30:
            action_item = "StockOut Risk"
        else:
            action_item = "Sufficient Stock"

        forecast_data_for_counts.append({"Action_Item": action_item})

    sufficient_stock_count = sum(1 for f in forecast_data_for_counts if f["Action_Item"] == "Sufficient Stock")
    stockout_risk_count = sum(1 for f in forecast_data_for_counts if f["Action_Item"] == "StockOut Risk")

    # ---------------- Forecast Data with correct Action_Item ---------------- #
    forecast_data = []
    for f in latest_forecasts:
        sku_norm = normalize_sku(f.sku)
        forecast_30 = f.predicted_sales_30 or 0
        forecast_60 = f.predicted_sales_60 or 0
        forecast_90 = f.predicted_sales_90 or 0
        live_inventory = max(f.live_inventory or 0, 0)
        on_order = on_orders.get(sku_norm, 0)
        reason = f.reason or ""

        total_available = live_inventory + on_order

        if forecast_30 == 0:
            action_item = "Sufficient Stock" if total_available > 0 else "StockOut Risk"
        elif total_available < forecast_30:
            action_item = "StockOut Risk"
        else:
            action_item = "Sufficient Stock"

        reorder_qty = max(forecast_30 + forecast_60 - total_available, 0)

        try:
            variant = ProductVariant.objects.get(company=user, sku=f.sku)
            price = float(variant.price) if variant.price else 0.0
            product_id = variant.product_id
            variant_title = variant.title or ""
            product = Product.objects.filter(company=user, shopify_id=product_id).first()
            category = product.product_type if product else None
            product_title = product.title if product else ""
        except ProductVariant.DoesNotExist:
            price = 0.0
            variant_title = ""
            category = None
            product_title = ""

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
        })

    # ---------------- Slow Movers ---------------- #
    start_slow_month = dashboard_month - relativedelta(months=3)
    sales_summary_3 = SKUForecastHistory.objects.filter(
        company=user,
        month__gte=start_slow_month,
        month__lt=dashboard_month
    ).values("sku").annotate(total_sales=Sum("actual_sales_30"))
    slow_movers_count = sum(1 for s in sales_summary_3 if (s["total_sales"] or 0) < 3)

    # ---------------- Last 12 months sales (Actual + Predicted) ---------------- #
    reference_month = dashboard_month
    start_12_months = (reference_month - relativedelta(months=12)).replace(day=1)

    # Initialize data structure
    last_12_months_data = OrderedDict()
    for i in range(12, 0, -1):
        month_start = (reference_month - relativedelta(months=i)).replace(day=1)
        last_12_months_data[month_start.strftime("%b %Y")] = {
            "actual_sales_units": 0,
            "actual_sales_amount": 0.0,
            "predicted_sales_units": 0,
            "predicted_sales_amount": 0.0
        }

    # Actual Sales (from Order + OrderLineItem)
    orders_last_12_months = Order.objects.filter(
        company=user,
        order_date__gte=start_12_months,
        order_date__lt=reference_month + relativedelta(months=1)
    ).values("shopify_id", "order_date")
    order_id_to_month = {o["shopify_id"]: o["order_date"].replace(day=1) for o in orders_last_12_months}
    order_ids = list(order_id_to_month.keys())
    line_items_last_12_months = OrderLineItem.objects.filter(
        company=user, order_id__in=order_ids
    ).values("order_id", "quantity", "total")

    for item in line_items_last_12_months:
        order_month = order_id_to_month.get(item["order_id"])
        if order_month:
            key = order_month.strftime("%b %Y")
            if key in last_12_months_data:
                last_12_months_data[key]["actual_sales_units"] += item["quantity"] or 0
                last_12_months_data[key]["actual_sales_amount"] += float(item["total"] or 0.0)

    # Predicted Sales (from SKUForecastHistory + ProductVariant.price)
    forecast_history_12 = SKUForecastHistory.objects.filter(
        company=user, month__gte=start_12_months, month__lt=reference_month + relativedelta(months=1)
    )

    variant_prices = {
        normalize_sku(v.sku): float(v.price) if v.price else 0.0
        for v in ProductVariant.objects.filter(company=user)
    }

    for record in forecast_history_12:
        key = record.month.strftime("%b %Y")
        sku_norm = normalize_sku(record.sku)
        price = variant_prices.get(sku_norm, 0.0)
        if key in last_12_months_data:
            predicted_units = record.predicted_sales_30 or 0
            last_12_months_data[key]["predicted_sales_units"] += predicted_units
            last_12_months_data[key]["predicted_sales_amount"] += predicted_units * price

    # ---------------- Final JSON ---------------- #
    dashboard_data = {
        "company": getattr(user, "company_name", str(user)),
        "month": dashboard_month.strftime("%Y-%m"),
        "forecast_accuracy": safe_median(acc_values),
        "forecast_bias": safe_median(bias_values),
        "days_of_inventory": safe_average(doi_values),
        "sell_through_rate": safe_average(str_values),
        "inventory_turnover": safe_average(it_values),
        "sku_count_considered": {
            "forecast_accuracy": len(acc_values),
            "forecast_bias": len(bias_values),
            "days_of_inventory": len(doi_values),
            "sell_through_rate": len(str_values),
            "inventory_turnover": len(it_values),
        },
        "summary_counts": {
            "slow_movers_count": slow_movers_count,
            "sufficient_stock_count": sufficient_stock_count,
            "stockout_risk_count": stockout_risk_count,
        },
        "forecasts": forecast_data,
        "last_12_months": last_12_months_data,
    }

    return JsonResponse(dashboard_data, status=200, safe=False)


# Collections API 


from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.db.models import Count
from CoreApplication.models import Collection, CollectionItem
from CoreApplication.views import get_user_from_token


@csrf_exempt
def get_collections_list(request):
    """
    Returns all collections for a shop owner with basic details only.
    Includes the number of items in each collection.
    """
    user, error = get_user_from_token(request)
    if error:
        return error

    company = user

    # Annotate each collection with total_items
    collections = (
        Collection.objects.filter(company_id=company.id)
        .annotate(total_items=Count('items'))
        .order_by('-updated_at')
    )
    total_collections = collections.count()

    collection_list = []
    for c in collections:
        collection_list.append({
            "collection_id": c.id,
            "shopify_id": c.shopify_id,
            "title": c.title,
            "handle": c.handle,
            "image_src": c.image_src,
            "updated_at": c.updated_at,
            "created_at": c.created_at,
            "total_items": c.total_items,  # Added number of items
        })

    return JsonResponse({
        "company_id": company.id,
        "total_collections": total_collections,
        "collections": collection_list
    }, safe=False, status=200)



from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from CoreApplication.models import CollectionItem, Product, ProductVariant, Collection
from CoreApplication.views import get_user_from_token


@csrf_exempt
def get_collection_details(request, collection_id):
    """
    Returns all items for a single collection.
    Includes product details, variant info, tags, and images.
    """
    user, error = get_user_from_token(request)
    if error:
        return error

    company = user

    # Validate collection belongs to this company
    try:
        collection = Collection.objects.get(id=collection_id, company_id=company.id)
    except Collection.DoesNotExist:
        return JsonResponse({"error": "Collection not found"}, status=404)

    # Fetch items
    items = CollectionItem.objects.filter(collection=collection)
    product_ids = [item.product_id for item in items]

    # Preload products and variants
    products = {p.shopify_id: p for p in Product.objects.filter(company=company, shopify_id__in=product_ids)}
    variants = {v.product_id: v for v in ProductVariant.objects.filter(company=company, product_id__in=product_ids)}

    item_list = []
    for item in items:
        product = products.get(item.product_id)
        variant = variants.get(item.product_id)

        item_list.append({
            "collection_item_id": item.id,
            "product_shopify_id": item.product_id,
            "product_title": product.title if product else None,
            "vendor": product.vendor if product else None,
            "product_type": product.product_type if product else None,
            "sku": variant.sku if variant else None,
            "price": float(variant.price) if variant and variant.price else 0.0,
            "inventory_quantity": variant.inventory_quantity if variant else 0,
            "product_tags": product.tags if product and getattr(product, "tags", None) else None,
            "product_image_src": getattr(product, "image_src", None),
            "collection_item_image_src": item.image_src,
            "created_at": item.created_at,
        })

    response = {
        "collection_id": collection.id,
        "shopify_id": collection.shopify_id,
        "title": collection.title,
        "handle": collection.handle,
        "image_src": collection.image_src,
        "updated_at": collection.updated_at,
        "created_at": collection.created_at,
        "total_items": len(item_list),
        "items": item_list,
    }

    return JsonResponse(response, safe=False, status=200)


import pytz
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from CoreApplication.models import CompanyUser, Product, ProductVariant, Order, OrderLineItem

@csrf_exempt
def masterdatahub(request):
    try:
        # --- Get company ---
        company_id = request.GET.get("company_id", 2)
        company = CompanyUser.objects.get(id=company_id)
        print(f"DEBUG: company_id={company_id}")

        # --- Month parameter ---
        month_param = request.GET.get("month")
        if month_param:
            month_dt = datetime.strptime(month_param, "%Y-%m")
            print(f"DEBUG: Received month parameter: {month_param}")
        else:
            month_dt = datetime.now()
            print(f"DEBUG: No month parameter, using current month: {month_dt.strftime('%Y-%m')}")

        tz = pytz.UTC
        start_date = tz.localize(datetime(month_dt.year, month_dt.month, 1, 0, 0, 0))
        next_month = start_date + relativedelta(months=1)
        end_date = next_month - timedelta(seconds=1)
        print(f"DEBUG: Filtering orders from {start_date} to {end_date}")

        # --- Product stats ---
        total_skus = ProductVariant.objects.filter(company=company).count()
        active_products = Product.objects.filter(company=company, status="active").count()
        draft_products = Product.objects.filter(company=company, status="draft").count()
        total_categories = Product.objects.filter(company=company).values("product_type").distinct().count()
        print(f"DEBUG: total_skus={total_skus}, active_products={active_products}, draft_products={draft_products}, total_categories={total_categories}")

        # --- Orders in month ---
        orders = Order.objects.filter(
            company=company,
            order_date__gte=start_date,
            order_date__lte=end_date
        )
        order_ids = list(orders.values_list("shopify_id", flat=True))
        print(f"DEBUG: Filtered orders: {len(order_ids)}")

        # --- OrderLineItems for filtered orders ---
        line_items = OrderLineItem.objects.filter(order_id__in=order_ids)
        print(f"DEBUG: Line items count: {line_items.count()}")

        # --- Aggregate sales ---
        total_sales_units = 0
        total_sales_price = 0.0
        category_units = {}
        category_price = {}

        # Build a mapping of product_id to product_type
        product_types = {p.shopify_id: p.product_type or "Unknown" for p in Product.objects.filter(company=company)}

        for li in line_items:
            category_name = product_types.get(li.product_id, "Unknown")
            total_sales_units += li.quantity or 0
            total_sales_price += float(li.price or 0) * (li.quantity or 0)

            category_units[category_name] = category_units.get(category_name, 0) + (li.quantity or 0)
            category_price[category_name] = category_price.get(category_name, 0.0) + float(li.price or 0) * (li.quantity or 0)

        print(f"DEBUG: Total sales_units: {total_sales_units}, Total sales_price: {total_sales_price}")

        response_data = {
            "company_id": company.id,
            "total_skus": total_skus,
            "total_categories": total_categories,
            "active_products": active_products,
            "draft_products": draft_products,
            "sales_units_current_month": total_sales_units,
            "sales_price_current_month": total_sales_price,
            "category_wise_sales_units": category_units,
            "category_wise_sales_price": category_price
        }

        return JsonResponse(response_data)

    except Exception as e:
        print(f"ERROR in masterdatahub: {str(e)}")
        return JsonResponse({"error": str(e)})
