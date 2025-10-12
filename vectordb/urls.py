

from django.urls import path
from .views import GenericVectorSearchView,gemini_chatbot

urlpatterns = [
    path('vectordb/query/', GenericVectorSearchView.as_view(), name='vector_query') ,#Testing Vector DB Both number and text
    path('trooba_gemini_query/',gemini_chatbot, name='trooba_gemini_query') #Testing gemini 
]
