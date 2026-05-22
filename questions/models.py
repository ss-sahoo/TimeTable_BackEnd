from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from accounts.models import Institute, User
from exams.models import Exam


class QuestionBank(models.Model):
    """Question bank for storing reusable questions"""
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='question_banks', db_constraint=False)
    is_public = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_question_banks', db_constraint=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = ['name', 'institute']

    def __str__(self):
        return f"{self.name} ({self.institute.name})"


class Question(models.Model):
    """Individual question model"""
    DIFFICULTY_CHOICES = [
        ('easy', 'Easy'),
        ('medium', 'Medium'),
        ('hard', 'Hard'),
    ]

    QUESTION_TYPE_CHOICES = [
        ('single_mcq', 'Single Correct MCQ'),
        ('multiple_mcq', 'Multiple Correct MCQ'),
        ('numerical', 'Numerical Questions'),
        ('subjective', 'Subjective Questions'),
        ('true_false', 'True/False Questions'),
        ('fill_blank', 'Fill in the Blanks'),
    ]

    # Basic info
    question_text = models.TextField()
    question_type = models.CharField(max_length=20, choices=QUESTION_TYPE_CHOICES)
    difficulty = models.CharField(max_length=10, choices=DIFFICULTY_CHOICES, default='medium')
    
    # Structure for nested questions (internal choices, sub-parts)
    structure = models.JSONField(default=dict, blank=True, help_text='Defines nested sub-questions or internal choices for complex questions')
    
    # Options for MCQ
    options = models.JSONField(default=list, blank=True)
    correct_answer = models.TextField(blank=True)
    
    # Solution and explanation
    solution = models.TextField(blank=True)
    explanation = models.TextField(blank=True)
    
    # Metadata
    marks = models.IntegerField(default=1, validators=[MinValueValidator(1)])
    negative_marks = models.DecimalField(max_digits=3, decimal_places=2, default=0.25)
    
    # Organization
    subject = models.CharField(max_length=100)
    topic = models.CharField(max_length=100, blank=True)
    subtopic = models.CharField(max_length=100, blank=True)
    tags = models.JSONField(default=list, blank=True)
    
    # IMPORTANT: Questions belong to EXAMS, not Patterns
    # Patterns are templates only - questions are linked via ExamQuestion model
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='questions', help_text='Exam this question belongs to', null=True, blank=True)
    
    # Pattern section reference (for structure/organization only, not a foreign key)
    pattern_section_id = models.IntegerField(null=True, blank=True, help_text='Reference to pattern section for organization')
    pattern_section_name = models.CharField(max_length=200, blank=True, help_text='Name of the pattern section')
    
    # Position within the exam (1-based)
    question_number = models.IntegerField(validators=[MinValueValidator(1)], help_text='Question number in the exam', null=True, blank=True)
    # Position within the pattern (subject-wise numbering)
    question_number_in_pattern = models.IntegerField(null=True, blank=True, help_text='Question number within the pattern/subject grouping')
    
    # References
    question_bank = models.ForeignKey(QuestionBank, on_delete=models.CASCADE, related_name='questions', null=True, blank=True)
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='questions', db_constraint=False)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_questions', db_constraint=False)
    
    # Status
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False)
    verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='verified_questions', db_constraint=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    
    # Usage tracking
    usage_count = models.IntegerField(default=0)
    success_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['question_number', 'created_at']
        unique_together = ['exam', 'pattern_section_id', 'question_number']

    def __str__(self):
        return f"Exam {self.exam_id} - Q{self.question_number}: {self.question_text[:50]}..."

    def normalize_correct_answer(self):
        """
        Convert index-based or label-based correct_answer (e.g., '1', 'A') 
        to the actual option text. This is critical for robust evaluation 
        when options are shuffled for different students.
        """
        if self.question_type not in ['single_mcq', 'multiple_mcq', 'true_false']:
            return

        if not self.options or not self.correct_answer:
            return

        def map_value_to_text(val, opts):
            # 1. Check if it's already an exact match
            val_str = str(val).strip()
            if val_str in opts:
                return val_str
            
            # 1b. Check case-insensitive match
            val_lower = val_str.lower()
            for opt in opts:
                if str(opt).strip().lower() == val_lower:
                    return str(opt).strip()

            # 2. Check if it's a numeric index (1-based)
            if val_str.isdigit():
                idx = int(val_str) - 1
                if 0 <= idx < len(opts):
                    return str(opts[idx])

            # 3. Check if it's a label (A, B, C...)
            if len(val_str) == 1 and val_str.isalpha():
                idx = ord(val_str.upper()) - 65
                if 0 <= idx < len(opts):
                    return str(opts[idx])
            
            return val_str

        if self.question_type == 'multiple_mcq':
            # Handle multiple answers (comma or pipe separated)
            if '|' in self.correct_answer:
                parts = [p.strip() for p in self.correct_answer.split('|') if p.strip()]
            else:
                parts = [p.strip() for p in self.correct_answer.split(',') if p.strip()]
            
            normalized_parts = [map_value_to_text(p, self.options) for p in parts]
            # Use pipe as standard internal separator
            self.correct_answer = '|'.join(filter(None, normalized_parts))
        else:
            # Single MCQ or True/False
            self.correct_answer = map_value_to_text(self.correct_answer, self.options)

    def save(self, *args, **kwargs):
        # Normalize correct_answer before saving
        self.normalize_correct_answer()
        super().save(*args, **kwargs)

    def increment_usage(self):
        self.usage_count += 1
        self.save(update_fields=['usage_count'])


class ExamQuestion(models.Model):
    """Questions assigned to specific exams"""
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='exam_questions')
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='exam_assignments')
    question_number = models.IntegerField(validators=[MinValueValidator(1)])
    section_name = models.CharField(max_length=100)
    marks = models.IntegerField(validators=[MinValueValidator(1)])
    negative_marks = models.DecimalField(max_digits=3, decimal_places=2, default=0.25)
    order = models.IntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ['exam', 'question_number', 'section_name']
        ordering = ['section_name', 'question_number']

    def __str__(self):
        return f"{self.exam.title} - Q{self.question_number}"

    def save(self, *args, **kwargs):
        # Update question usage count
        self.question.increment_usage()
        super().save(*args, **kwargs)


class QuestionImage(models.Model):
    """Images associated with questions"""
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='question_images/')
    caption = models.CharField(max_length=200, blank=True)
    order = models.IntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Image for Q{self.question.id}"


class QuestionComment(models.Model):
    """Comments and reviews on questions"""
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name='comments')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='question_comments', db_constraint=False)
    comment = models.TextField()
    is_review = models.BooleanField(default=False)
    rating = models.IntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)],
        null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Comment by {self.user.email} on Q{self.question.id}"


class QuestionTemplate(models.Model):
    """Templates for common question types"""
    TEMPLATE_CATEGORIES = [
        ('academic', 'Academic'),
        ('competitive', 'Competitive Exams'),
        ('language', 'Language Learning'),
        ('technical', 'Technical'),
        ('mathematics', 'Mathematics'),
        ('science', 'Science'),
        ('general', 'General Knowledge'),
    ]
    
    name = models.CharField(max_length=200)
    description = models.TextField()
    category = models.CharField(max_length=20, choices=TEMPLATE_CATEGORIES, default='general')
    question_type = models.CharField(max_length=20, choices=Question.QUESTION_TYPE_CHOICES)
    difficulty = models.CharField(max_length=10, choices=Question.DIFFICULTY_CHOICES, default='medium')
    subject = models.CharField(max_length=100, blank=True)
    topic = models.CharField(max_length=100, blank=True)
    template_data = models.JSONField(default=dict)
    example_question = models.TextField(blank=True)
    tags = models.JSONField(default=list, blank=True)
    usage_count = models.PositiveIntegerField(default=0)
    is_public = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='question_templates', db_constraint=False)
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='question_templates', null=True, blank=True, db_constraint=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-is_featured', '-usage_count', 'name']

    def __str__(self):
        return f"{self.name} ({self.question_type})"
    
    def increment_usage(self):
        self.usage_count += 1
        self.save(update_fields=['usage_count'])


# AI & Vector Search Models
import os
from django.db import models

_ENABLE_PGVECTOR = os.getenv("ENABLE_PGVECTOR", "").lower() in ("1", "true", "yes")

try:
    if not _ENABLE_PGVECTOR:
        raise ModuleNotFoundError("pgvector disabled via env")
    from pgvector.django import VectorField as _PgVectorField  # type: ignore
except ModuleNotFoundError:
    class VectorField(models.JSONField):  # type: ignore
        """
        JSON fallback when pgvector extension isn't available.
        Stores embeddings as lists so core flows keep working.
        """
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("default", list)
            kwargs.pop("dimensions", None)
            super().__init__(*args, **kwargs)
else:
    class VectorField(_PgVectorField):  # type: ignore
        pass


class QuestionEmbedding(models.Model):
    """Store vector embeddings for questions"""
    question = models.OneToOneField(
        Question, 
        on_delete=models.CASCADE, 
        related_name='embedding',
        primary_key=True
    )
    # OpenAI ada-002 produces 1536-dimensional vectors
    text_embedding = VectorField(dimensions=1536)
    # Combined embedding (question + options + solution)
    combined_embedding = VectorField(dimensions=1536, null=True, blank=True)
    
    embedding_model = models.CharField(max_length=100, default='text-embedding-ada-002')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'question_embeddings'
        indexes = [
            models.Index(fields=['created_at']),
        ]
    
    def __str__(self):
        return f"Embedding for Q{self.question.id}"


class ChatHistory(models.Model):
    """Store chat conversations for context"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_history', db_constraint=False)
    session_id = models.CharField(max_length=100, db_index=True)
    role = models.CharField(max_length=20, choices=[
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('system', 'System')
    ])
    content = models.TextField()
    metadata = models.JSONField(default=dict, blank=True)  # Store sources, timestamps, etc.
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['user', 'session_id', 'created_at']),
        ]
    
    def __str__(self):
        return f"{self.role}: {self.content[:50]}..."



# ===========================
# AI Question Extraction Models
# ===========================

import uuid
from django.utils import timezone


class OCRResult(models.Model):
    """
    Store OCR extraction results for reuse.
    Caches Mathpix API results to avoid redundant API calls.
    """
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Source file identification
    file_path = models.CharField(
        max_length=500,
        help_text='Path to the source file'
    )
    file_hash = models.CharField(
        max_length=64,
        db_index=True,
        help_text='SHA256 hash for file deduplication'
    )
    file_size = models.IntegerField(
        help_text='File size in bytes'
    )
    file_name = models.CharField(
        max_length=255,
        blank=True,
        help_text='Original filename'
    )
    
    # OCR Provider info (Mathpix specific)
    ocr_provider = models.CharField(
        max_length=50,
        default='mathpix',
        help_text='OCR service used (mathpix, tesseract, etc.)'
    )
    mathpix_pdf_id = models.CharField(
        max_length=100,
        blank=True,
        help_text='Mathpix PDF processing ID'
    )
    
    # Extracted content
    extracted_text = models.TextField(
        blank=True,
        help_text='Full extracted text content'
    )
    page_count = models.IntegerField(
        default=0,
        help_text='Number of pages processed'
    )
    
    # Processing metadata
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True
    )
    error_message = models.TextField(
        blank=True,
        help_text='Error message if extraction failed'
    )
    processing_time_seconds = models.FloatField(
        null=True,
        blank=True,
        help_text='Time taken to process the file'
    )
    
    # Usage tracking
    usage_count = models.IntegerField(
        default=0,
        help_text='Number of times this result was reused'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_accessed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'OCR Result'
        verbose_name_plural = 'OCR Results'
        indexes = [
            models.Index(fields=['file_hash', 'status']),
            models.Index(fields=['ocr_provider', 'status']),
        ]
    
    def __str__(self):
        return f"OCR {self.id} - {self.file_name or 'Unknown'} ({self.status})"
    
    def mark_processing(self, mathpix_pdf_id=None):
        """Mark as processing with optional Mathpix PDF ID"""
        self.status = 'processing'
        if mathpix_pdf_id:
            self.mathpix_pdf_id = mathpix_pdf_id
        self.save(update_fields=['status', 'mathpix_pdf_id'])
    
    def mark_completed(self, extracted_text, page_count=0, processing_time=None):
        """Mark as completed with extracted content"""
        self.status = 'completed'
        self.extracted_text = extracted_text
        self.page_count = page_count
        self.processing_time_seconds = processing_time
        self.completed_at = timezone.now()
        self.save()
    
    def mark_failed(self, error_message):
        """Mark as failed with error message"""
        self.status = 'failed'
        self.error_message = error_message
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'error_message', 'completed_at'])
    
    def record_access(self):
        """Record that this cached result was accessed"""
        self.usage_count += 1
        self.last_accessed_at = timezone.now()
        self.save(update_fields=['usage_count', 'last_accessed_at'])
    
    @classmethod
    def get_cached_result(cls, file_hash):
        """Get cached OCR result by file hash if available"""
        return cls.objects.filter(
            file_hash=file_hash,
            status='completed'
        ).first()


class ExtractionJob(models.Model):
    """Track question extraction jobs from uploaded files"""
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('partial', 'Partially Completed'),
    ]
    
    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationships
    exam = models.ForeignKey(
        Exam, 
        on_delete=models.CASCADE, 
        related_name='extraction_jobs',
        help_text='Exam to import questions into'
    )
    pattern = models.ForeignKey(
        'patterns.ExamPattern', 
        on_delete=models.CASCADE, 
        related_name='extraction_jobs',
        help_text='Pattern structure for organizing questions'
    )
    created_by = models.ForeignKey(
        User, 
        on_delete=models.CASCADE, 
        related_name='extraction_jobs',
        help_text='User who uploaded the file',
        db_constraint=False
    )
    
    # Link to pre-analysis job (for subject-separated content)
    pre_analysis_job = models.ForeignKey(
        'PreAnalysisJob',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='extraction_jobs_from_pre',
        help_text='Pre-analysis job that triggered this extraction (contains subject-separated content)'
    )
    
    # Link to cached OCR result
    ocr_result = models.ForeignKey(
        OCRResult,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='extraction_jobs',
        help_text='Cached OCR extraction result'
    )
    
    # File information
    file_name = models.CharField(max_length=255, help_text='Original filename')
    file_type = models.CharField(max_length=100, help_text='MIME type of uploaded file')
    file_size = models.IntegerField(help_text='File size in bytes')
    file_path = models.CharField(max_length=500, help_text='Path to uploaded file')
    
    # Status tracking
    status = models.CharField(
        max_length=20, 
        choices=STATUS_CHOICES, 
        default='pending',
        db_index=True
    )
    progress_percent = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
        help_text='Extraction progress percentage'
    )
    
    # Results metrics
    total_questions_found = models.IntegerField(
        default=0,
        help_text='Total questions detected in file'
    )
    questions_extracted = models.IntegerField(
        default=0,
        help_text='Questions successfully extracted'
    )
    questions_imported = models.IntegerField(
        default=0,
        help_text='Questions successfully imported to exam'
    )
    questions_failed = models.IntegerField(
        default=0,
        help_text='Questions that failed extraction or import'
    )
    
    # AI metadata
    ai_model_used = models.CharField(
        max_length=100,
        default='gemini-2.5-flash',
        help_text='AI model used for extraction'
    )
    tokens_used = models.IntegerField(
        default=0,
        help_text='Total tokens consumed by AI API'
    )
    processing_time_seconds = models.FloatField(
        null=True,
        blank=True,
        help_text='Total processing time in seconds'
    )
    
    # Error handling
    error_message = models.TextField(
        blank=True,
        help_text='Error message if extraction failed'
    )
    retry_count = models.IntegerField(
        default=0,
        help_text='Number of retry attempts'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['exam', 'status']),
            models.Index(fields=['created_by', 'created_at']),
        ]
    
    def __str__(self):
        return f"Extraction Job {self.id} - {self.file_name} ({self.status})"
    
    def update_progress(self, percent, save=True):
        """Update progress percentage"""
        self.progress_percent = min(100, max(0, percent))
        if save:
            self.save(update_fields=['progress_percent'])
    
    def mark_completed(self):
        """Mark job as completed"""
        self.status = 'completed'
        self.progress_percent = 100
        self.completed_at = timezone.now()
        self.save(update_fields=[
            'status', 'progress_percent', 'completed_at',
            'questions_extracted', 'processing_time_seconds'
        ])
    
    def mark_failed(self, error_message):
        """Mark job as failed with error message"""
        self.status = 'failed'
        self.error_message = error_message
        self.completed_at = timezone.now()
        self.save(update_fields=[
            'status', 'error_message', 'completed_at',
            'questions_extracted', 'processing_time_seconds'
        ])
    
    def mark_partial(self):
        """Mark job as partially completed"""
        self.status = 'partial'
        self.completed_at = timezone.now()
        self.save(update_fields=[
            'status', 'completed_at',
            'questions_extracted', 'processing_time_seconds'
        ])
    
    @property
    def success_rate(self):
        """Calculate success rate of extraction"""
        if self.questions_extracted == 0:
            return 0
        return (self.questions_imported / self.questions_extracted) * 100


class ExtractedQuestion(models.Model):
    """Temporary storage for extracted questions before import"""
    
    # Relationship to extraction job
    job = models.ForeignKey(
        ExtractionJob,
        on_delete=models.CASCADE,
        related_name='extracted_questions',
        help_text='Extraction job this question belongs to'
    )
    
    # Extracted question data
    question_text = models.TextField(null=True, blank=True, help_text='Question text extracted from file')
    question_type = models.CharField(
        max_length=20,
        choices=Question.QUESTION_TYPE_CHOICES,
        null=True,
        blank=True,
        help_text='Type of question'
    )
    options = models.JSONField(
        default=list,
        blank=True,
        null=True,
        help_text='Answer options for MCQ questions'
    )
    structure = models.JSONField(
        default=dict,
        blank=True,
        null=True,
        help_text='Defines nested sub-questions or internal choices for complex questions'
    )
    correct_answer = models.TextField(null=True, blank=True, help_text='Correct answer or solution')
    solution = models.TextField(
        blank=True,
        null=True,
        help_text='Detailed solution explanation'
    )
    explanation = models.TextField(
        blank=True,
        help_text='Additional explanation or hints'
    )
    images_data = models.JSONField(
        default=dict,
        blank=True,
        help_text='Internal mapping of image IDs to temporary storage paths'
    )
    difficulty = models.CharField(
        max_length=10,
        choices=Question.DIFFICULTY_CHOICES,
        default='medium',
        help_text='Question difficulty level'
    )
    
    # AI metadata
    confidence_score = models.FloatField(
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        null=True,
        blank=True,
        help_text='AI confidence score (0.0 to 1.0)'
    )
    requires_review = models.BooleanField(
        default=False,
        help_text='Whether question needs manual review'
    )
    
    # Subject and section mapping
    suggested_subject = models.CharField(
        max_length=100,
        blank=True,
        help_text='AI-suggested subject for this question'
    )
    suggested_section_id = models.IntegerField(
        null=True,
        blank=True,
        help_text='AI-suggested pattern section ID'
    )
    detection_reasoning = models.TextField(
        blank=True,
        null=True,
        default='',
        help_text='AI reasoning for subject detection'
    )
    assigned_subject = models.CharField(
        max_length=100,
        blank=True,
        help_text='User-assigned subject'
    )
    assigned_section_id = models.IntegerField(
        null=True,
        blank=True,
        help_text='User-assigned pattern section ID'
    )
    
    # Import status
    is_validated = models.BooleanField(
        default=False,
        help_text='Whether question passed validation'
    )
    is_imported = models.BooleanField(
        default=False,
        db_index=True,
        help_text='Whether question was imported to exam'
    )
    import_error = models.TextField(
        blank=True,
        help_text='Error message if import failed'
    )
    
    # Reference to imported question
    imported_question = models.ForeignKey(
        Question,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='extraction_source',
        help_text='Reference to the imported Question record'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['job', 'id']
        indexes = [
            models.Index(fields=['job', 'is_imported']),
            models.Index(fields=['job', 'requires_review']),
        ]
    
    def __str__(self):
        return f"Extracted Q{self.id} - {self.question_text[:50]}... ({self.confidence_score:.2f})"
    
    def validate(self):
        """Validate extracted question data"""
        errors = []
        
        # Check required fields
        is_nested = self.structure and self.structure.get('nested_parts')
        if not self.question_text or not self.question_text.strip():
            if not is_nested:
                errors.append("Question text is required")
        
        if not self.correct_answer or not self.correct_answer.strip():
            # Allow empty correct answer for nested questions (handled per-part)
            if not is_nested:
                errors.append("Correct answer is required")
        
        # Validate MCQ questions
        if self.question_type in ['single_mcq', 'multiple_mcq']:
            if not self.options or len(self.options) < 2:
                errors.append("MCQ questions must have at least 2 options")
            
            if self.question_type == 'single_mcq':
                if self.correct_answer not in self.options:
                    errors.append("Correct answer must be one of the options")
        
        # Validate numerical questions
        if self.question_type == 'numerical':
            try:
                float(self.correct_answer)
            except (ValueError, TypeError):
                errors.append("Numerical questions must have a numeric answer")
        
        self.is_validated = len(errors) == 0
        
        if errors:
            self.import_error = "; ".join(errors)
            self.requires_review = True
        
        return self.is_validated, errors
    
    def mark_imported(self, question):
        """Mark as successfully imported"""
        self.is_imported = True
        self.imported_question = question
        self.import_error = ''
        self.save(update_fields=['is_imported', 'imported_question', 'import_error'])
    
    def mark_failed(self, error_message):
        """Mark import as failed"""
        self.is_imported = False
        self.import_error = error_message
        self.requires_review = True
        self.save(update_fields=['is_imported', 'import_error', 'requires_review'])


class PreAnalysisJob(models.Model):
    """Track document pre-analysis jobs before extraction"""
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    DOCUMENT_TYPE_CHOICES = [
        ('questions_with_answers', 'Questions with Answers'),
        ('questions_only', 'Questions Only'),
        ('other', 'Other'),
    ]
    
    # Primary key
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationships
    pattern = models.ForeignKey(
        'patterns.ExamPattern',
        on_delete=models.CASCADE,
        related_name='pre_analysis_jobs',
        help_text='Pattern to match subjects against'
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='pre_analysis_jobs',
        help_text='User who uploaded the file'
    )
    
    # File information
    file_name = models.CharField(max_length=255, help_text='Original filename')
    file_type = models.CharField(max_length=100, help_text='MIME type of uploaded file')
    file_size = models.IntegerField(help_text='File size in bytes')
    file_path = models.CharField(max_length=500, help_text='Path to uploaded file')
    
    # Status tracking
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True
    )
    
    # Document type detection result
    document_type = models.CharField(
        max_length=30,
        choices=DOCUMENT_TYPE_CHOICES,
        blank=True,
        help_text='Detected document type'
    )
    document_type_confidence = models.FloatField(
        default=0.0,
        validators=[MinValueValidator(0.0), MaxValueValidator(1.0)],
        help_text='Confidence score for document type detection'
    )
    is_valid_document = models.BooleanField(
        default=False,
        help_text='Whether document is valid for question extraction'
    )
    
    # Subject detection results
    detected_subjects = models.JSONField(
        default=list,
        blank=True,
        help_text='List of subjects detected in document'
    )
    matched_subjects = models.JSONField(
        default=list,
        blank=True,
        help_text='Subjects matched against pattern'
    )
    unmatched_subjects = models.JSONField(
        default=list,
        blank=True,
        help_text='Detected subjects not in pattern'
    )
    subject_question_counts = models.JSONField(
        default=dict,
        blank=True,
        help_text='Estimated question count per subject'
    )
    
    # Subject-separated content
    subject_separated_content = models.JSONField(
        default=dict,
        blank=True,
        help_text='Document content separated by subject'
    )
    
    # Document structure (sections, types, format)
    document_structure = models.JSONField(
        default=dict,
        blank=True,
        help_text='Detected document structure with sections and question types'
    )
    
    # Per-subject section detection (cached for later use)
    detected_sections_per_subject = models.JSONField(
        default=dict,
        blank=True,
        help_text='Detected sections for each subject, cached to avoid re-detecting during extraction'
    )
    
    # Total estimates
    total_estimated_questions = models.IntegerField(
        default=0,
        help_text='Total estimated questions in document'
    )
    
    # Error handling
    error_message = models.TextField(
        blank=True,
        help_text='Error message if analysis failed'
    )
    analysis_reason = models.TextField(
        blank=True,
        help_text='Reason for document type classification'
    )
    
    # Reference to extraction job (created after confirmation)
    extraction_job = models.ForeignKey(
        ExtractionJob,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='pre_analysis',
        help_text='Extraction job created from this pre-analysis'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Pre-Analysis Job'
        verbose_name_plural = 'Pre-Analysis Jobs'
        indexes = [
            models.Index(fields=['pattern', 'status']),
            models.Index(fields=['created_by', 'created_at']),
        ]
    
    def __str__(self):
        return f"Pre-Analysis {self.id} - {self.file_name} ({self.status})"
    
    def mark_completed(self, result):
        """Mark job as completed with analysis result"""
        self.status = 'completed'
        self.document_type = result.document_type
        self.document_type_confidence = result.confidence
        self.is_valid_document = result.is_valid
        self.detected_subjects = result.detected_subjects
        self.matched_subjects = result.matched_subjects
        self.unmatched_subjects = result.unmatched_subjects
        self.subject_question_counts = result.subject_question_counts
        self.subject_separated_content = result.subject_separated_content
        self.document_structure = result.document_structure or {}
        self.total_estimated_questions = result.total_estimated_questions
        self.error_message = result.error_message or ''
        self.analysis_reason = result.reason or ''
        self.completed_at = timezone.now()
        self.save()
    
    def mark_failed(self, error_message):
        """Mark job as failed with error message"""
        self.status = 'failed'
        self.error_message = error_message
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'error_message', 'completed_at'])
    
    def get_subject_content(self, subject):
        """Get separated content for a specific subject"""
        data = self.subject_separated_content.get(subject, {})
        # Handle both new format (dict) and old format (string) for backward compatibility
        if isinstance(data, dict):
            return data.get('content', '')
        else:
            return str(data) if data else ''
    
    def get_subject_instructions(self, subject):
        """Get instructions for a specific subject"""
        data = self.subject_separated_content.get(subject, {})
        # Handle both new format (dict) and old format (string) for backward compatibility
        if isinstance(data, dict):
            return data.get('instructions', '')
        else:
            return ''
    
    def get_all_subjects_preview(self, max_chars=500):
        """Get preview of content for all subjects"""
        previews = {}
        for subject, data in self.subject_separated_content.items():
            # Handle both new format (dict) and old format (string) for backward compatibility
            if isinstance(data, dict):
                content = data.get('content', '')
                instructions = data.get('instructions', '')
            else:
                content = str(data) if data else ''
                instructions = ''
            
            previews[subject] = {
                'subject': subject,
                'question_count': self.subject_question_counts.get(subject, 0),
                'content_preview': content[:max_chars] + '...' if len(content) > max_chars else content,
                'full_content_length': len(content),
                'has_instructions': bool(instructions),
                'instructions_preview': instructions[:200] + '...' if len(instructions) > 200 else instructions
            }
        return previews


class AnswerExtractionJob(models.Model):
    """Track answer-key extraction jobs that update correct_answer on existing exam questions."""

    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    exam = models.ForeignKey(
        Exam,
        on_delete=models.CASCADE,
        related_name='answer_extraction_jobs',
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='answer_extraction_jobs',
        db_constraint=False,
    )

    file_name = models.CharField(max_length=255)
    file_type = models.CharField(max_length=100)
    file_size = models.IntegerField()
    file_path = models.CharField(max_length=500)

    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True,
    )
    progress_percent = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0), MaxValueValidator(100)],
    )
    error_message = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['exam', 'status'])]

    def __str__(self):
        return f"AnswerExtractionJob {self.id} - {self.file_name} ({self.status})"

    def mark_completed(self):
        """Mark the job as completed."""
        self.status = 'completed'
        self.progress_percent = 100
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'progress_percent', 'completed_at'])

    def mark_failed(self, error_message):
        """Mark the job as failed with the given message."""
        self.status = 'failed'
        self.error_message = error_message
        self.completed_at = timezone.now()
        self.save(update_fields=['status', 'error_message', 'completed_at'])


class ExtractedAnswer(models.Model):
    """One (question_number, answer) row staged from an answer-key PDF for review."""

    MATCH_STATUS_CHOICES = [
        ('matched', 'Matched'),
        ('unmatched', 'Unmatched'),
    ]

    job = models.ForeignKey(
        AnswerExtractionJob,
        on_delete=models.CASCADE,
        related_name='extracted_answers',
    )
    question_number = models.IntegerField()
    extracted_answer = models.TextField()

    matched_question = models.ForeignKey(
        Question,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='extracted_answer_rows',
        db_constraint=False,
    )
    current_answer = models.TextField(
        blank=True,
        help_text='Snapshot of Question.correct_answer when the row was matched, for diff display.',
    )
    match_status = models.CharField(
        max_length=20,
        choices=MATCH_STATUS_CHOICES,
        db_index=True,
    )

    skip = models.BooleanField(default=False)
    is_applied = models.BooleanField(default=False, db_index=True)
    apply_error = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['question_number']
        unique_together = [('job', 'question_number')]

    def __str__(self):
        return f"Q{self.question_number} -> {self.extracted_answer[:30]} ({self.match_status})"
