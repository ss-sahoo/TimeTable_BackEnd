from rest_framework import serializers
from .models import BillingInvoice, InvoiceLineItem, InstitutePricing, GlobalPricing
from accounts.models import Institute

class InvoiceLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLineItem
        fields = ['id', 'description', 'quantity', 'unit_price', 'total_price']

class BillingInvoiceSerializer(serializers.ModelSerializer):
    institute_name = serializers.CharField(source='institute.name', read_only=True)
    institute_email = serializers.CharField(source='institute.email', read_only=True, default='')
    line_items = InvoiceLineItemSerializer(many=True, read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = BillingInvoice
        fields = [
            'id', 'invoice_number', 'institute', 'institute_name', 'institute_email',
            'billing_period_start', 'billing_period_end',
            'subtotal', 'tax_amount', 'total_amount', 'is_paid', 'paid_at', 'due_date',
            'pdf_invoice', 'line_items', 'status', 'created_at'
        ]

    def get_status(self, obj):
        # Determine status based on is_paid, due_date etc.
        if obj.is_paid:
            return 'PAID'
        # Simplified
        return 'SENT'


class InstitutePricingSerializer(serializers.ModelSerializer):
    institute_name = serializers.CharField(source='institute.name', read_only=True)

    class Meta:
        model = InstitutePricing
        fields = '__all__'

class GlobalPricingSerializer(serializers.ModelSerializer):
    class Meta:
        model = GlobalPricing
        fields = '__all__'
