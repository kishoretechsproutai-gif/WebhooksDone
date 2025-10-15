from django.urls import path
from . import views

urlpatterns = [
    path('prompts/', views.get_all_prompts),
    path('prompts/create/', views.create_prompt),
    path('prompts/<int:prompt_id>/', views.get_prompt),
    path('prompts/<int:prompt_id>/update/', views.update_prompt),
    path('prompts/<int:prompt_id>/delete/', views.delete_prompt),
]
