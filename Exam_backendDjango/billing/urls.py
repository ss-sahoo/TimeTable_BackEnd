from django.urls import path
from .views import (
    BillingInvoiceListView,
    BillingInvoiceDetailView,
    mark_invoice_paid,
    SubscriptionPlanListView,
    TransactionListView,
    BillingOverviewView,
)

urlpatterns = [
    path('invoices/', BillingInvoiceListView.as_view(), name='billing-invoice-list'),
    path('invoices/<uuid:pk>/', BillingInvoiceDetailView.as_view(), name='billing-invoice-detail'),
    path('invoices/<uuid:pk>/mark-paid/', mark_invoice_paid, name='billing-invoice-mark-paid'),
    path('plans/', SubscriptionPlanListView.as_view(), name='billing-plan-list'),
    path('transactions/', TransactionListView.as_view(), name='billing-transaction-list'),
    path('overview/', BillingOverviewView.as_view(), name='billing-overview'),
]
