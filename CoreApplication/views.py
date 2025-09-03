import base64, hmac, hashlib, json, time, re, logging, requests
from decimal import Decimal
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

logger = logging.getLogger(__name__)

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


# ---------------- Webhook Helper ----------------
def create_webhook(shop_url, token, topic, callback_url):
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
        

@shared_task(bind=True, max_retries=3)
def fetch_shopify_data_task(self, company_user_id):
    logger.info(f"🚀 Starting Shopify sync for company_user_id={company_user_id}")
    try:
        user = CompanyUser.objects.get(id=company_user_id)
        logger.info(f"✅ Found CompanyUser: {user.email}")

        fernet = Fernet(settings.ENCRYPTION_KEY)
        access_token = fernet.decrypt(user.shopify_access_token.encode()).decode()
        shopify_store_url = fernet.decrypt(user.shopify_store_url.encode()).decode()
        logger.info(f"🔓 Decrypted credentials for {shopify_store_url}")

        headers = {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}

        # --- Customers ---
        url = f"https://{shopify_store_url}/admin/api/2025-04/customers.json?limit=250"
        for page_no, page in enumerate(fetch_pages(url, headers), start=1):
            logger.info(f"👥 Customers: Processing page {page_no}, {len(page)} records")
            for c in page:
                addr = c.get("default_address") or {}
                Customer.objects.update_or_create(
                    shopify_id=c.get("id"),
                    defaults={
                        "company": user,
                        "email": c.get("email"),
                        "first_name": c.get("first_name"),
                        "last_name": c.get("last_name"),
                        "phone": c.get("phone"),
                        "created_at": c.get("created_at"),
                        "updated_at": c.get("updated_at"),
                        "city": addr.get("city"),
                        "region": addr.get("province"),
                        "country": addr.get("country"),
                        "total_spent": Decimal(c.get("total_spent") or "0.00"),
                    },
                )
            logger.info(f"✅ Customers page {page_no} saved")

        # --- Products ---
        url = f"https://{shopify_store_url}/admin/api/2025-01/products.json?limit=250"
        for page_no, page in enumerate(fetch_pages(url, headers), start=1):
            logger.info(f"📦 Products: Processing page {page_no}, {len(page)} records")
            for p in page:
                product, _ = Product.objects.update_or_create(
                    shopify_id=p.get("id"),
                    defaults={
                        "company": user,
                        "title": p.get("title"),
                        "vendor": p.get("vendor"),
                        "product_type": p.get("product_type"),
                        "tags": p.get("tags"),
                        "status": p.get("status"),
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
                            "title": v.get("title"),
                            "sku": v.get("sku"),
                            "price": Decimal(v.get("price") or "0.00"),
                            "compare_at_price": Decimal(v.get("compare_at_price") or "0.00"),
                            "cost": Decimal(v.get("cost") or "0.00"),
                            "inventory_quantity": v.get("inventory_quantity"),
                            "created_at": v.get("created_at"),
                            "updated_at": v.get("updated_at"),
                        },
                    )
            logger.info(f"✅ Products page {page_no} saved")

        # --- Orders ---
        url = f"https://{shopify_store_url}/admin/api/2025-01/orders.json?limit=250&status=any"
        for page_no, page in enumerate(fetch_pages(url, headers), start=1):
            logger.info(f"🧾 Orders: Processing page {page_no}, {len(page)} records")
            for o in page:
                order, _ = Order.objects.update_or_create(
                    shopify_id=o.get("id"),
                    defaults={
                        "company": user,
                        "customer_id": o.get("customer", {}).get("id"),
                        "order_number": o.get("order_number"),
                        "order_date": o.get("created_at"),
                        "fulfillment_status": o.get("fulfillment_status"),
                        "financial_status": o.get("financial_status"),
                        "currency": o.get("currency"),
                        "total_price": Decimal(o.get("total_price") or "0.00"),
                        "subtotal_price": Decimal(o.get("subtotal_price") or "0.00"),
                        "total_tax": Decimal(o.get("total_tax") or "0.00"),
                        "total_discount": Decimal(o.get("total_discounts") or "0.00"),
                        "created_at": o.get("created_at"),
                        "updated_at": o.get("updated_at"),
                    },
                )
                for li in o.get("line_items") or []:
                    OrderLineItem.objects.update_or_create(
                        shopify_line_item_id=li.get("id"),
                        defaults={
                            "company": user,
                            "order_id": order.shopify_id,
                            "product_id": li.get("product_id"),
                            "variant_id": li.get("variant_id"),
                            "quantity": li.get("quantity"),
                            "price": Decimal(li.get("price") or "0.00"),
                            "discount_allocated": Decimal(li.get("total_discount") or "0.00"),
                            "total": Decimal(li.get("price") or 0) * (li.get("quantity") or 0),
                        },
                    )
            logger.info(f"✅ Orders page {page_no} saved")

        # --- Webhooks ---
        logger.info("🔔 Registering webhooks...")
        topics = ["customers/create", "customers/update", "products/create",
                  "products/update", "products/delete", "orders/create", "orders/updated"]
        for topic in topics:
            callback = f"{settings.WEBHOOK_BASE_URL}/webhooks/{company_user_id}/{topic.replace('/', '_')}/"
            create_webhook(shopify_store_url, access_token, topic, callback)

        logger.info(f"🎉 Shopify sync completed for company_user_id={company_user_id}")

    except Exception as e:
        logger.error(f"❌ Sync error for company_user_id={company_user_id}: {e}")
        self.retry(exc=e, countdown=60)



# ---------------- Webhook Security ----------------
def verify_hmac(request):
    logger.info("🔐 Verifying Shopify webhook HMAC...")
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    body = request.body
    digest = base64.b64encode(
        hmac.new(settings.SHOPIFY_WEBHOOK_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(digest, hmac_header)


# ---------------- Webhook Task ----------------
@shared_task
def process_webhook_task(company_user_id, topic, payload):
    logger.info(f"⚙️ Processing webhook: {topic} for company_user_id={company_user_id}")
    try:
        if topic in ["customers_create", "customers_update"]:
            logger.info(f"👤 Upserting customer {payload.get('id')}")
            Customer.objects.update_or_create(shopify_id=payload.get("id"), defaults={"company_id": company_user_id})

        elif topic in ["products_create", "products_update"]:
            logger.info(f"📦 Upserting product {payload.get('id')}")
            Product.objects.update_or_create(shopify_id=payload.get("id"), defaults={"company_id": company_user_id})

        elif topic == "products_delete":
            logger.info(f"🗑️ Deleting product {payload.get('id')}")
            Product.objects.filter(shopify_id=payload.get("id")).delete()

        elif topic in ["orders_create", "orders_updated"]:
            logger.info(f"🧾 Upserting order {payload.get('id')}")
            Order.objects.update_or_create(shopify_id=payload.get("id"), defaults={"company_id": company_user_id})

        logger.info(f"✅ Finished webhook {topic} for company_user_id={company_user_id}")

    except Exception as e:
        logger.error(f"❌ Webhook task error ({topic}) for company_user_id={company_user_id}: {e}")


# ---------------- Webhook View ----------------
@csrf_exempt
def shopify_webhook_view(request, company_user_id, topic):
    logger.info(f"📩 Incoming webhook: topic={topic}, company_user_id={company_user_id}")

    if request.method != "POST":
        logger.warning("⚠️ Invalid method for webhook")
        return JsonResponse({"error": "Invalid method"}, status=405)

    if not verify_hmac(request):
        logger.warning("❌ Invalid HMAC signature")
        return JsonResponse({"error": "Invalid HMAC"}, status=401)

    try:
        payload = json.loads(request.body.decode())
        logger.debug(f"📦 Webhook payload: {str(payload)[:300]}...")  # truncate
    except json.JSONDecodeError:
        logger.error("❌ Invalid JSON payload in webhook")
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    process_webhook_task.delay(company_user_id, topic, payload)
    logger.info(f"🚀 Webhook task queued: {topic} for company_user_id={company_user_id}")

    return HttpResponse(status=200)


# ---------------- Register & Login ----------------
class RegisterView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        logger.info("📝 RegisterView called")
        company = request.data.get("company")
        email = request.data.get("email")
        password = request.data.get("password")

        if not company or not email or not password:
            logger.warning("⚠️ Missing fields in registration")
            return Response({"error": "All fields are required"}, status=status.HTTP_400_BAD_REQUEST)

        if CompanyUser.objects.filter(email=email).exists():
            logger.warning("⚠️ Email already exists")
            return Response({"error": "Email already exists"}, status=status.HTTP_400_BAD_REQUEST)

        user = CompanyUser(company=company, email=email)
        user.set_password(password)
        user.save()
        logger.info(f"✅ User registered: {email}")

        return Response({"message": "User registered successfully"}, status=status.HTTP_201_CREATED)


class LoginView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        logger.info("🔐 LoginView called")
        email = request.data.get("email")
        password = request.data.get("password")

        try:
            user = CompanyUser.objects.get(email=email)
        except CompanyUser.DoesNotExist:
            logger.warning("❌ Invalid login: user not found")
            return Response({"error": "Invalid credentials"}, status=status.HTTP_400_BAD_REQUEST)

        if not user.check_password(password):
            logger.warning("❌ Invalid login: wrong password")
            return Response({"error": "Invalid credentials"}, status=status.HTTP_400_BAD_REQUEST)

        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)
        logger.info(f"✅ User logged in: {email}")

        return Response({"access_token": access_token, "expires_in_days": 15}, status=status.HTTP_200_OK)


# ---------------- Save & Get Shopify Credentials ----------------
class SaveShopifyCredentialsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        logger.info("💾 Saving Shopify credentials")
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        access_token = request.data.get("access_token")
        store_url = request.data.get("store_url")
        if not access_token or not store_url:
            logger.warning("⚠️ Missing Shopify credentials")
            return Response({"error": "Access token and store URL are required"},
                            status=status.HTTP_400_BAD_REQUEST)

        fernet = Fernet(settings.ENCRYPTION_KEY)
        user.shopify_access_token = fernet.encrypt(access_token.encode()).decode()
        user.shopify_store_url = fernet.encrypt(store_url.encode()).decode()
        user.save()
        logger.info("✅ Shopify credentials encrypted and saved")

        fetch_shopify_data_task.apply_async(args=[user.id], countdown=5)
        logger.info("🚀 Shopify sync task triggered")

        return Response({"message": "Shopify credentials saved, sync started", "user_id": user.id},
                        status=status.HTTP_200_OK)


class GetShopifyCredentialsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        logger.info("🔍 Fetching Shopify credentials")
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        if not user.shopify_access_token or not user.shopify_store_url:
            logger.warning("⚠️ No Shopify credentials found for user")
            return Response({"error": "No Shopify credentials found"}, status=status.HTTP_404_NOT_FOUND)

        try:
            fernet = Fernet(settings.ENCRYPTION_KEY)
            decrypted_token = fernet.decrypt(user.shopify_access_token.encode()).decode()
            decrypted_url = fernet.decrypt(user.shopify_store_url.encode()).decode()
            logger.info("✅ Shopify credentials decrypted")
            return Response({"shopify_access_token": decrypted_token, "shopify_store_url": decrypted_url},
                            status=status.HTTP_200_OK)
        except Exception as e:
            logger.error(f"❌ Failed to decrypt credentials: {e}")
            return Response({"error": f"Failed to decrypt credentials: {str(e)}"},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR)
