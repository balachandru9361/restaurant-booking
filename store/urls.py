from django.urls import path

from . import views

app_name = "store"

urlpatterns = [
    path("login/", views.store_login, name="login"),
    path("logout/", views.store_logout, name="logout"),
    path("", views.dashboard, name="dashboard"),
    path("items/", views.item_list, name="item_list"),
    path("items/add/", views.item_add, name="item_add"),
    path("items/<int:pk>/edit/", views.item_edit, name="item_edit"),
    path("items/<int:pk>/stock/", views.stock_update, name="stock_update"),
    path("items/<int:pk>/remove/", views.item_remove, name="item_remove"),
    path("categories/", views.categories, name="categories"),
    path("categories/<int:pk>/delete/", views.category_delete, name="category_delete"),
    path("movements/", views.movements, name="movements"),
    path("stock-in/", views.stock_in, name="stock_in"),
    path("stock-in/add/", views.stock_in_add, name="stock_in_add"),
    path("stock-out/", views.stock_out, name="stock_out"),
    path("stock-out/add/", views.stock_out_add, name="stock_out_add"),
    path("damage/", views.damage, name="damage"),
    path("damage/add/", views.damage_add, name="damage_add"),
    path("alerts/", views.stock_alert, name="stock_alert"),
]
