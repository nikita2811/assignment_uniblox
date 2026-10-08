from django.urls import path
from store.views.cartViews import CartItemListView,CartCreateView,CartDetailView,CartItemDetailView
from store.views.orderViews import OrderDetailView,CartCheckoutView
from store.views.productViews import ProductDetailView,ProductListView,AdminProductCreateView,AdminProductDetailView,AdminProductInventoryView

urlpatterns=[
   
    path("carts/", CartCreateView.as_view(), name="cart-create"),
    path("carts/<str:cart_id>/", CartDetailView.as_view(), name="cart-detail"),
    path("carts/<str:cart_id>/items/", CartItemListView.as_view(), name="cart-items"),
    path("carts/<str:cart_id>/items/<str:product_id>/",CartItemDetailView.as_view(), name="cart-item-detail"),
    path("carts/<str:cart_id>/checkout/", CartCheckoutView.as_view(), name="cart-checkout"),
    path("orders/<str:order_id>/", OrderDetailView.as_view(), name="order-detail"),
    path("products/", ProductListView.as_view(), name="product-list"),
    path("products/<str:product_id>/", ProductDetailView.as_view(), name="product-detail"),
    # administrative
    path("admin/products/", AdminProductCreateView.as_view(), name="admin-product-create"),
    path("admin/products/<str:product_id>/", AdminProductDetailView.as_view(),
         name="admin-product-detail"),
    path("admin/products/<str:product_id>/adjust-inventory/", AdminProductInventoryView.as_view(),
         name="admin-product-adjust-inventory"),
]