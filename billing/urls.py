from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import PlatformInvoiceViewSet, InstituteBillingInfoView, InstitutePricingViewSet, GlobalPricingViewSet, PlatformOwnerDashboardStatsView

router = DefaultRouter()
router.register(r'platform-invoices', PlatformInvoiceViewSet, basename='platform-invoice')
router.register(r'institute-pricing', InstitutePricingViewSet, basename='institute-pricing')
router.register(r'global-pricing', GlobalPricingViewSet, basename='global-pricing')

urlpatterns = [
    path('my-billing/', InstituteBillingInfoView.as_view(), name='my-billing'),
    path('platform-dashboard/', PlatformOwnerDashboardStatsView.as_view(), name='platform-dashboard'),
    path('', include(router.urls)),
]
