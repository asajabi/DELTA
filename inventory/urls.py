from django.urls import path
from . import views

urlpatterns = [
    path("search/", views.part_search, name="part_search"),
    path("vehicles/", views.vehicle_catalog, name="vehicle_catalog"),
    path("pos/", views.pos_console, name="pos_console"),
    path("add-to-cart/<int:stock_id>/", views.add_to_cart, name="add_to_cart"),
    path("cart/", views.cart_view, name="cart_view"),
    path("cart/update/<int:stock_id>/", views.update_cart_item, name="update_cart_item"),
    path("cart/clear/", views.clear_cart, name="clear_cart"),
    path("checkout/", views.finalize_order, name="finalize_order"),
    path("receipt/<str:order_id>/", views.receipt_view, name="receipt_view"),
    path("orders/", views.order_list, name="order_list"),
    path("scanner/", views.scanner_view, name="scanner_view"),
    path("sell/<int:stock_id>/", views.sell_part, name="sell_part"),
    path("reports/", views.reports_dashboard, name="reports_dashboard"),
    path("history/", views.sales_history, name="sales_history"),
    path("history/refund/<int:sale_id>/", views.refund_sale, name="refund_sale"),
    path("history/export/", views.export_sales_csv, name="export_sales_csv"),
    path("low-stock/", views.low_stock_list, name="low_stock_list"),
]
