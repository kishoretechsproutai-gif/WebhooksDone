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


# 🔑 Reusable helper function inside views.py
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


class SaveShopifyCredentialsView(APIView):
    authentication_classes = [] 
    permission_classes = [AllowAny]
    def post(self, request):
        # ✅ Reuse helper function
        user, error_response = get_user_from_token(request)
        if error_response:
            return error_response

        access_token = request.data.get("access_token")
        store_url = request.data.get("store_url")

        if not access_token or not store_url:
            return Response(
                {"error": "Access token and store URL are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 🔒 Encrypt before saving
        fernet = Fernet(settings.ENCRYPTION_KEY)
        encrypted_token = fernet.encrypt(access_token.encode()).decode()
        encrypted_url = fernet.encrypt(store_url.encode()).decode()

        user.shopify_access_token = encrypted_token
        user.shopify_store_url = encrypted_url
        user.save()

        return Response(
            {"message": "Shopify credentials saved successfully"},
            status=status.HTTP_200_OK,
        )