from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from accounts.models import Institute


class Subject(models.Model):
    """Subject model for organizing exam sections"""
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='subjects', db_constraint=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']
        unique_together = ['name', 'institute']

    def __str__(self):
        return f"{self.name} ({self.institute.name})"


class ExamPattern(models.Model):
    """Exam pattern template that defines the structure of exams"""
    PATTERN_TYPE_CHOICES = [
        ('fixed', 'Fixed Questions'),
        ('template', 'Template Only'),
        ('hybrid', 'Hybrid (Mixed)'),
    ]
    
    # Exam Mode - Controls how the exam is conducted
    EXAM_MODE_CHOICES = [
        ('online', 'Online Exam'),
        ('offline_omr', 'Offline OMR-Based'),
        ('offline_subjective', 'Offline Subjective'),
    ]
    
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='exam_patterns', db_constraint=False)
    total_questions = models.IntegerField(validators=[MinValueValidator(1), MaxValueValidator(500)])
    total_duration = models.IntegerField(help_text="Duration in minutes", validators=[MinValueValidator(1)])
    total_marks = models.IntegerField(validators=[MinValueValidator(1)])
    pattern_type = models.CharField(max_length=20, choices=PATTERN_TYPE_CHOICES, default='fixed')
    exam_mode = models.CharField(
        max_length=20,
        choices=EXAM_MODE_CHOICES,
        default='online',
        help_text="Default exam mode for exams using this pattern"
    )
    omr_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="Default OMR configuration (candidate fields, etc.)"
    )
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False, help_text="Designates if this is a system-default pattern")
    created_by = models.ForeignKey('accounts.User', on_delete=models.CASCADE, related_name='created_patterns', db_constraint=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['name', 'institute']

    def __str__(self):
        return f"{self.name} ({self.institute.name})"


class PatternSection(models.Model):
    """Sections within an exam pattern"""
    QUESTION_TYPE_CHOICES = [
        ('single_mcq', 'Single Correct MCQ'),
        ('multiple_mcq', 'Multiple Correct MCQ'),
        ('numerical', 'Numerical Questions'),
        ('subjective', 'Subjective Questions'),
        ('true_false', 'True/False Questions'),
        ('fill_blank', 'Fill in the Blanks'),
    ]
    
    SECTION_TYPE_CHOICES = [
        ('fixed', 'Fixed Questions'),
        ('template', 'Template Selection'),
        ('random', 'Random Selection'),
    ]

    pattern = models.ForeignKey(ExamPattern, on_delete=models.CASCADE, related_name='sections')
    name = models.CharField(max_length=100)
    subject = models.CharField(max_length=100)  # Keep as CharField for now
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES)
    start_question = models.IntegerField(validators=[MinValueValidator(1)])
    end_question = models.IntegerField(validators=[MinValueValidator(1)])
    marks_per_question = models.IntegerField(validators=[MinValueValidator(1)])
    negative_marking = models.DecimalField(
        max_digits=4,
        decimal_places=2,
        default=1.00,
        help_text="Fixed negative marks deducted per incorrect answer (e.g., 1.00)"
    )
    min_questions_to_attempt = models.IntegerField(default=5, help_text="Minimum questions student must attempt")
    is_compulsory = models.BooleanField(default=True)
    order = models.IntegerField(default=1)
    section_type = models.CharField(max_length=20, choices=SECTION_TYPE_CHOICES, default='fixed')
    fixed_questions = models.JSONField(default=list, blank=True, help_text='List of fixed question IDs for fixed sections')
    selection_criteria = models.JSONField(default=dict, blank=True, help_text='Criteria for question selection (difficulty, topic, etc.)')
    question_bank = models.ForeignKey('questions.QuestionBank', on_delete=models.SET_NULL, null=True, blank=True, related_name='pattern_sections')
    question_configurations = models.JSONField(default=dict, blank=True, help_text='Per-question configuration overrides (e.g. nested structure for subjective questions)')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['subject', 'order', 'start_question']
        unique_together = ['pattern', 'subject', 'start_question', 'end_question']

    def __str__(self):
        return f"{self.pattern.name} - {self.name} ({self.subject})"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.start_question >= self.end_question:
            raise ValidationError("Start question must be less than end question")
        
        total_questions = (self.end_question - self.start_question) + 1
        if total_questions <= 0:
            raise ValidationError("Section must contain at least one question")
        self.min_questions_to_attempt = total_questions
        
        # Subjective questions should have no negative marking
        if self.question_type == 'subjective' and self.negative_marking != 0:
            self.negative_marking = 0
        
        # Check for overlapping question ranges within the same pattern AND same subject
        # Different subjects can have overlapping question numbers (subject-wise numbering)
        overlapping = PatternSection.objects.filter(
            pattern=self.pattern,
            subject=self.subject  # Only check within the same subject
        ).exclude(pk=self.pk).filter(
            start_question__lte=self.end_question,
            end_question__gte=self.start_question
        )
        if overlapping.exists():
            raise ValidationError(f"Question ranges cannot overlap within the same subject ({self.subject})")

    def save(self, *args, **kwargs):
        self.clean()
        super().save(*args, **kwargs)

    @property
    def total_questions_in_section(self):
        return self.end_question - self.start_question + 1
    
    @property
    def total_questions(self):
        """Alias for total_questions_in_section for compatibility"""
        return self.total_questions_in_section

    @property
    def total_marks_in_section(self):
        return self.total_questions_in_section * self.marks_per_question


class PatternTemplate(models.Model):
    """Predefined pattern templates for common exam types"""
    name = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(max_length=100, help_text="e.g., Engineering, Medical, Competitive")
    total_questions = models.IntegerField()
    total_duration = models.IntegerField()
    total_marks = models.IntegerField()
    is_public = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'name']

    def __str__(self):
        return f"{self.name} ({self.category})"