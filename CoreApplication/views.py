from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import RefreshToken
from .models import CompanyUser
from rest_framework.permissions import AllowAny

class RegisterView(APIView):
    permission_classes = [AllowAny]
    def post(self, request):
        company = request.data.get("company")
        email = request.data.get("email")
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
        email = request.data.get("email")
        password = request.data.get("password")

        try:
            user = CompanyUser.objects.get(email=email)
        except CompanyUser.DoesNotExist:
            return Response({"error": "Invalid credentials"}, status=status.HTTP_400_BAD_REQUEST)

        if not user.check_password(password):
            return Response({"error": "Invalid credentials"}, status=status.HTTP_400_BAD_REQUEST)

        refresh = RefreshToken.for_user(user)  # uses SimpleJWT
        access_token = str(refresh.access_token)

        return Response({
            "access_token": access_token,
            "expires_in_days": 15
        }, status=status.HTTP_200_OK)
        

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework_simplejwt.tokens import AccessToken
from cryptography.fernet import Fernet
from django.conf import settings

from .models import CompanyUser


#Reusable helper function for getting the user id from JWT Token
def get_user_from_token(request):
    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return None, Response(
            {"error": "No token provided"},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        token = auth_header.split(" ")[1]  # "Bearer <token>"
        access_token = AccessToken(token)

        user_id = access_token.payload.get("user_id")
        if not user_id:
            return None, Response(
                {"error": "Invalid token"},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        user = CompanyUser.objects.get(id=user_id)
        return user, None

    except CompanyUser.DoesNotExist:
        return None, Response(
            {"error": "User not found"},
            status=status.HTTP_404_NOT_FOUND,
        )
    except Exception as e:
        return None, Response(
            {"error": str(e)},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    
from celery import shared_task
from decimal import Decimal
import requests
from django.conf import settings
from CoreApplication.models import CompanyUser, Customer, Product, ProductVariant, Order, OrderLineItem
import logging
from cryptography.fernet import Fernet
import time, re, json

logger = logging.getLogger(__name__)

# ---------------- Pagination Helper ----------------
def fetch_pages(url, headers):
    """Yield one page of Shopify results at a time (250 records)"""
    while url:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        for key, value in data.items():
            yield value or []

        link = resp.headers.get("Link")
        if link and 'rel="next"' in link:
            match = re.search(r'<([^>]+)>; rel="next"', link)
            url = match.group(1) if match else None
        else:
            url = None

        time.sleep(0.5)  # respect Shopify rate limits


# ---------------- Webhook Helper ----------------
def create_webhook(shop_url, token, topic, callback_url):
    headers = {
        "X-Shopify-Access-Token": token,
        "Content-Type": "application/json",
    }
    data = {"webhook": {"topic": topic, "address": callback_url, "format": "json"}}
    resp = requests.post(
        f"https://{shop_url}/admin/api/2025-01/webhooks.json",
        headers=headers,
        data=json.dumps(data),
        timeout=10,
    )
    if resp.status_code not in (200, 201):
        logger.error(f"❌ Webhook {topic} failed: {resp.text}")
    else:
        logger.info(f"✅ Webhook {topic} created")
    return resp.json()


# ---------------- Main Celery Task ----------------
@shared_task(bind=True, max_retries=3)
def fetch_shopify_data_task(self, company_user_id):
    try:
        # Credentials
        user = CompanyUser.objects.get(id=company_user_id)
        fernet = Fernet(settings.ENCRYPTION_KEY)
        access_token = fernet.decrypt(user.shopify_access_token).decode()
        shopify_store_url = fernet.decrypt(user.shopify_store_url).decode()

        headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }

        # --- Customers ---
        url = f"https://{shopify_store_url}/admin/api/2025-01/customers.json?limit=250"
        for page in fetch_pages(url, headers):
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

        # --- Products & Variants ---
        url = f"https://{shopify_store_url}/admin/api/2025-01/products.json?limit=250"
        for page in fetch_pages(url, headers):
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
                            "inventory_quantity": v.get("inventory_quantity") or 0,
                            "created_at": v.get("created_at"),
                            "updated_at": v.get("updated_at"),
                        },
                    )

        # --- Orders & Line Items ---
        url = f"https://{shopify_store_url}/admin/api/2025-01/orders.json?limit=250&status=any"
        for page in fetch_pages(url, headers):
            for o in page:
                customer_id = o["customer"].get("id") if o.get("customer") else None
                order, _ = Order.objects.update_or_create(
                    shopify_id=o.get("id"),
                    defaults={
                        "company": user,
                        "customer_id": customer_id,
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
                            "quantity": li.get("quantity") or 0,
                            "price": Decimal(li.get("price") or "0.00"),
                            "discount_allocated": Decimal(li.get("total_discount") or "0.00"),
                            "total": Decimal((Decimal(li.get("price") or 0) * (li.get("quantity") or 0))),
                        },
                    )

        # --- Webhook Registration ---
        topics = [
            "customers/create",
            "customers/update",
            "products/create",
            "products/update",
            "products/delete",
            "orders/create",
            "orders/updated",
        ]
        for topic in topics:
            callback = f"{settings.WEBHOOK_BASE_URL}/webhooks/{topic.replace('/', '_')}/"
            create_webhook(shopify_store_url, access_token, topic, callback)

    except Exception as e:
        logger.error(f"❌ Error syncing Shopify data for user {company_user_id}: {e}")
        self.retry(exc=e, countdown=60)








        # -------- View --------
class SaveShopifyCredentialsView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        access_token = request.data.get("access_token")
        store_url = request.data.get("store_url")
        if not access_token or not store_url:
            return Response({"error": "Access token and store URL are required"},
                            status=status.HTTP_400_BAD_REQUEST)

        # encrypt + save
        fernet = Fernet(settings.ENCRYPTION_KEY)
        encrypted_token = fernet.encrypt(access_token.encode()).decode()
        encrypted_url = fernet.encrypt(store_url.encode()).decode()

        user.shopify_access_token = encrypted_token
        user.shopify_store_url = encrypted_url
        user.save()

        # ✅ safer queue trigger
        fetch_shopify_data_task.apply_async(args=[user.id], countdown=5)

        return Response(
            {"message": "Shopify credentials saved successfully, background sync started",
             "user_id": user.id},
            status=status.HTTP_200_OK,
        )

    
    
from cryptography.fernet import Fernet
from django.conf import settings
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

class GetShopifyCredentialsView(APIView):
    authentication_classes = [] 
    permission_classes = [AllowAny]
    def get(self, request):
        # ✅ Get the user from JWT token
        user, error_response = get_user_from_token(request)
        
        # Check if there was an error in getting the user
        if error_response:
            return error_response

        # ✅ Check if credentials exist
        if not user.shopify_access_token or not user.shopify_store_url:
            return Response(
                {"error": "No Shopify credentials found"},
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            # 🔓 Decrypt
            fernet = Fernet(settings.ENCRYPTION_KEY)
            decrypted_token = fernet.decrypt(user.shopify_access_token.encode()).decode()
            decrypted_url = fernet.decrypt(user.shopify_store_url.encode()).decode()

            return Response(
                {
                    "shopify_access_token": decrypted_token,
                    "shopify_store_url": decrypted_url,
                },
                status=status.HTTP_200_OK,
            )

        except Exception as e:
            return Response(
                {"error": f"Failed to decrypt credentials: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )