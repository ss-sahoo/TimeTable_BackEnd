from rest_framework import viewsets, permissions, status
from rest_framework.decorators import action
from rest_framework.response import Response
from django.utils import timezone
from .models import BillingInvoice, InstitutePricing, UsageMetric, InvoiceLineItem, GlobalPricing
from .serializers import BillingInvoiceSerializer, InstitutePricingSerializer, GlobalPricingSerializer
from django.db.models import Sum
from accounts.models import Institute
from rest_framework.views import APIView
from django.db import transaction
from decimal import Decimal

class IsPlatformOwner(permissions.BasePermission):
    def has_permission(self, request, view):
        return bool(request.user and request.user.is_authenticated and request.user.role == 'platform_owner')

class GlobalPricingViewSet(viewsets.ModelViewSet):
    """
    Viewset for Platform Owner to manage global platform-wide rates.
    """
    serializer_class = GlobalPricingSerializer
    permission_classes = [permissions.IsAuthenticated, IsPlatformOwner]
    
    def get_queryset(self):
        return GlobalPricing.objects.filter(id='00000000-0000-0000-0000-000000000001')

    def get_object(self):
        return GlobalPricing.get_instance()

    @action(detail=False, methods=['get', 'put', 'patch'])
    def current(self, request):
        instance = GlobalPricing.get_instance()
        if request.method == 'GET':
            serializer = self.get_serializer(instance)
            return Response(serializer.data)
        
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

class InstitutePricingViewSet(viewsets.ModelViewSet):
    """
    Viewset for Platform Owner to manage rates for each institute
    """
    serializer_class = InstitutePricingSerializer
    permission_classes = [permissions.IsAuthenticated, IsPlatformOwner]
    queryset = InstitutePricing.objects.all()

class PlatformInvoiceViewSet(viewsets.ModelViewSet):
    """
    Viewset for Platform Owner to manage all invoices
    """
    serializer_class = BillingInvoiceSerializer
    permission_classes = [permissions.IsAuthenticated, IsPlatformOwner]

    def get_queryset(self):
        return BillingInvoice.objects.all().order_by('-created_at')

    @action(detail=False, methods=['post'])
    def generate_invoice(self, request):
        """
        Calculates usage and generates an invoice for an institute.
        Body: { "institute_id": "uuid" }
        """
        institute_id = request.data.get('institute_id')
        if not institute_id:
            return Response({'error': 'institute_id is required'}, status=400)
            
        try:
            institute = Institute.objects.get(id=institute_id)
        except Institute.DoesNotExist:
            return Response({'error': 'Institute not found'}, status=404)
            
        # 1. Get rates (Try Institute Specific, fallback to Global)
        pricing = None
        try:
            pricing = InstitutePricing.objects.get(institute=institute)
        except InstitutePricing.DoesNotExist:
            pricing = GlobalPricing.get_instance()
            
        # 2. Get un-processed usage
        metrics = UsageMetric.objects.filter(institute=institute, processed=False)
        extra_rows = request.data.get('extra_rows', [])
        
        if not metrics.exists() and not extra_rows:
            return Response({'error': 'No new usage or custom charges to bill for this institute.'}, status=400)
            
        # 3. Aggregate usage
        usage_summary = {}
        for m in metrics:
            if m.metric_type not in usage_summary:
                usage_summary[m.metric_type] = Decimal('0.00')
            usage_summary[m.metric_type] += Decimal(str(m.quantity))
            
        with transaction.atomic():
            # Create Invoice
            now = timezone.now()
            invoice = BillingInvoice.objects.create(
                institute=institute,
                invoice_number=f"INV-{now.strftime('%Y%m%d')}-{str(now.timestamp())[-4:]}",
                billing_period_start=metrics.order_by('created_at').first().created_at.date(),
                billing_period_end=now.date(),
                due_date=(now + timezone.timedelta(days=15)).date(),
            )
            
            subtotal = Decimal('0.00')
            
            # Handle GST Rate
            gst_val = request.data.get('gst_rate', '18')
            try:
                gst_multiplier = Decimal(str(gst_val)) / Decimal('100')
            except:
                gst_multiplier = Decimal('0.18')

            # Mapping metric types to pricing fields and descriptions
            mapping = {
                'student_onboarding': (pricing.per_student_onboarding_fee, "Onboarding Charge"),
                'active_student': (pricing.per_active_student_monthly_fee, "Active Student Charge"),
                'exam_attempt': (pricing.per_exam_session_fee, "Exam Session Charge"),
                're_exam_attempt': (pricing.per_re_exam_fee, "Re-Exam Charge"),
                'proctoring_session': (pricing.per_proctoring_session_fee, "AI Proctoring Charge"),
                'storage_usage': (pricing.storage_per_gb_fee, "Cloud Storage Charge"),
            }
            
            for m_type, qty in usage_summary.items():
                rate, label = mapping.get(m_type, (Decimal('0.00'), f"Usage: {m_type}"))
                total_item_price = qty * rate
                
                if total_item_price > 0:
                    detailed_desc = f"{label} Charge\n({qty} units × ₹{rate}/unit)"
                    InvoiceLineItem.objects.create(
                        invoice=invoice,
                        description=detailed_desc,
                        quantity=qty,
                        unit_price=rate,
                        total_price=total_item_price
                    )
                    subtotal += total_item_price

            # Extra Rows
            extra_rows = request.data.get('extra_rows', [])
            for row in extra_rows:
                label = row.get('label', 'Extra Charge')
                amount = Decimal(str(row.get('amount', '0')))
                if amount != 0:
                    InvoiceLineItem.objects.create(
                        invoice=invoice,
                        description=label,
                        quantity=Decimal('1.00'),
                        unit_price=amount,
                        total_price=amount
                    )
                    subtotal += amount
            
            # Taxes (GST)
            tax = subtotal * gst_multiplier
            total = subtotal + tax
            
            invoice.subtotal = subtotal
            invoice.tax_amount = tax
            invoice.total_amount = total
            invoice.save()
            
            # Mark metrics as processed
            metrics.update(processed=True)
            
        return Response(BillingInvoiceSerializer(invoice).data)

    @action(detail=True, methods=['post'])
    def mark_paid(self, request, pk=None):
        invoice = self.get_object()
        invoice.is_paid = True
        invoice.paid_at = timezone.now()
        invoice.save()
        return Response({'status': 'Invoice marked as paid'})
        
    @action(detail=True, methods=['post'])
    def mark_unpaid(self, request, pk=None):
        invoice = self.get_object()
        invoice.is_paid = False
        invoice.paid_at = None
        invoice.save()
        return Response({'status': 'Invoice marked as unpaid'})

class PlatformOwnerDashboardStatsView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsPlatformOwner]

    def get(self, request):
        now = timezone.now()
        # 1. Financial Stats
        paid_invoices = BillingInvoice.objects.filter(is_paid=True)
        pending_invoices = BillingInvoice.objects.filter(is_paid=False)
        
        total_revenue = paid_invoices.aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
        pending_revenue = pending_invoices.aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
        
        # 2. Daily Revenue (Mocked for graph if not enough data, but let's try real data)
        # We'll group by day for the last 30 days
        last_30_days = []
        for i in range(29, -1, -1):
            day = now - timezone.timedelta(days=i)
            day_total = BillingInvoice.objects.filter(
                created_at__date=day.date()
            ).aggregate(Sum('total_amount'))['total_amount__sum'] or Decimal('0.00')
            last_30_days.append({
                'date': day.strftime('%d %b'),
                'amount': float(day_total)
            })

        return Response({
            'total_revenue': total_revenue,
            'pending_revenue': pending_revenue,
            'total_invoices': BillingInvoice.objects.count(),
            'revenue_trend': last_30_days
        })

class InstituteBillingInfoView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        user = request.user
        if not user.institute:
            return Response({'error': 'No institute linked to this user'}, status=400)
            
        institute = user.institute
        
        try:
            pricing = InstitutePricing.objects.get(institute=institute)
            pricing_data = {
                'per_student_onboarding_fee': pricing.per_student_onboarding_fee,
                'per_active_student_monthly_fee': pricing.per_active_student_monthly_fee,
                'per_exam_session_fee': pricing.per_exam_session_fee,
                'per_re_exam_fee': pricing.per_re_exam_fee,
                'platform_commission_percentage': pricing.platform_commission_percentage,
                'per_proctoring_session_fee': pricing.per_proctoring_session_fee,
                'storage_per_gb_fee': pricing.storage_per_gb_fee,
            }
        except InstitutePricing.DoesNotExist:
            pricing_data = None
            
        invoices = BillingInvoice.objects.filter(institute=institute).order_by('-created_at')
        invoices_data = BillingInvoiceSerializer(invoices, many=True).data

        return Response({
            'pricing': pricing_data,
            'invoices': invoices_data,
        })
