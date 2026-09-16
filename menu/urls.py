from django.urls import path
from . import views

urlpatterns = [
    path('', views.home, name='home'),
    path('menu/', views.menu_list, name='menu_list'),
    path('menu/<int:item_id>/', views.item_detail, name='item_detail'),
]