from django.db import models
from accounts.models import Institute, User
import uuid

class TimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True

class SubscriptionPlan(TimeStampedModel):
    """
    Defines available SaaS plans (Basic, Pro, Enterprise, etc.)
    """
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Monthly price for this plan")
    max_centers = models.IntegerField(default=1)
    max_students = models.IntegerField(default=100)
    features = models.JSONField(default=dict, help_text="Store extra features like 'ai_proctoring': True")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} - {self.price}"

class InstituteSubscription(TimeStampedModel):
    """
    Tracks which institute is on which plan.
    """
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('cancelled', 'Cancelled'),
        ('trial', 'Trialing'),
    ]
    institute = models.OneToOneField(Institute, on_delete=models.CASCADE, related_name='subscription')
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.PROTECT)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    auto_renew = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.institute.name} - {self.plan.name}"

class Wallet(TimeStampedModel):
    """
    Credits/Balance for institutes to pay for variable consumption or re-exams.
    """
    institute = models.OneToOneField(Institute, on_delete=models.CASCADE, related_name='wallet')
    balance = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    currency = models.CharField(max_length=10, default='INR')

    def __str__(self):
        return f"{self.institute.name} Wallet - {self.balance}"

class Transaction(TimeStampedModel):
    """
    The financial ledger for all money movements.
    """
    TRANSACTION_TYPES = [
        ('subscription_payment', 'Subscription Payment'),
        ('wallet_topup', 'Wallet Top-up'),
        ('exam_fee', 'Exam Consumption Fee'),
        ('proctoring_fee', 'AI Proctoring Fee'),
        ('revenue_share', 'Revenue Share Commission'),
        ('payout', 'Payout to Institute'),
        ('refund', 'Refund'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('success', 'Success'),
        ('failed', 'Failed'),
    ]
    
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='transactions')
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    transaction_type = models.CharField(max_length=30, choices=TRANSACTION_TYPES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    external_reference = models.CharField(max_length=255, blank=True, null=True, help_text="Razorpay/Stripe Payment ID")
    description = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    def __str__(self):
        return f"{self.transaction_type} - {self.amount} - {self.status}"

class InstitutePricing(TimeStampedModel):
    """
    Specific pricing configuration for a particular institute.
    Allows the platform owner to set unique rates for every institute.
    """
    institute = models.OneToOneField(Institute, on_delete=models.CASCADE, related_name='pricing')
    
    # Per-Student charges
    per_student_onboarding_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    per_active_student_monthly_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    # Exam charges
    per_exam_session_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    per_re_exam_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    
    # Revenue Sharing
    platform_commission_percentage = models.DecimalField(max_digits=5, decimal_places=2, default=0.00, help_text="Percentage of student fees taken by platform")
    
    # Add-on services
    per_proctoring_session_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)
    storage_per_gb_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)

    def __str__(self):
        return f"Pricing for {self.institute.name}"

class BillingInvoice(TimeStampedModel):
    """
    Represents a specific bill generated for an institute.
    """
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='invoices')
    invoice_number = models.CharField(max_length=50, unique=True)
    billing_period_start = models.DateField()
    billing_period_end = models.DateField()
    
    subtotal = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    tax_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=15, decimal_places=2, default=0.00)
    
    is_paid = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)
    due_date = models.DateField()
    
    pdf_invoice = models.FileField(upload_to='invoices/', null=True, blank=True)

    def __str__(self):
        return f"Invoice {self.invoice_number} - {self.institute.name}"

class InvoiceLineItem(TimeStampedModel):
    """
    Breakdown of charges inside an invoice.
    """
    invoice = models.ForeignKey(BillingInvoice, on_delete=models.CASCADE, related_name='line_items')
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(max_digits=12, decimal_places=2)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    total_price = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.description} x {self.quantity}"

class UsageMetric(TimeStampedModel):
    """
    Tracks actual consumption for billing.
    """
    METRIC_TYPES = [
        ('student_onboarding', 'Student Onboarding'),
        ('active_student', 'Monthly Active Student'),
        ('exam_attempt', 'Exam Attempt'),
        ('re_exam_attempt', 'Re-exam Attempt'),
        ('proctoring_session', 'AI Proctoring Session'),
        ('storage_usage', 'Storage Usage'),
    ]
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='usage_metrics')
    metric_type = models.CharField(max_length=30, choices=METRIC_TYPES)
    quantity = models.DecimalField(max_digits=15, decimal_places=2, default=1.00)
    processed = models.BooleanField(default=False, help_text="Marked true once included in an invoice")
    reference_id = models.CharField(max_length=100, blank=True, null=True)

    def __str__(self):
        return f"{self.institute.name} - {self.metric_type}"
