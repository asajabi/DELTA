# inventory/urls.py
from django.urls import path
from . import views

urlpatterns = [
    path('search/', views.part_search, name='part_search'),
    
    path('scanner/', views.scanner_view, name='scanner_view'),
    
    # The new selling path
    path('sell/<int:stock_id>/', views.sell_part, name='sell_part'),

    # THE NEW HISTORY PATH
    path('history/', views.sales_history, name='sales_history'),
    
    # NEW PATH
    path('history/export/', views.export_sales_csv, name='export_sales_csv'),
    
    # NEW PATH
    path('low-stock/', views.low_stock_list, name='low_stock_list'),
    
    path('add-to-cart/<int:stock_id>/', views.add_to_cart, name='add_to_cart'),
    
    path('cart/', views.cart_view, name='cart_view'),
    
    path('checkout/', views.finalize_order, name='finalize_order'),
   
    path('receipt/<str:order_id>/', views.receipt_view, name='receipt_view'),
    
    path('cart/update/<int:stock_id>/', views.update_cart_item, name='update_cart_item'),

    path('cart/update/<int:stock_id>/', views.update_cart_item, name='update_cart_item'),
    
    path('orders/', views.order_list, name='order_list'),
]