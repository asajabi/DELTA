from django.urls import path
from . import views

urlpatterns = [
    path('search/', views.part_search, name='part_search'),
    path('scan/', views.scanner_view, name='scanner'),
    path('sell/<int:stock_id>/', views.sell_part, name='sell_part'), # <--- NEW LINE
]