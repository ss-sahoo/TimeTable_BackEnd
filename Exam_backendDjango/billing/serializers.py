from rest_framework import serializers
from .models import BillingInvoice, InvoiceLineItem, SubscriptionPlan, InstituteSubscription, Transaction


class InvoiceLineItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = InvoiceLineItem
        fields = ['id', 'description', 'quantity', 'unit_price', 'total_price']


class BillingInvoiceSerializer(serializers.ModelSerializer):
    institute_name = serializers.CharField(source='institute.name', read_only=True)
    line_items = InvoiceLineItemSerializer(many=True, read_only=True)
    status = serializers.SerializerMethodField()

    class Meta:
        model = BillingInvoice
        fields = [
            'id', 'invoice_number', 'institute', 'institute_name',
            'billing_period_start', 'billing_period_end',
            'subtotal', 'tax_amount', 'total_amount',
            'is_paid', 'paid_at', 'due_date',
            'line_items', 'status', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'invoice_number', 'created_at', 'updated_at']

    def get_status(self, obj):
        from django.utils import timezone
        if obj.is_paid:
            return 'paid'
        if obj.due_date and obj.due_date < timezone.now().date():
            return 'overdue'
        return 'pending'


class SubscriptionPlanSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubscriptionPlan
        fields = ['id', 'name', 'description', 'price', 'max_centers', 'max_students', 'features', 'is_active']


class InstituteSubscriptionSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source='plan.name', read_only=True)
    institute_name = serializers.CharField(source='institute.name', read_only=True)

    class Meta:
        model = InstituteSubscription
        fields = ['id', 'institute', 'institute_name', 'plan', 'plan_name', 'start_date', 'end_date', 'status', 'auto_renew']


class TransactionSerializer(serializers.ModelSerializer):
    institute_name = serializers.CharField(source='institute.name', read_only=True)

    class Meta:
        model = Transaction
        fields = [
            'id', 'institute', 'institute_name', 'amount', 'transaction_type',
            'status', 'external_reference', 'description', 'metadata', 'created_at',
        ]
