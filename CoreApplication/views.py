from rest_framework_simplejwt.tokens import RefreshToken
from .models import PurchaseOrder, CompanyUser
from django.utils.dateparse import parse_date
from CoreApplication.models import Order
from django.shortcuts import get_object_or_404
from CoreApplication.models import CompanyUser
from .models import CompanyUser, Collection, CollectionItem
from .models import PromotionalData
from django.utils.decorators import method_decorator
from django.http import JsonResponse
from django.views import View
from datetime import date
import pandas as pd
from celery import chain
from sentence_transformers import SentenceTransformer
import chromadb
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
    CompanyUser, Customer, Product, ProductVariant, Order, OrderLineItem, Prompt
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
    logger.info(f"🔔 Creating webhook for topic={topic} at {callback_url}")
    headers = {"X-Shopify-Access-Token": token,
               "Content-Type": "application/json"}
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
    logger.info(
        f"🚀 Starting Shopify sync for company_user_id={company_user_id}")
    try:
        user = CompanyUser.objects.get(id=company_user_id)
        fernet = Fernet(settings.ENCRYPTION_KEY)
        access_token = fernet.decrypt(
            user.shopify_access_token.encode()).decode()
        shopify_store_url = fernet.decrypt(
            user.shopify_store_url.encode()).decode()
        headers = {"X-Shopify-Access-Token": access_token,
                   "Content-Type": "application/json"}

        # Customers
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
                        # <-- region added
                        "region": sanitize_text(remove_emoji(addr.get("province"))),
                        "country": sanitize_text(remove_emoji(addr.get("country"))),
                        "total_spent": sanitize_decimal(c.get("total_spent")),
                    },
                )

        # Products & Variants
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

        # Orders
        url = f"https://{shopify_store_url}/admin/api/2025-01/orders.json?limit=250&status=any"
        total_orders = 0
        for page_no, page in enumerate(fetch_pages(url, headers), start=1):
            for o in page:
                customer_id = o.get("customer", {}).get(
                    "id") if o.get("customer") else None
                addr = o.get("billing_address") or {}
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
                        # <-- region added here
                        "region": sanitize_text(remove_emoji(addr.get("country"))),
                    },
                )
                for li in o.get("line_items") or []:
                    quantity = li.get("quantity") or 0
                    price = sanitize_decimal(li.get("price"))
                    discount_allocated = sanitize_decimal(
                        li.get("total_discount"))
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
            create_webhook(shopify_store_url, access_token, topic,
                           callback, secret=user.webhook_secret)

        logger.info(
            f"🎉 Shopify sync completed for company_user_id={company_user_id}")

    except Exception as e:
        logger.error(
            f"❌ Sync error for company_user_id={company_user_id}: {e}")
        self.retry(exc=e, countdown=60)

# ---------------- Webhook Security ----------------


def verify_hmac(request, company_user_id):
    try:
        user = CompanyUser.objects.get(id=company_user_id)
        secret = user.webhook_secret
        if not secret:
            logger.error(
                f"❌ No webhook secret found for company_user_id={company_user_id}")
            return False
        hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
        body = request.body
        digest = base64.b64encode(
            hmac.new(secret.encode(), body, hashlib.sha256).digest()
        ).decode()
        return hmac.compare_digest(digest, hmac_header)
    except CompanyUser.DoesNotExist:
        logger.error(
            f"❌ CompanyUser {company_user_id} not found for HMAC verification")
        return False
    except Exception as e:
        logger.error(
            f"❌ HMAC verification error for company_user_id={company_user_id}: {e}")
        return False

# ---------------- Webhook Task with Vector DB Updates ----------------


@shared_task
def process_webhook_task(company_user_id, topic, payload):
    try:
        # ---------------- Customers ----------------
        if topic in ["customers_create", "customers_update"]:
            addr = payload.get("default_address") or {}
            customer_obj, _ = Customer.objects.update_or_create(
                shopify_id=payload.get("id"),
                defaults={
                    "company_id": company_user_id,
                    "first_name": sanitize_text(remove_emoji(payload.get("first_name"))),
                    "last_name": sanitize_text(remove_emoji(payload.get("last_name"))),
                    "email": sanitize_text(remove_emoji(payload.get("email"))),
                    "city": sanitize_text(remove_emoji(addr.get("city"))),
                    "region": sanitize_text(remove_emoji(addr.get("province"))),
                    "country": sanitize_text(remove_emoji(addr.get("country"))),
                }
            )
            # Vector DB incremental update
            update_customer_vector(customer_obj)

        # ---------------- Products ----------------
        elif topic in ["products_create", "products_update"]:
            product_obj, _ = Product.objects.update_or_create(
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
            update_product_vector(product_obj)

        elif topic == "products_delete":
            Product.objects.filter(shopify_id=payload.get("id")).delete()
            # Optional: remove from vector DB if needed

        # ---------------- Orders ----------------
        elif topic in ["orders_create", "orders_updated"]:
            addr = payload.get("billing_address") or {}
            order_obj, _ = Order.objects.update_or_create(
                shopify_id=payload.get("id"),
                defaults={
                    "company_id": company_user_id,
                    "order_number": sanitize_text(remove_emoji(payload.get("order_number"))),
                    "fulfillment_status": sanitize_text(remove_emoji(payload.get("fulfillment_status"))),
                    "financial_status": sanitize_text(remove_emoji(payload.get("financial_status"))),
                    "currency": sanitize_text(remove_emoji(payload.get("currency"))),
                    "region": sanitize_text(remove_emoji(addr.get("country"))),
                }
            )
            update_order_vector(order_obj)

        # ---------------- Order Line Items ----------------
        elif topic in ["order_line_items_create", "order_line_items_update"]:
            line_item_obj, _ = OrderLineItem.objects.update_or_create(
                shopify_line_item_id=payload.get("id"),
                defaults={
                    "company_id": company_user_id,
                    "order_id": payload.get("order_id"),
                    "product_id": payload.get("product_id"),
                    "variant_id": payload.get("variant_id"),
                    "quantity": payload.get("quantity"),
                    "price": payload.get("price"),
                    "discount_allocated": payload.get("discount_allocated"),
                    "total": payload.get("total")
                }
            )
            update_order_line_item_vector(line_item_obj)

        # ---------------- Product Variants ----------------
        elif topic in ["product_variants_create", "product_variants_update"]:
            variant_obj, _ = ProductVariant.objects.update_or_create(
                shopify_id=payload.get("id"),
                defaults={
                    "company_id": company_user_id,
                    "product_id": payload.get("product_id"),
                    "title": payload.get("title"),
                    "sku": payload.get("sku"),
                    "price": payload.get("price"),
                    "compare_at_price": payload.get("compare_at_price"),
                    "cost": payload.get("cost"),
                    "inventory_quantity": payload.get("inventory_quantity")
                }
            )
            update_variant_vector(variant_obj)

        # ---------------- Collections ----------------
        elif topic in ["collections_create", "collections_update"]:
            collection_obj, _ = Collection.objects.update_or_create(
                shopify_id=payload.get("id"),
                defaults={
                    "company_id": company_user_id,
                    "title": payload.get("title"),
                    "handle": payload.get("handle"),
                    "updated_at": payload.get("updated_at"),
                    "image_src": payload.get("image_src")
                }
            )
            update_collection_vector(collection_obj)

        elif topic == "collections_delete":
            Collection.objects.filter(shopify_id=payload.get("id")).delete()

        # ---------------- Collection Items ----------------
        elif topic in ["collection_items_create", "collection_items_update"]:
            coll_item_obj, _ = CollectionItem.objects.update_or_create(
                collection_id=payload.get("collection_id"),
                product_id=payload.get("product_id"),
                defaults={
                    "image_src": payload.get("image_src")
                }
            )
            update_collection_item_vector(coll_item_obj)

        # ---------------- Promotional Data ----------------
        elif topic in ["promotional_data_create", "promotional_data_update"]:
            promo_obj, _ = PromotionalData.objects.update_or_create(
                user_id=company_user_id,
                date=payload.get("date"),
                campaign_name=payload.get("campaign_name"),
                ad_group_name=payload.get("ad_group_name"),
                defaults={
                    "clicks": payload.get("clicks"),
                    "impressions": payload.get("impressions"),
                    "cost": payload.get("cost"),
                    "conversions": payload.get("conversions"),
                    "conversion_value": payload.get("conversion_value"),
                    "ctr": payload.get("ctr"),
                    "cpc": payload.get("cpc"),
                    "roas": payload.get("roas"),
                }
            )
            update_promotional_vector(promo_obj)

    except Exception as e:
        logger.error(
            f"❌ Webhook task error ({topic}) for company_user_id={company_user_id}: {e}")


# Initialize once globally for efficiency
model = SentenceTransformer('all-MiniLM-L6-v2')


def add_or_update_vector(text, metadata, id_prefix, obj_id, company_user_id):
    folder_path = f"D:/TROOBA_PRODUCTION/chroma_db/tenant_{company_user_id}"
    client = chromadb.PersistentClient(path=folder_path)
    collection_name = f"tenant_{company_user_id}"
    vector_collection = client.get_or_create_collection(name=collection_name)

    vector_collection.add(
        documents=[text],
        ids=[f"{id_prefix}_{obj_id}"],
        embeddings=model.encode([text], convert_to_tensor=False),
        metadatas=[metadata]
    )


def update_customer_vector(customer):
    text = f"Customer ID: {customer.id}, Shopify ID: {customer.shopify_id}, Name: {customer.first_name} {customer.last_name}, Email: {customer.email}, Total Spent: {customer.total_spent}"
    metadata = {
        "id": customer.id,
        "shopify_id": customer.shopify_id,
        "company_id": customer.company_id,
        "total_spent": float(customer.total_spent or 0)
    }
    add_or_update_vector(text, metadata, "customer",
                         customer.id, customer.company_id)


def update_product_vector(product):
    text = f"Product ID: {product.id}, Shopify ID: {product.shopify_id}, Title: {product.title}, Vendor: {product.vendor}, Product Type: {product.product_type}, Tags: {product.tags}"
    metadata = {
        "id": product.id,
        "shopify_id": product.shopify_id,
        "company_id": product.company_id
    }
    add_or_update_vector(text, metadata, "product",
                         product.id, product.company_id)


def update_order_vector(order):
    text = f"Order ID: {order.id}, Shopify ID: {order.shopify_id}, Order Number: {order.order_number}, Total: {order.total_price}, Fulfillment: {order.fulfillment_status}, Financial: {order.financial_status}"
    metadata = {
        "id": order.id,
        "shopify_id": order.shopify_id,
        "company_id": order.company_id,
        "total_price": float(order.total_price or 0)
    }
    add_or_update_vector(text, metadata, "order", order.id, order.company_id)


# ---------------- Webhook View ----------------
@csrf_exempt
def shopify_webhook_view(request, company_user_id, topic):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid method"}, status=405)
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

        # Create the user
        user = CompanyUser(company=company, email=email)
        user.set_password(password)
        user.save()

        # Create a row in Prompt table with company name
        Prompt.objects.create(
            company=company,  # store the company name
            prompt=f"Welcome prompt for {company}",
        )

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
        company_name = user.company
        company_shopify_token = user.shopify_access_token
        company_shopify_url = user.shopify_store_url

        # Default flags
        shopify_connected = False
        decrypted_url = None

        # ✅ If Shopify credentials exist, decrypt and include URL
        if company_shopify_token and company_shopify_url:
            try:
                fernet = Fernet(settings.ENCRYPTION_KEY)
                decrypted_token = fernet.decrypt(
                    company_shopify_token.encode()).decode()
                decrypted_url = fernet.decrypt(
                    company_shopify_url.encode()).decode()
                shopify_connected = True
            except Exception as e:
                # Fallback if decryption fails
                decrypted_url = None
                shopify_connected = False

        return Response({
            "access_token": access_token,
            "expires_in_days": 15,
            "company_name": company_name,
            "shopify_access_token_check": shopify_connected,
            "shopify_store_url": decrypted_url if shopify_connected else None
        }, status=status.HTTP_200_OK)


# ---------------- Save & Get Shopify Credentials ----------------


class SaveShopifyCredentialsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        access_token = sanitize_text(
            remove_emoji(request.data.get("access_token")))
        store_url = sanitize_text(remove_emoji(request.data.get("store_url")))

        if not access_token or not store_url:
            return Response({"error": "Access token and store URL are required"}, status=status.HTTP_400_BAD_REQUEST)

        fernet = Fernet(settings.ENCRYPTION_KEY)
        user.shopify_access_token = fernet.encrypt(
            access_token.encode()).decode()
        user.shopify_store_url = fernet.encrypt(store_url.encode()).decode()

        if not user.webhook_secret:
            user.webhook_secret = secrets.token_hex(32)

        user.save()

        # Chain Shopify data fetch and vector training
        chain(
            fetch_shopify_data_task.s(user.id),
            train_vector_db_task.s(user.id)
        ).apply_async()

        return Response({"message": "Shopify credentials saved, data sync and training scheduled", "user_id": user.id}, status=status.HTTP_200_OK)


class GetShopifyCredentialsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response
        if not user.shopify_access_token or not user.shopify_store_url:
            return Response({"error": "No Shopify credentials found"}, status=status.HTTP_404_NOT_FOUND)
        try:
            fernet = Fernet(settings.ENCRYPTION_KEY)
            decrypted_token = fernet.decrypt(
                user.shopify_access_token.encode()).decode()
            decrypted_url = fernet.decrypt(
                user.shopify_store_url.encode()).decode()
            return Response({"shopify_access_token": decrypted_token, "shopify_store_url": decrypted_url}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": f"Failed to decrypt credentials: {str(e)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# Promotional Data Fetching From Excel Sheet


logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class UploadPromotionalDataView(View):
    def post(self, request):
        # ✅ Get user from token
        user, error_response = get_user_from_token(
            request)  # `user` = CompanyUser object
        if error_response:
            return error_response

        # ✅ File check
        excel_file = request.FILES.get('file')
        if not excel_file:
            return JsonResponse({"error": "No file uploaded"}, status=400)

        try:
            # ✅ Force Excel to read all values as string
            df = pd.read_excel(excel_file, dtype=str, engine="openpyxl")
            today = date.today()

            def safe_int(value, default=0):
                try:
                    if pd.isna(value) or str(value).strip().lower() in ["", "nan", "none"]:
                        return default
                    return int(float(value))
                except Exception:
                    return default

            def safe_float(value, default=None):
                try:
                    if pd.isna(value) or str(value).strip().lower() in ["", "nan", "none"]:
                        return default
                    return float(value)
                except Exception:
                    return default

            for index, row in df.iterrows():
                raw_variant_id = row.get('variant_id') or row.get(
                    'varient_id')  # handle typo

                # ✅ Skip rows with missing variant_id
                if pd.isna(raw_variant_id) or not str(raw_variant_id).strip():
                    logger.warning(
                        f"⚠️ Skipping row {index} - missing variant_id")
                    continue

                # ✅ Convert safely to integer
                try:
                    variant_id = int(float(str(raw_variant_id).strip()))
                except Exception as ex:
                    logger.error(
                        f"❌ Invalid variant_id at row {index}: {raw_variant_id} ({ex})")
                    continue

                # ✅ Create or update record
                PromotionalData.objects.update_or_create(
                    variant_id=variant_id,
                    date=today,
                    defaults={
                        'user_id': user,
                        'image_url': row.get('Image'),
                        'title': row.get('Title'),
                        'price': safe_float(row.get('Price')),
                        'clicks': safe_int(row.get('Clicks')),
                        'impressions': safe_int(row.get('Impressions')),
                        'ctr': safe_float(row.get('CTR')),
                        'currency_code': row.get('CurrencyCode'),
                        'avg_cpc': safe_float(row.get('AvgCPC')),
                        'cost': safe_float(row.get('Cost')),
                        'conversions': safe_int(row.get('Conversions')),
                        'conversion_value': safe_float(row.get('ConvValue')),
                        'conv_value_per_cost': safe_float(row.get('ConvValue/cost')),
                        'cost_per_conversion': safe_float(row.get('Cost/conv.')),
                        'conversion_rate': safe_float(row.get('ConvRate')),
                    }
                )

            return JsonResponse({"message": "Promotional data uploaded successfully"}, status=200)

        except Exception as e:
            logger.error(f"Error uploading promotional data: {e}")
            return JsonResponse({"error": str(e)}, status=500)


# --------------------------------------------------------------------------------

logger = logging.getLogger(__name__)

# ---------------------------
# Celery Task: Fetch Collections for a Single User
# ---------------------------


@shared_task(bind=True)
def fetch_collections_task(self, company_user_id):
    """
    Fetch Shopify collections and products for a given user ID.
    - Updates or creates Collection and CollectionItem objects.
    - Fetches product images from Shopify products API.
    """
    print("🚀 Starting fetch_collections_task...")
    logger.info("Fetch collections task started")

    try:
        # Get the CompanyUser
        user = CompanyUser.objects.get(id=company_user_id)
        print(f"🔑 Fetching collections for user: {user.email} (ID: {user.id})")

        # Decrypt Shopify credentials
        fernet = Fernet(settings.ENCRYPTION_KEY)
        access_token = fernet.decrypt(
            user.shopify_access_token.encode()).decode()
        shopify_store_url = fernet.decrypt(
            user.shopify_store_url.encode()).decode()
        print(f"🔐 Decrypted Shopify URL: {shopify_store_url}")

        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json"
        }

        # Fetch custom collections from Shopify
        collections_url = f"https://{shopify_store_url}/admin/api/2025-01/custom_collections.json"
        print(f"🌐 Sending request to Shopify collections API...")
        response = requests.get(collections_url, headers=headers)
        print(f"📦 Shopify response code: {response.status_code}")
        if response.status_code != 200:
            print(
                f"❌ Shopify API error: {response.status_code} {response.text}")
            return

        collections = response.json().get("custom_collections", [])
        print(f"📂 Fetched {len(collections)} collections")

        # Loop through collections
        for c in collections:
            title = c.get("title")
            print(f"🔹 Processing collection: {title}")

            collection, created = Collection.objects.update_or_create(
                shopify_id=c.get("id"),
                defaults={
                    "company_id": user.id,
                    "title": title,
                    "handle": c.get("handle"),
                    "updated_at": c.get("updated_at"),
                    "image_src": c.get("image", {}).get("src") if c.get("image") else None,
                },
            )
            print(
                f"{'✅ Created' if created else '♻ Updated'} collection '{collection.title}'")

            # Fetch products for this collection
            collects_url = f"https://{shopify_store_url}/admin/api/2025-01/collects.json?collection_id={collection.shopify_id}"
            print(f"🛒 Fetching products from: {collects_url}")
            collects_resp = requests.get(collects_url, headers=headers)
            print(f"📦 Collects response code: {collects_resp.status_code}")
            if collects_resp.status_code != 200:
                print(
                    f"❌ Error fetching products for collection '{collection.title}'")
                continue

            collects = collects_resp.json().get("collects", [])
            print(
                f"📌 Found {len(collects)} products in collection '{collection.title}'")

            for item in collects:
                product_id = item.get("product_id")
                print(
                    f"➕ Adding/updating product {product_id} in collection '{collection.title}'")

                # Fetch product details to get image
                product_url = f"https://{shopify_store_url}/admin/api/2025-01/products/{product_id}.json"
                product_resp = requests.get(product_url, headers=headers)
                image_src = None

                if product_resp.status_code == 200:
                    product_data = product_resp.json().get("product", {})
                    images = product_data.get("images", [])
                    if images:
                        # take first product image
                        image_src = images[0].get("src")

                # Save or update product in CollectionItem
                CollectionItem.objects.update_or_create(
                    collection=collection,
                    product_id=product_id,
                    defaults={"image_src": image_src},
                )
                print(f"✅ Product {product_id} saved with image {image_src}")

        print("🎉 Collections fetch completed successfully")
        logger.info("Collections fetch completed successfully")

    except Exception as e:
        print("❌ Error occurred in fetch_collections_task:", e)
        logger.error("Error in fetch_collections_task", exc_info=True)


# ---------------------------
# API View: Manual Trigger
# ---------------------------
class FetchCollectionsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        """
        Manual trigger endpoint to fetch collections for a user.
        Uses JWT token to identify the user.
        """
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        print(f"User fetched from token: {user.email} (ID: {user.id})")

        # Call the Celery task asynchronously
        print("Calling fetch collections task asynchronously...")
        fetch_collections_task.apply_async(args=[user.id])
        print("Task has been queued")

        return Response({"message": "Collection fetch started"}, status=status.HTTP_200_OK)


# ---------------------------
# Celery Beat Scheduled Task: Automatic 24-hour Fetch
# ---------------------------

@shared_task
def fetch_collections_for_all_users():
    """
    Automatic task that runs every 24 hours.
    - Fetches collections for all users who have Shopify credentials.
    """
    users = CompanyUser.objects.filter(
        shopify_access_token__isnull=False, shopify_store_url__isnull=False)
    print(f"📅 Running scheduled fetch for {users.count()} users")
    for user in users:
        print(f"➡️ Fetching collections for user: {user.email}")
        fetch_collections_task.apply_async(args=[user.id])

# Collections are fetching but not images . images will be empty in CollectionItem table . Its not important but will do it in Version 2


# ===================================================================================================================


logger = logging.getLogger(__name__)

# -----------------------------
# Helper functions to sanitize metadata
# -----------------------------


def safe_int(val):
    return int(val) if val is not None else 0


def safe_float(val):
    return float(val) if val is not None else 0.0


def safe_str(val):
    return str(val) if val is not None else ""


def safe_bool(val):
    return bool(val) if val is not None else False

# -----------------------------
# Celery Task
# -----------------------------


@shared_task(bind=True, max_retries=3)
def train_vector_db_task(self, previous_result,company_user_id):
    logger.info(
        f"🚀 Starting Vector DB training for company_user_id={company_user_id}")

    try:
        # Fetch tenant-specific data
        orders = list(Order.objects.filter(company_id=company_user_id))
        order_items = list(OrderLineItem.objects.filter(
            company_id=company_user_id))
        customers = list(Customer.objects.filter(company_id=company_user_id))
        collections = list(Collection.objects.filter(
            company_id=company_user_id))
        collection_items = list(CollectionItem.objects.filter(
            collection__company_id=company_user_id))
        products = list(Product.objects.filter(company_id=company_user_id))
        variants = list(ProductVariant.objects.filter(
            company_id=company_user_id))
        promotions = list(PromotionalData.objects.filter(
            user_id=company_user_id))

        logger.info(f"📦 Data counts — Orders: {len(orders)}, OrderItems: {len(order_items)}, Customers: {len(customers)}, Collections: {len(collections)}, CollectionItems: {len(collection_items)}, Products: {len(products)}, Variants: {len(variants)}, Promotions: {len(promotions)}")

        # Initialize ChromaDB
        client = chromadb.PersistentClient(
            path=f"D:/TROOBA_PRODUCTION/chroma_db/tenant_{company_user_id}")
        collection_name = f"tenant_{company_user_id}"
        vector_collection = client.get_or_create_collection(
            name=collection_name)

        model = SentenceTransformer('all-MiniLM-L6-v2')
        batch_size = 500

        # -----------------------------
        # Helper for batch processing
        # -----------------------------
        def process_batch(items, get_text_fn, get_metadata_fn, id_prefix):
            for i in range(0, len(items), batch_size):
                batch = items[i:i+batch_size]
                texts = [get_text_fn(obj) for obj in batch]
                embeddings = model.encode(texts, convert_to_tensor=False)
                ids = [f"{id_prefix}_{safe_int(obj.id)}" for obj in batch]
                metadatas = [get_metadata_fn(obj) for obj in batch]
                vector_collection.add(
                    documents=texts, ids=ids, embeddings=embeddings, metadatas=metadatas)
                logger.info(
                    f"✅ Batch {i//batch_size + 1}/{(len(items)-1)//batch_size + 1} for {id_prefix} persisted successfully.")

        # -----------------------------
        # Metadata & Text functions
        # -----------------------------
        def order_text(order):
            return (
                f"Order ID: {safe_int(order.id)}, Shopify ID: {safe_int(order.shopify_id)}, Company ID: {safe_int(order.company_id)}, "
                f"Customer ID: {safe_int(order.customer_id)}, Order Number: {safe_str(order.order_number)}, Order Date: {safe_str(order.order_date)}, "
                f"Fulfillment Status: {safe_str(order.fulfillment_status)}, Financial Status: {safe_str(order.financial_status)}, Currency: {safe_str(order.currency)}, "
                f"Total Price: {safe_float(order.total_price)}, Subtotal Price: {safe_float(order.subtotal_price)}, Total Tax: {safe_float(order.total_tax)}, Total Discount: {safe_float(order.total_discount)}, "
                f"Created At: {safe_str(order.created_at)}, Updated At: {safe_str(order.updated_at)}, Region: {safe_str(order.region)}"
            )

        def order_metadata(order):
            return {
                "id": safe_int(order.id),
                "shopify_id": safe_int(order.shopify_id),
                "company_id": safe_int(order.company_id),
                "customer_id": safe_int(order.customer_id),
                "total_price": safe_float(order.total_price),
                "subtotal_price": safe_float(order.subtotal_price),
                "total_tax": safe_float(order.total_tax),
                "total_discount": safe_float(order.total_discount)
            }

        def order_item_text(item):
            return (
                f"Line Item ID: {safe_int(item.id)}, Shopify Line Item ID: {safe_int(item.shopify_line_item_id)}, Company ID: {safe_int(item.company_id)}, "
                f"Order ID: {safe_int(item.order_id)}, Product ID: {safe_int(item.product_id)}, Variant ID: {safe_int(item.variant_id)}, "
                f"Quantity: {safe_int(item.quantity)}, Price: {safe_float(item.price)}, Discount Allocated: {safe_float(item.discount_allocated)}, Total: {safe_float(item.total)}"
            )

        def order_item_metadata(item):
            return {
                "id": safe_int(item.id),
                "order_id": safe_int(item.order_id),
                "product_id": safe_int(item.product_id),
                "variant_id": safe_int(item.variant_id),
                "quantity": safe_int(item.quantity),
                "price": safe_float(item.price)
            }

        def customer_text(customer):
            return (
                f"Customer ID: {safe_int(customer.id)}, Shopify ID: {safe_int(customer.shopify_id)}, Company ID: {safe_int(customer.company_id)}, "
                f"Email: {safe_str(customer.email)}, First Name: {safe_str(customer.first_name)}, Last Name: {safe_str(customer.last_name)}, "
                f"Phone: {safe_str(customer.phone)}, Created At: {safe_str(customer.created_at)}, Updated At: {safe_str(customer.updated_at)}, "
                f"City: {safe_str(customer.city)}, Region: {safe_str(customer.region)}, Country: {safe_str(customer.country)}, Total Spent: {safe_float(customer.total_spent)}"
            )

        def customer_metadata(customer):
            return {
                "id": safe_int(customer.id),
                "company_id": safe_int(customer.company_id),
                "total_spent": safe_float(customer.total_spent)
            }

        def collection_text(coll):
            return f"Collection ID: {safe_int(coll.id)}, Company ID: {safe_int(coll.company_id)}, Shopify ID: {safe_int(coll.shopify_id)}, Title: {safe_str(coll.title)}, Handle: {safe_str(coll.handle)}, Updated At: {safe_str(coll.updated_at)}"

        def collection_metadata(coll):
            return {
                "id": safe_int(coll.id),
                "company_id": safe_int(coll.company_id)
            }

        def collection_item_text(ci):
            return f"CollectionItem ID: {safe_int(ci.id)}, Collection ID: {safe_int(ci.collection_id)}, Product ID: {safe_int(ci.product_id)}, Image Src: {safe_str(ci.image_src)}"

        def collection_item_metadata(ci):
            return {
                "id": safe_int(ci.id),
                "collection_id": safe_int(ci.collection_id),
                "product_id": safe_int(ci.product_id)
            }

        def product_text(product):
            return f"Product ID: {safe_int(product.id)}, Shopify ID: {safe_int(product.shopify_id)}, Company ID: {safe_int(product.company_id)}, Title: {safe_str(product.title)}, Vendor: {safe_str(product.vendor)}, Product Type: {safe_str(product.product_type)}, Tags: {safe_str(product.tags)}"

        def product_metadata(product):
            return {
                "id": safe_int(product.id),
                "company_id": safe_int(product.company_id)
            }

        def variant_text(variant):
            return f"Variant ID: {safe_int(variant.id)}, Shopify ID: {safe_int(variant.shopify_id)}, Company ID: {safe_int(variant.company_id)}, Product ID: {safe_int(variant.product_id)}, Title: {safe_str(variant.title)}, SKU: {safe_str(variant.sku)}, Price: {safe_float(variant.price)}, Compare At Price: {safe_float(variant.compare_at_price)}, Cost: {safe_float(variant.cost)}, Inventory Quantity: {safe_int(variant.inventory_quantity)}"

        def variant_metadata(variant):
            return {
                "id": safe_int(variant.id),
                "company_id": safe_int(variant.company_id),
                "product_id": safe_int(variant.product_id),
                "price": safe_float(variant.price),
                "compare_at_price": safe_float(variant.compare_at_price),
                "cost": safe_float(variant.cost),
                "inventory_quantity": safe_int(variant.inventory_quantity)
            }

        def promo_text(promo):
            return f"Promo: {safe_str(promo.campaign_name)}, Clicks: {safe_int(promo.clicks)}, Impressions: {safe_int(promo.impressions)}, Cost: {safe_float(promo.cost)}"

        def promo_metadata(promo):
            return {
                "clicks": safe_int(promo.clicks),
                "impressions": safe_int(promo.impressions),
                "cost": safe_float(promo.cost),
                "conversions": safe_int(promo.conversions)
            }

        # -----------------------------
        # Process all batches
        # -----------------------------
        process_batch(orders, order_text, order_metadata, "order")
        process_batch(order_items, order_item_text,
                      order_item_metadata, "orderitem")
        process_batch(customers, customer_text, customer_metadata, "customer")
        process_batch(collections, collection_text,
                      collection_metadata, "collection")
        process_batch(collection_items, collection_item_text,
                      collection_item_metadata, "collectionitem")
        process_batch(products, product_text, product_metadata, "product")
        process_batch(variants, variant_text, variant_metadata, "variant")
        process_batch(promotions, promo_text, promo_metadata, "promo")

        logger.info(
            f"✅ Vector DB training completed for company_user_id={company_user_id}")

    except Exception as exc:
        logger.error(
            f"❌ Vector DB training failed for company_user_id={company_user_id}: {exc}", exc_info=True)
        self.retry(exc=exc, countdown=60)

# -----------------------------
# API View
# -----------------------------


class TrainVectorDBView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        train_vector_db_task.apply_async(args=[user.id])
        return Response({"message": "Vector DB training started for your account"}, status=status.HTTP_200_OK)


# Testing Vector DBfrom django.http import JsonResponse

# views.py

# You can reuse your token helper


# -----------------------------
# API View
# -----------------------------


class VectorDBSearchView(APIView):
    authentication_classes = []  # Add JWT auth if needed
    permission_classes = []      # Add permissions if needed

    def post(self, request):
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        query_text = request.data.get("query")
        if not query_text:
            return Response({"error": "Query text is required"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            company_user_id = user.id
            folder_path = f"D:/TROOBA_PRODUCTION/chroma_db/tenant_{company_user_id}"
            client = chromadb.PersistentClient(path=folder_path)
            collection_name = f"tenant_{company_user_id}"
            vector_collection = client.get_or_create_collection(
                name=collection_name)

            model = SentenceTransformer('all-MiniLM-L6-v2')
            query_embedding = model.encode(
                [query_text], convert_to_tensor=False)

            numeric_id_filter = {
                "$or": [
                    {"id": int(query_text)}, {"shopify_id": int(query_text)}, {
                        "customer_id": int(query_text)},
                    {"order_id": int(query_text)}, {"product_id": int(query_text)}, {
                        "variant_id": int(query_text)},
                    {"collection_id": int(query_text)}, {
                        "user_id": int(query_text)}
                ]
            }

            results = vector_collection.query(
                query_embeddings=query_embedding,
                n_results=10000,
                where=numeric_id_filter,
                include=["documents", "distances", "metadatas"]
            )

            matches = []
            for i, doc in enumerate(results['documents'][0]):
                metadata = results['metadatas'][0][i]
                order_data = None

                # If order_id exists, look up Order by shopify_id
                order_id = metadata.get("order_id")
                if order_id:
                    try:
                        order = Order.objects.get(
                            shopify_id=order_id, company_id=company_user_id)
                        order_data = {
                            "id": order.id,
                            "shopify_id": order.shopify_id,
                            "order_number": order.order_number,
                            "order_date": str(order.order_date),
                            "fulfillment_status": order.fulfillment_status,
                            "financial_status": order.financial_status,
                            "currency": order.currency,
                            "total_price": float(order.total_price),
                            "subtotal_price": float(order.subtotal_price),
                            "total_tax": float(order.total_tax),
                            "total_discount": float(order.total_discount),
                            "region": order.region,
                            "created_at": str(order.created_at),
                            "updated_at": str(order.updated_at)
                        }
                    except Order.DoesNotExist:
                        order_data = None

                matches.append({
                    "text": doc,
                    "distance": results['distances'][0][i],
                    "metadata": metadata,
                    "order": order_data
                })

            return Response({"matches": matches}, status=status.HTTP_200_OK)

        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# views.py

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name='dispatch')
class UploadPurchaseOrderView(View):
    def post(self, request):
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        excel_file = request.FILES.get('file')
        if not excel_file:
            return JsonResponse({"error": "No file uploaded"}, status=400)

        try:
            df = pd.read_excel(excel_file)

            required_columns = [
                'PurchaseOrderID', 'SupplierName', 'SKUID(VariantID)',
                'OrderDate', 'DeliveryDate', 'QuantityOrdered'
            ]
            for col in required_columns:
                if col not in df.columns:
                    return JsonResponse(
                        {"error": f"Missing required column: {col}"},
                        status=400
                    )

            created_count = 0
            for _, row in df.iterrows():
                try:
                    PurchaseOrder.objects.create(
                        purchase_order_id=str(row['PurchaseOrderID']).strip(),
                        supplier_name=str(row['SupplierName']).strip(),
                        sku_id=str(row['SKUID(VariantID)']).strip(),
                        order_date=str(row['OrderDate']),
                        delivery_date=str(row['DeliveryDate']),
                        quantity_ordered=int(row['QuantityOrdered']),
                        company=user
                    )
                    created_count += 1

                except Exception as row_error:
                    logger.warning(f"Skipping row due to error: {row_error}")

            return JsonResponse({
                "message": "Purchase orders uploaded successfully",
                "rows_created": created_count,
                "total_rows": len(df)
            }, status=200)

        except Exception as e:
            logger.error(f"Error uploading purchase orders: {e}")
            return JsonResponse({"error": str(e)}, status=500)
