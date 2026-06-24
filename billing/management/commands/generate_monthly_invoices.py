from django.core.management.base import BaseCommand
from django.utils import timezone
from billing.models import UsageMetric, InstitutePricing, GlobalPricing, BillingInvoice, InvoiceLineItem
from accounts.models import Institute
from decimal import Decimal
from django.db import transaction

class Command(BaseCommand):
    help = 'Automatically generates monthly invoices for all institutes based on unbilled usage.'

    def handle(self, *args, **options):
        self.stdout.write("Starting automated monthly invoice generation...")
        
        institutes = Institute.objects.all()
        now = timezone.now()
        count = 0

        for institute in institutes:
            # 1. Get unprocessed metrics
            metrics = UsageMetric.objects.filter(institute=institute, processed=False)
            if not metrics.exists():
                continue

            # 2. Get pricing
            try:
                pricing = InstitutePricing.objects.get(institute=institute)
            except InstitutePricing.DoesNotExist:
                pricing = GlobalPricing.get_instance()

            # 3. Aggregate
            usage_summary = {}
            for m in metrics:
                if m.metric_type not in usage_summary:
                    usage_summary[m.metric_type] = Decimal('0.00')
                usage_summary[m.metric_type] += Decimal(str(m.quantity))

            try:
                with transaction.atomic():
                    # Create Invoice
                    invoice = BillingInvoice.objects.create(
                        institute=institute,
                        invoice_number=f"INV-{now.strftime('%Y%m')}-{institute.id.hex[:4]}-{str(now.timestamp())[-4:]}",
                        billing_period_start=metrics.order_by('created_at').first().created_at.date(),
                        billing_period_end=now.date(),
                        due_date=(now + timezone.timedelta(days=15)).date(),
                    )

                    subtotal = Decimal('0.00')
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
                            InvoiceLineItem.objects.create(
                                invoice=invoice,
                                description=f"{label}\n({qty} units × ₹{rate}/unit)",
                                quantity=qty,
                                unit_price=rate,
                                total_price=total_item_price
                            )
                            subtotal += total_item_price

                    tax = subtotal * Decimal('0.18')
                    invoice.subtotal = subtotal
                    invoice.tax_amount = tax
                    invoice.total_amount = subtotal + tax
                    invoice.save()

                    # Mark as processed
                    metrics.update(processed=True)
                    count += 1
                    self.stdout.write(self.style.SUCCESS(f"Generated invoice {invoice.invoice_number} for {institute.name}"))

            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Failed to generate invoice for {institute.name}: {str(e)}"))

        self.stdout.write(self.style.SUCCESS(f"Finished! Generated {count} invoices."))
