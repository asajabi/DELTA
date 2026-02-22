from django.urls import path
from . import views

urlpatterns = [
    # Dashboard & Search
    path('search/', views.part_search, name='part_search'),
    
    # NEW: The Vehicle Catalog Page (This was missing)
    path('vehicles/', views.vehicle_catalog, name='vehicle_catalog'),

    # Cart & Checkout
    path('add-to-cart/<int:stock_id>/', views.add_to_cart, name='add_to_cart'),
    path('cart/', views.cart_view, name='cart_view'),
    path('cart/update/<int:stock_id>/', views.update_cart_item, name='update_cart_item'),
    path('checkout/', views.finalize_order, name='finalize_order'),
    path('receipt/<str:order_id>/', views.receipt_view, name='receipt_view'),
    path('orders/', views.order_list, name='order_list'),

    # Tools
    path('scanner/', views.scanner_view, name='scanner_view'),
    path('sell/<int:stock_id>/', views.sell_part, name='sell_part'),

    # Reports (Admin Only)
    path('history/', views.sales_history, name='sales_history'),
    path('history/export/', views.export_sales_csv, name='export_sales_csv'),
    path('low-stock/', views.low_stock_list, name='low_stock_list'),
    
    # In inventory/urls.py
    path('pos/', views.pos_console, name='pos_console'),
    
    path('cart/clear/', views.clear_cart, name='clear_cart'),

]