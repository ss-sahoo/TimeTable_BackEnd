from django.utils import timezone
from django.db.models import Q
from rest_framework.views import APIView
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.pagination import PageNumberPagination

from .models import BillingInvoice, InvoiceLineItem, SubscriptionPlan, InstituteSubscription, Transaction
from .serializers import (
    BillingInvoiceSerializer, InvoiceLineItemSerializer,
    SubscriptionPlanSerializer, InstituteSubscriptionSerializer,
    TransactionSerializer,
)
from accounts.models import Institute
import uuid


class BillingInvoicePagination(PageNumberPagination):
    page_size = 15
    page_size_query_param = 'page_size'
    max_page_size = 100


def is_super_admin(user):
    return user.role in ('super_admin', 'SUPER_ADMIN', 'superadmin')


class BillingInvoiceListView(APIView):
    """List & Create invoices for the institute (super-admin scoped)."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        institute = getattr(user, 'institute', None)

        qs = BillingInvoice.objects.select_related('institute').prefetch_related('line_items')

        # Super-admins see all invoices for their institute
        if institute:
            qs = qs.filter(institute=institute)

        # Filters
        status_filter = request.query_params.get('status')
        if status_filter == 'paid':
            qs = qs.filter(is_paid=True)
        elif status_filter == 'overdue':
            today = timezone.now().date()
            qs = qs.filter(is_paid=False, due_date__lt=today)
        elif status_filter == 'pending':
            today = timezone.now().date()
            qs = qs.filter(is_paid=False, due_date__gte=today)

        search = request.query_params.get('search', '').strip()
        if search:
            qs = qs.filter(
                Q(invoice_number__icontains=search) |
                Q(institute__name__icontains=search)
            )

        qs = qs.order_by('-created_at')

        paginator = BillingInvoicePagination()
        page = paginator.paginate_queryset(qs, request)
        serializer = BillingInvoiceSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        user = request.user
        institute = getattr(user, 'institute', None)
        if not institute:
            return Response({'error': 'No institute found for this user.'}, status=status.HTTP_400_BAD_REQUEST)

        data = request.data.copy()
        data['institute'] = institute.id

        # Auto-generate invoice number
        count = BillingInvoice.objects.filter(institute=institute).count() + 1
        year = timezone.now().year
        data['invoice_number'] = f"INV-{year}-{str(count).zfill(4)}"

        serializer = BillingInvoiceSerializer(data=data)
        if serializer.is_valid():
            invoice = serializer.save()
            # Handle line items from nested data
            line_items = request.data.get('line_items', [])
            for item in line_items:
                InvoiceLineItem.objects.create(invoice=invoice, **item)
            return Response(BillingInvoiceSerializer(invoice).data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class BillingInvoiceDetailView(APIView):
    """Retrieve, Update, Delete a single invoice."""
    permission_classes = [IsAuthenticated]

    def get_object(self, pk):
        try:
            return BillingInvoice.objects.select_related('institute').prefetch_related('line_items').get(pk=pk)
        except BillingInvoice.DoesNotExist:
            return None

    def get(self, request, pk):
        invoice = self.get_object(pk)
        if not invoice:
            return Response({'error': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)
        return Response(BillingInvoiceSerializer(invoice).data)

    def put(self, request, pk):
        invoice = self.get_object(pk)
        if not invoice:
            return Response({'error': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)

        serializer = BillingInvoiceSerializer(invoice, data=request.data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, pk):
        invoice = self.get_object(pk)
        if not invoice:
            return Response({'error': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)
        invoice.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_invoice_paid(request, pk):
    """Mark a billing invoice as paid."""
    try:
        invoice = BillingInvoice.objects.get(pk=pk)
    except BillingInvoice.DoesNotExist:
        return Response({'error': 'Invoice not found.'}, status=status.HTTP_404_NOT_FOUND)

    invoice.is_paid = True
    invoice.paid_at = timezone.now()
    invoice.save()

    # Optionally record a transaction
    Transaction.objects.create(
        institute=invoice.institute,
        amount=invoice.total_amount,
        transaction_type='subscription_payment',
        status='success',
        description=f"Payment for invoice {invoice.invoice_number}",
        external_reference=request.data.get('transaction_id', ''),
    )

    return Response(BillingInvoiceSerializer(invoice).data)


class SubscriptionPlanListView(APIView):
    """List all active subscription plans."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        plans = SubscriptionPlan.objects.filter(is_active=True).order_by('price')
        return Response(SubscriptionPlanSerializer(plans, many=True).data)


class TransactionListView(APIView):
    """List transactions for the current institute."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        institute = getattr(user, 'institute', None)
        if not institute:
            return Response([])

        qs = Transaction.objects.filter(institute=institute).order_by('-created_at')[:50]
        return Response(TransactionSerializer(qs, many=True).data)


class BillingOverviewView(APIView):
    """Quick billing summary stats for the dashboard."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        institute = getattr(user, 'institute', None)
        if not institute:
            return Response({'total_invoices': 0, 'paid': 0, 'overdue': 0, 'pending': 0, 'total_revenue': 0})

        today = timezone.now().date()
        invoices = BillingInvoice.objects.filter(institute=institute)
        paid = invoices.filter(is_paid=True)
        overdue = invoices.filter(is_paid=False, due_date__lt=today)
        pending = invoices.filter(is_paid=False, due_date__gte=today)

        total_revenue = sum(float(i.total_amount) for i in paid)

        return Response({
            'total_invoices': invoices.count(),
            'paid': paid.count(),
            'overdue': overdue.count(),
            'pending': pending.count(),
            'total_revenue': total_revenue,
        })
