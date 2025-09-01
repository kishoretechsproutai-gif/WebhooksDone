from django.urls import path
from CoreApplication.views import RegisterView,LoginView,SaveShopifyCredentialsView,GetShopifyCredentialsView
urlpatterns = [
    path('Register/', RegisterView.as_view()), #Class based function so using .as_view()
    path('Login/',LoginView.as_view()), #Class based function so using .as_view()
    path('ShopifyDetails/',SaveShopifyCredentialsView.as_view()), #Stores Shopify token n link
    path('ShopifyDecryptDetails/',GetShopifyCredentialsView.as_view()),
]
