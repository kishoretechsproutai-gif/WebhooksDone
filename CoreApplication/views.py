import base64
import hmac
import hashlib
import json
import time
import re
import logging
import requests
from decimal import Decimal
from datetime import datetime, timedelta
from django.conf import settings
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework_simplejwt.tokens import RefreshToken, AccessToken
from cryptography.fernet import Fernet
from celery import shared_task
from CoreApplication.models import (
    CompanyUser, Customer, Product, ProductVariant, Order, OrderLineItem
)
import secrets

logger = logging.getLogger(__name__)

# ---------------- Helper: Sanitize Text & Decimal ----------------
def sanitize_text(value, max_length=255):
    if value is None:
        return ""
    return str(value)[:max_length]

def sanitize_decimal(value, default="0.00"):
    try:
        return Decimal(value or default)
    except Exception:
        return Decimal(default)

# ---------------- Helper: Remove Emoji ----------------
def remove_emoji(text):
    if not text:
        return ""
    text = str(text)
    return re.sub(r'[\U00010000-\U0010FFFF]', '', text)

# ---------------- Helper: JWT ----------------
def get_user_from_token(request):
    logger.info("🔑 Extracting user from JWT token...")
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        logger.warning("⚠️ No token provided")
        return None, Response({"error": "No token provided"}, status=status.HTTP_401_UNAUTHORIZED)
    try:
        token = auth_header.split(" ")[1]
        access_token = AccessToken(token)
        user_id = access_token.payload.get("user_id")
        if not user_id:
            logger.error("❌ Invalid token: missing user_id")
            return None, Response({"error": "Invalid token"}, status=status.HTTP_401_UNAUTHORIZED)
        user = CompanyUser.objects.get(id=user_id)
        logger.info(f"✅ Authenticated user {user.email} (id={user.id})")
        return user, None
    except CompanyUser.DoesNotExist:
        logger.error("❌ User not found in DB")
        return None, Response({"error": "User not found"}, status=status.HTTP_404_NOT_FOUND)
    except Exception as e:
        logger.error(f"❌ Token error: {e}")
        return None, Response({"error": str(e)}, status=status.HTTP_401_UNAUTHORIZED)

# ---------------- Pagination Helper ----------------
def fetch_pages(url, headers):
    logger.info(f"🌐 Fetching Shopify pages from URL: {url}")
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for key, value in data.items():
            logger.info(f"📦 Got {len(value or [])} items from {key}")
            yield value or []
        link = resp.headers.get("Link")
        if link and 'rel="next"' in link:
            match = re.search(r'<([^>]+)>; rel="next"', link)
            url = match.group(1) if match else None
            logger.info(f"➡️ Next page: {url}")
        else:
            url = None
            logger.info("🏁 No more pages")
        time.sleep(0.5)

# ---------------- Date Range Helper ----------------
def generate_date_ranges(start_date_str="2015-01-01", window_days=4):
    start_date = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_date = datetime.utcnow()
    ranges = []
    batch_start = start_date
    while batch_start < end_date:
        batch_end = min(batch_start + timedelta(days=window_days), end_date)
        ranges.append((batch_start.isoformat(), batch_end.isoformat()))
        batch_start = batch_end
    return ranges

# ---------------- Webhook Helper ----------------
def create_webhook(shop_url, token, topic, callback_url, secret=None):
    """
    Creates Shopify webhook. Optionally sets webhook_secret in DB.
    """
    logger.info(f"🔔 Creating webhook for topic={topic} at {callback_url}")
    headers = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}
    data = {"webhook": {"topic": topic, "address": callback_url, "format": "json"}}
    resp = requests.post(
        f"https://{shop_url}/admin/api/2025-01/webhooks.json",
        headers=headers, data=json.dumps(data), timeout=10
    )
    if resp.status_code not in (200, 201):
        logger.error(f"❌ Webhook {topic} failed: {resp.text}")
    else:
        logger.info(f"✅ Webhook {topic} created")
    return resp.json()

# ---------------- Celery: Shopify Sync ----------------
@shared_task(bind=True, max_retries=3, soft_time_limit=604800)
def fetch_shopify_data_task(self, company_user_id):
    logger.info(f"🚀 Starting Shopify sync for company_user_id={company_user_id}")
    try:
        # ---------------- Decrypt user info ----------------
        user = CompanyUser.objects.get(id=company_user_id)
        fernet = Fernet(settings.ENCRYPTION_KEY)
        access_token = fernet.decrypt(user.shopify_access_token.encode()).decode()
        shopify_store_url = fernet.decrypt(user.shopify_store_url.encode()).decode()
        headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}

        # ---------------- Customers ----------------
        url = f"https://{shopify_store_url}/admin/api/2025-01/customers.json?limit=250"
        for page_no, page in enumerate(fetch_pages(url, headers), start=1):
            for c in page:
                addr = c.get("default_address") or {}
                Customer.objects.update_or_create(
                    shopify_id=c.get("id"),
                    defaults={
                        "company": user,
                        "email": sanitize_text(remove_emoji(c.get("email"))),
                        "first_name": sanitize_text(remove_emoji(c.get("first_name"))),
                        "last_name": sanitize_text(remove_emoji(c.get("last_name"))),
                        "phone": sanitize_text(remove_emoji(c.get("phone"))),
                        "created_at": c.get("created_at"),
                        "updated_at": c.get("updated_at"),
                        "city": sanitize_text(remove_emoji(addr.get("city"))),
                        "region": sanitize_text(remove_emoji(addr.get("province"))),
                        "country": sanitize_text(remove_emoji(addr.get("country"))),
                        "total_spent": sanitize_decimal(c.get("total_spent")),
                    },
                )

        # ---------------- Products & Variants ----------------
        url = f"https://{shopify_store_url}/admin/api/2025-01/products.json?limit=250"
        for page_no, page in enumerate(fetch_pages(url, headers), start=1):
            for p in page:
                product, _ = Product.objects.update_or_create(
                    shopify_id=p.get("id"),
                    defaults={
                        "company": user,
                        "title": sanitize_text(remove_emoji(p.get("title"))),
                        "vendor": sanitize_text(remove_emoji(p.get("vendor"))),
                        "product_type": sanitize_text(remove_emoji(p.get("product_type"))),
                        "tags": sanitize_text(remove_emoji(p.get("tags")), max_length=1000),
                        "status": sanitize_text(remove_emoji(p.get("status"))),
                        "created_at": p.get("created_at"),
                        "updated_at": p.get("updated_at"),
                    },
                )
                for v in p.get("variants") or []:
                    ProductVariant.objects.update_or_create(
                        shopify_id=v.get("id"),
                        defaults={
                            "company": user,
                            "product_id": product.shopify_id,
                            "title": sanitize_text(remove_emoji(v.get("title"))),
                            "sku": sanitize_text(remove_emoji(v.get("sku"))),
                            "price": sanitize_decimal(v.get("price")),
                            "compare_at_price": sanitize_decimal(v.get("compare_at_price")),
                            "cost": sanitize_decimal(v.get("cost")),
                            "inventory_quantity": v.get("inventory_quantity") or 0,
                            "created_at": v.get("created_at"),
                            "updated_at": v.get("updated_at"),
                        },
                    )

        # ---------------- Orders ----------------
        url = f"https://{shopify_store_url}/admin/api/2025-01/orders.json?limit=250&status=any"
        total_orders = 0
        for page_no, page in enumerate(fetch_pages(url, headers), start=1):
            for o in page:
                customer_id = o.get("customer", {}).get("id") if o.get("customer") else None
                order, _ = Order.objects.update_or_create(
                    shopify_id=o.get("id"),
                    defaults={
                        "company": user,
                        "customer_id": customer_id,
                        "order_number": sanitize_text(remove_emoji(o.get("order_number"))),
                        "order_date": o.get("created_at"),
                        "fulfillment_status": sanitize_text(remove_emoji(o.get("fulfillment_status"))),
                        "financial_status": sanitize_text(remove_emoji(o.get("financial_status"))),
                        "currency": sanitize_text(remove_emoji(o.get("currency"))),
                        "total_price": sanitize_decimal(o.get("total_price")),
                        "subtotal_price": sanitize_decimal(o.get("subtotal_price")),
                        "total_tax": sanitize_decimal(o.get("total_tax")),
                        "total_discount": sanitize_decimal(o.get("total_discounts")),
                        "created_at": o.get("created_at"),
                        "updated_at": o.get("updated_at"),
                    },
                )
                for li in o.get("line_items") or []:
                    quantity = li.get("quantity") or 0
                    price = sanitize_decimal(li.get("price"))
                    discount_allocated = sanitize_decimal(li.get("total_discount"))
                    total = price * quantity
                    OrderLineItem.objects.update_or_create(
                        shopify_line_item_id=li.get("id"),
                        defaults={
                            "company": user,
                            "order_id": order.shopify_id,
                            "product_id": li.get("product_id"),
                            "variant_id": li.get("variant_id"),
                            "quantity": quantity,
                            "price": price,
                            "discount_allocated": discount_allocated,
                            "total": total,
                        },
                    )
            total_orders += len(page)
            logger.info(f"✅ Synced {len(page)} orders (page {page_no})")

        logger.info(f"🎉 Total orders synced: {total_orders}")

        # ---------------- Webhooks ----------------
        if not user.webhook_secret:
            user.webhook_secret = secrets.token_hex(32)
            user.save()

        topics = [
            "customers/create", "customers/update",
            "products/create", "products/update", "products/delete",
            "orders/create", "orders/updated"
        ]
        for topic in topics:
            callback = f"{settings.WEBHOOK_BASE_URL}/webhooks/{company_user_id}/{topic.replace('/', '_')}/"
            create_webhook(shopify_store_url, access_token, topic, callback, secret=user.webhook_secret)

        logger.info(f"🎉 Shopify sync completed for company_user_id={company_user_id}")

    except Exception as e:
        logger.error(f"❌ Sync error for company_user_id={company_user_id}: {e}")
        self.retry(exc=e, countdown=60)

# ---------------- Webhook Security ----------------
def verify_hmac(request, company_user_id):
    try:
        user = CompanyUser.objects.get(id=company_user_id)
        secret = user.webhook_secret
        if not secret:
            logger.error(f"❌ No webhook secret found for company_user_id={company_user_id}")
            return False
        hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
        body = request.body
        digest = base64.b64encode(
            hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ).decode()
        return hmac.compare_digest(digest, hmac_header)
    except CompanyUser.DoesNotExist:
        logger.error(f"❌ CompanyUser {company_user_id} not found for HMAC verification")
        return False
    except Exception as e:
        logger.error(f"❌ HMAC verification error for company_user_id={company_user_id}: {e}")
        return False

# ---------------- Webhook Task ----------------
@shared_task
def process_webhook_task(company_user_id, topic, payload):
    try:
        if topic in ["customers_create", "customers_update"]:
            Customer.objects.update_or_create(
                shopify_id=payload.get("id"),
                defaults={
                    "company_id": company_user_id,
                    "first_name": sanitize_text(remove_emoji(payload.get("first_name"))),
                    "last_name": sanitize_text(remove_emoji(payload.get("last_name"))),
                    "email": sanitize_text(remove_emoji(payload.get("email"))),
                }
            )
        elif topic in ["products_create", "products_update"]:
            Product.objects.update_or_create(
                shopify_id=payload.get("id"),
                defaults={
                    "company_id": company_user_id,
                    "title": sanitize_text(remove_emoji(payload.get("title"))),
                    "vendor": sanitize_text(remove_emoji(payload.get("vendor"))),
                    "product_type": sanitize_text(remove_emoji(payload.get("product_type"))),
                    "tags": sanitize_text(remove_emoji(payload.get("tags")), max_length=1000),
                    "status": sanitize_text(remove_emoji(payload.get("status"))),
                }
            )
        elif topic == "products_delete":
            Product.objects.filter(shopify_id=payload.get("id")).delete()
        elif topic in ["orders_create", "orders_updated"]:
            Order.objects.update_or_create(
                shopify_id=payload.get("id"),
                defaults={
                    "company_id": company_user_id,
                    "order_number": sanitize_text(remove_emoji(payload.get("order_number"))),
                    "fulfillment_status": sanitize_text(remove_emoji(payload.get("fulfillment_status"))),
                    "financial_status": sanitize_text(remove_emoji(payload.get("financial_status"))),
                    "currency": sanitize_text(remove_emoji(payload.get("currency"))),
                }
            )
    except Exception as e:
        logger.error(f"❌ Webhook task error ({topic}) for company_user_id={company_user_id}: {e}")

# ---------------- Webhook View ----------------
@csrf_exempt
def shopify_webhook_view(request, company_user_id, topic):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid method"}, status=405)
    
    # ---------------- TEMP: Disable HMAC verification for local testing ----------------
    # if not verify_hmac(request, company_user_id):
    #     return JsonResponse({"error": "Invalid HMAC"}, status=401)
    
    try:
        payload = json.loads(request.body.decode())
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    process_webhook_task.delay(company_user_id, topic, payload)
    return HttpResponse(status=200)


# ---------------- Register & Login ----------------
class RegisterView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        company = sanitize_text(remove_emoji(request.data.get("company")))
        email = sanitize_text(remove_emoji(request.data.get("email")))
        password = request.data.get("password")
        if not company or not email or not password:
            return Response({"error": "All fields are required"}, status=status.HTTP_400_BAD_REQUEST)
        if CompanyUser.objects.filter(email=email).exists():
            return Response({"error": "Email already exists"}, status=status.HTTP_400_BAD_REQUEST)
        user = CompanyUser(company=company, email=email)
        user.set_password(password)
        user.save()
        return Response({"message": "User registered successfully"}, status=status.HTTP_201_CREATED)

class LoginView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        email = sanitize_text(remove_emoji(request.data.get("email")))
        password = request.data.get("password")
        try:
            user = CompanyUser.objects.get(email=email)
        except CompanyUser.DoesNotExist:
            return Response({"error": "Invalid credentials"}, status=status.HTTP_400_BAD_REQUEST)
        if not user.check_password(password):
            return Response({"error": "Invalid credentials"}, status=status.HTTP_400_BAD_REQUEST)
        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)
        return Response({"access_token": access_token, "expires_in_days": 15}, status=status.HTTP_200_OK)

# ---------------- Save & Get Shopify Credentials ----------------
class SaveShopifyCredentialsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    def post(self, request):
        user, error_response = get_user_from_token(request)
        if error_response: return error_response
        access_token = sanitize_text(remove_emoji(request.data.get("access_token")))
        store_url = sanitize_text(remove_emoji(request.data.get("store_url")))
        if not access_token or not store_url:
            return Response({"error": "Access token and store URL are required"}, status=status.HTTP_400_BAD_REQUEST)
        fernet = Fernet(settings.ENCRYPTION_KEY)
        user.shopify_access_token = fernet.encrypt(access_token.encode()).decode()
        user.shopify_store_url = fernet.encrypt(store_url.encode()).decode()
        if not user.webhook_secret:
            user.webhook_secret = secrets.token_hex(32)
        user.save()
        fetch_shopify_data_task.apply_async(args=[user.id], countdown=5)
        return Response({"message": "Shopify credentials saved, sync started", "user_id": user.id}, status=status.HTTP_200_OK)

class GetShopifyCredentialsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    def get(self, request):
        user, error_response = get_user_from_token(request)
        if error_response: return error_response
        if not user.shopify_access_token or not user.shopify_store_url:
            return Response({"error": "No Shopify credentials found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            fernet = Fernet(settings.ENCRYPTION_KEY)
            decrypted_token = fernet.decrypt(user.shopify_access_token.encode()).decode()
            decrypted_url = fernet.decrypt(user.shopify_store_url.encode()).decode()
            return Response({"shopify_access_token": decrypted_token, "shopify_store_url": decrypted_url}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Failed to decrypt credentials: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
