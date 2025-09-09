from django.urls import path
from CoreApplication.views import RegisterView,LoginView,SaveShopifyCredentialsView,GetShopifyCredentialsView
from CoreApplication.views import shopify_webhook_view,UploadPromotionalDataView,FetchCollectionsView,TrainVectorDBView,VectorDBSearchView

urlpatterns = [
    path('Register/', RegisterView.as_view()), #Class based function so using .as_view()
    path('Login/',LoginView.as_view()), #Class based function so using .as_view()
    path('ShopifyDetails/',SaveShopifyCredentialsView.as_view()), #Stores Shopify token n link
    path('ShopifyDecryptDetails/',GetShopifyCredentialsView.as_view()),
    path("webhooks/<int:company_user_id>/<str:topic>/", shopify_webhook_view, name="shopify_webhook"),
    path('PromotionalExcel/',UploadPromotionalDataView.as_view(),name='PromotionalExcel'),
    path('FetchCollections/',FetchCollectionsView.as_view(),name='FetchCollections'),
    path('TrainVectorDB/', TrainVectorDBView.as_view(), name='TrainVectorDB'),
    path('VectorDBSearch/', VectorDBSearchView.as_view(), name='VectorDBSearch'), #Using same view as it has both get n post methods

]

