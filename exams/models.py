from django.db import models
from django.core.validators import MinValueValidator, MaxValueValidator
from django.utils import timezone as dj_timezone
from accounts.models import Institute, User
from patterns.models import ExamPattern
import uuid
import ipaddress


class Exam(models.Model):
    """Main Exam model"""
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('published', 'Published'),
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ]

    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='exams', db_constraint=False)
    pattern = models.ForeignKey(ExamPattern, on_delete=models.CASCADE, related_name='exams')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='draft')
    
    # Timing
    start_date = models.DateTimeField()
    end_date = models.DateTimeField()
    duration_minutes = models.IntegerField(validators=[MinValueValidator(1)])
    is_flexible = models.BooleanField(default=False, help_text="If enabled, students can start the exam anytime between start_date and end_date")
    
    # Advanced Scheduling
    timezone = models.CharField(max_length=50, default='UTC')
    grace_period_minutes = models.IntegerField(default=0, help_text="Extra time after end_date")
    buffer_time_minutes = models.IntegerField(default=15, help_text="Time before exam starts when students can access")
    auto_start = models.BooleanField(default=True, help_text="Automatically start exam at start_date")
    auto_end = models.BooleanField(default=True, help_text="Automatically end exam at end_date")
    reschedule_allowed = models.BooleanField(default=False)
    max_reschedules = models.IntegerField(default=0, validators=[MinValueValidator(0)])
    reschedule_deadline = models.DateTimeField(null=True, blank=True, help_text="Last date when rescheduling is allowed")
    
    # Settings
    max_attempts = models.IntegerField(default=1, validators=[MinValueValidator(1)])
    allow_late_submission = models.BooleanField(default=False)
    late_submission_penalty = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    
    # Security settings
    require_fullscreen = models.BooleanField(default=True)
    disable_copy_paste = models.BooleanField(default=True)
    disable_right_click = models.BooleanField(default=True)
    enable_webcam_proctoring = models.BooleanField(default=False)
    allow_tab_switching = models.BooleanField(default=False)
    
    # Question Shuffling Settings
    shuffle_questions = models.BooleanField(default=False, help_text="Enable question shuffling for students")
    shuffle_within_sections = models.BooleanField(default=True, help_text="Shuffle questions within each section")
    shuffle_sections = models.BooleanField(default=False, help_text="Shuffle the order of sections")
    shuffle_subjects = models.BooleanField(default=False, help_text="Shuffle the order of subjects")
    shuffle_options = models.BooleanField(default=False, help_text="Shuffle MCQ options for each question")
    shuffle_seed_per_student = models.BooleanField(default=True, help_text="Use different shuffle order for each student")
    
    # Result settings
    show_result_after_exam_end = models.BooleanField(
        default=True, 
        help_text="If enabled, students can only see their results after the exam's end_date has passed"
    )
    
    # Access control
    is_public = models.BooleanField(default=True)
    public_access_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    public_token_expires_at = models.DateTimeField(null=True, blank=True)
    public_allowed_ip_ranges = models.JSONField(default=list, blank=True, help_text="List of allowed IPv4 / IPv6 addresses or CIDR ranges")
    public_allow_multiple_devices = models.BooleanField(default=True)
    public_link_created_at = models.DateTimeField(default=dj_timezone.now)
    public_link_last_used_at = models.DateTimeField(null=True, blank=True)
    public_link_usage_count = models.IntegerField(default=0)
    allowed_users = models.ManyToManyField(User, blank=True, related_name='allowed_exams', db_constraint=False)
    
    # Visibility Scope - controls who can access this exam
    VISIBILITY_SCOPE_CHOICES = [
        ('institute', 'Institute-Wide'),
        ('centers', 'Specific Centers'),
        ('batches', 'Specific Batches'),
        ('program', 'Specific Program'),
    ]
    visibility_scope = models.CharField(
        max_length=20,
        choices=VISIBILITY_SCOPE_CHOICES,
        default='institute',
        help_text="Controls which students can access this exam"
    )
    allowed_centers = models.ManyToManyField(
        'accounts.Center',
        blank=True,
        related_name='exams',
        help_text="Centers whose students can access this exam (when visibility_scope='centers')",
        db_constraint=False
    )
    allowed_batches = models.ManyToManyField(
        'accounts.Batch',
        blank=True,
        related_name='exams',
        help_text="Batches whose students can access this exam (when visibility_scope='batches')",
        db_constraint=False
    )
    
    # Exam Mode - Controls how the exam is conducted
    EXAM_MODE_CHOICES = [
        ('online', 'Online Exam'),
        ('offline_omr', 'Offline OMR-Based'),
        ('offline_subjective', 'Offline Subjective'),
        ('hybrid', 'Hybrid (Online + Offline)'),
    ]
    exam_mode = models.CharField(
        max_length=20,
        choices=EXAM_MODE_CHOICES,
        default='online',
        help_text="How the exam is conducted: online (in-app), offline_omr (OMR sheets), offline_subjective (handwritten answers), hybrid (combination)"
    )
    
    # Exam Scope - Controls how questions are sourced
    EXAM_SCOPE_CHOICES = [
        ('exam_specific', 'Exam-Specific Questions'),
        ('program_specific', 'Program-Specific Questions'),
    ]
    exam_scope = models.CharField(
        max_length=20,
        choices=EXAM_SCOPE_CHOICES,
        default='exam_specific',
        help_text="Whether exam uses its own questions or program-level questions"
    )
    
    # Program reference (for program-specific exams)
    program = models.ForeignKey(
        'accounts.Program',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='exams',
        help_text="Program this exam belongs to (for program-specific exams)",
        db_constraint=False
    )
    
    # Center reference
    center = models.ForeignKey(
        'accounts.Center',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='owned_exams',
        help_text="Center this exam belongs to",
        db_constraint=False
    )
    
    # OMR Configuration (for offline_omr mode)
    omr_config = models.JSONField(
        default=dict,
        blank=True,
        help_text="OMR sheet configuration (candidate fields, question types, etc.)"
    )
    omr_metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text="Generated OMR layout metadata for evaluation"
    )
    omr_sheet_generated = models.BooleanField(
        default=False,
        help_text="Whether OMR sheet has been generated for this exam"
    )
    omr_sheet_file = models.FileField(
        upload_to='omr_sheets/',
        blank=True,
        null=True,
        help_text="Generated OMR sheet PDF file"
    )
    
    # Subjective Configuration (for offline_subjective mode)
    ai_evaluation_enabled = models.BooleanField(
        default=False,
        help_text="Enable AI-powered evaluation for subjective answers"
    )
    MARKING_STRICTNESS_CHOICES = [
        ('lenient', 'Lenient'),
        ('moderate', 'Moderate'),
        ('strict', 'Strict'),
    ]
    marking_strictness = models.CharField(
        max_length=20,
        choices=MARKING_STRICTNESS_CHOICES,
        default='moderate',
        help_text="How strict the AI grading should be"
    )
    
    # Metadata
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_exams', db_constraint=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    
    @property
    def total_questions(self):
        """Calculate total questions from pattern sections"""
        if not self.pattern:
            return 0
        total = 0
        for section in self.pattern.sections.all():
            total += section.end_question - section.start_question + 1
        return total
    
    @property
    def total_marks(self):
        """Calculate total marks from pattern sections"""
        if not self.pattern:
            return 0
        total = 0
        for section in self.pattern.sections.all():
            questions_count = section.end_question - section.start_question + 1
            total += section.marks_per_question * questions_count
        return total

    @property
    def questions_added(self):
        return self.questions.filter(is_active=True).count()

    @property
    def questions_required(self):
        return self.total_questions

    @property
    def questions_remaining(self):
        required = self.questions_required
        added = self.questions_added
        remaining = required - added
        return remaining if remaining > 0 else 0

    @property
    def question_completion_percent(self):
        required = self.questions_required
        if required <= 0:
            return 0
        added = self.questions_added
        effective_added = min(added, required)
        return round((effective_added / required) * 100, 2)

    @property
    def is_question_complete(self):
        required = self.questions_required
        if required <= 0:
            return False
        return self.questions_added >= required

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.institute.name})"

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.start_date >= self.end_date:
            raise ValidationError("Start date must be before end date")
        
        if self.duration_minutes > self.pattern.total_duration:
            raise ValidationError("Exam duration cannot exceed pattern duration")

    def save(self, *args, **kwargs):
        self.clean()
        if not self.public_link_created_at:
            self.public_link_created_at = dj_timezone.now()
        # Auto-enable AI evaluation for offline_subjective exams
        if self.exam_mode == 'offline_subjective' and not self.ai_evaluation_enabled:
            self.ai_evaluation_enabled = True
        super().save(*args, **kwargs)

    def is_active(self):
        import pytz
        now = dj_timezone.now()
        
        # Convert to exam timezone for comparison
        exam_tz = pytz.timezone(self.timezone)
        now_in_exam_tz = now.astimezone(exam_tz)
        start_in_exam_tz = self.start_date.astimezone(exam_tz)
        end_in_exam_tz = self.end_date.astimezone(exam_tz)
        
        return self.status == 'active' and start_in_exam_tz <= now_in_exam_tz <= end_in_exam_tz

    def is_accessible(self):
        """Check if exam is accessible (within buffer time)"""
        import pytz
        from datetime import timedelta
        
        now = dj_timezone.now()
        exam_tz = pytz.timezone(self.timezone)
        now_in_exam_tz = now.astimezone(exam_tz)
        start_in_exam_tz = self.start_date.astimezone(exam_tz)
        buffer_start = start_in_exam_tz - timedelta(minutes=self.buffer_time_minutes)
        
        return self.status in ['published', 'active'] and buffer_start <= now_in_exam_tz

    def is_available_for_reschedule(self):
        """Check if exam can be rescheduled"""
        import pytz
        
        if not self.reschedule_allowed:
            return False
            
        if self.reschedule_deadline:
            now = dj_timezone.now()
            exam_tz = pytz.timezone(self.timezone)
            now_in_exam_tz = now.astimezone(exam_tz)
            deadline_in_exam_tz = self.reschedule_deadline.astimezone(exam_tz)
            return now_in_exam_tz <= deadline_in_exam_tz
            
        return True

    def get_timezone_aware_dates(self):
        """Get start and end dates in exam timezone"""
        import pytz
        exam_tz = pytz.timezone(self.timezone)
        return {
            'start_date': self.start_date.astimezone(exam_tz),
            'end_date': self.end_date.astimezone(exam_tz),
            'timezone': self.timezone,
            'timezone_name': exam_tz.zone
        }

    def get_remaining_time(self):
        """Get remaining time until exam starts/ends"""
        import pytz
        from datetime import timedelta
        
        now = dj_timezone.now()
        exam_tz = pytz.timezone(self.timezone)
        now_in_exam_tz = now.astimezone(exam_tz)
        start_in_exam_tz = self.start_date.astimezone(exam_tz)
        end_in_exam_tz = self.end_date.astimezone(exam_tz)
        
        if now_in_exam_tz < start_in_exam_tz:
            # Exam hasn't started
            time_diff = start_in_exam_tz - now_in_exam_tz
            return {
                'status': 'upcoming',
                'time_remaining': time_diff,
                'message': f'Exam starts in {time_diff}'
            }
        elif now_in_exam_tz <= end_in_exam_tz:
            # Exam is active
            time_diff = end_in_exam_tz - now_in_exam_tz
            return {
                'status': 'active',
                'time_remaining': time_diff,
                'message': f'Exam ends in {time_diff}'
            }
        else:
            # Exam has ended
            return {
                'status': 'ended',
                'time_remaining': timedelta(0),
                'message': 'Exam has ended'
            }

    def regenerate_public_token(self):
        self.public_access_token = uuid.uuid4()
        self.public_link_created_at = dj_timezone.now()
        self.public_link_last_used_at = None
        self.public_link_usage_count = 0
        self.save(update_fields=[
            'public_access_token',
            'public_link_created_at',
            'public_link_last_used_at',
            'public_link_usage_count'
        ])

    def is_public_link_expired(self):
        if not self.public_token_expires_at:
            return False
        return dj_timezone.now() > self.public_token_expires_at

    def is_ip_allowed(self, ip_address):
        if not ip_address:
            return True
        if not self.public_allowed_ip_ranges:
            return True

        try:
            ip_obj = ipaddress.ip_address(ip_address)
        except ValueError:
            # If the IP cannot be parsed, deny access for safety
            return False

        for pattern in self.public_allowed_ip_ranges:
            if not pattern:
                continue
            pattern = pattern.strip()
            try:
                if '/' in pattern:
                    network = ipaddress.ip_network(pattern, strict=False)
                    if ip_obj in network:
                        return True
                else:
                    if ip_obj == ipaddress.ip_address(pattern):
                        return True
            except ValueError:
                # Skip invalid patterns silently
                continue
        return False

    def can_student_access(self, student):
        """
        Check if a student can access this exam based on visibility scope.
        
        Returns:
            bool: True if the student is allowed to access this exam
        """
        # If student is not actually a student role, deny access
        if not student or not student.is_student():
            return False
        
        # Check if student belongs to the same institute as the exam
        student_institute_id = getattr(student, 'institute_id', None) or getattr(student.institute, 'id', None) if hasattr(student, 'institute') else None
        if student_institute_id and str(student_institute_id) != str(self.institute_id):
            return False
        
        # Institute-wide: all students in the institute can access
        if self.visibility_scope == 'institute':
            return True
        
        # Center-specific: only students in the allowed centers can access
        if self.visibility_scope == 'centers':
            student_center = getattr(student, 'center', None)
            if student_center and self.allowed_centers.filter(id=student_center.id).exists():
                return True
            return False
        
        # Batch-specific: only students in the allowed batches can access
        if self.visibility_scope == 'batches':
            # Check if student is in any of the allowed batches
            student_batches = student.batches.all() if hasattr(student, 'batches') else []
            return self.allowed_batches.filter(id__in=[b.id for b in student_batches]).exists()
        
        # Program-specific: only students in the specified program can access
        if self.visibility_scope == 'program':
            if not self.program:
                return False
            # Check if student is in any batch belonging to this program
            student_batches = student.batches.all() if hasattr(student, 'batches') else []
            return any(batch.program_id == self.program_id for batch in student_batches)
            
        return False


class ExamReschedule(models.Model):
    """Track exam rescheduling requests"""
    RESCHEDULE_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]
    
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='reschedules')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='exam_reschedules')
    original_start_date = models.DateTimeField()
    original_end_date = models.DateTimeField()
    new_start_date = models.DateTimeField()
    new_end_date = models.DateTimeField()
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=RESCHEDULE_STATUS_CHOICES, default='pending')
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_reschedules', db_constraint=False)
    review_notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        ordering = ['-created_at']
        unique_together = ['exam', 'student', 'status']
    
    def __str__(self):
        return f"Reschedule request for {self.exam.title} by {self.student.email}"


class ExamAttempt(models.Model):
    """Student's attempt at an exam"""
    STATUS_CHOICES = [
        ('not_started', 'Not Started'),
        ('in_progress', 'In Progress'),
        ('submitted', 'Submitted'),
        ('auto_submitted', 'Auto Submitted'),
        ('disqualified', 'Disqualified'),
    ]

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='attempts')
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='exam_attempts', db_constraint=False)
    attempt_number = models.IntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='not_started')
    
    # Timing
    started_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    time_spent = models.IntegerField(default=0, help_text="Time spent in seconds")
    
    # Results
    score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    percentage = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    rank = models.IntegerField(null=True, blank=True)
    
    # Security
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    violations_count = models.IntegerField(default=0)
    
    # Geolocation
    geolocation_latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geolocation_longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    geolocation_captured_at = models.DateTimeField(null=True, blank=True)
    geolocation_permission_denied = models.BooleanField(default=False)
    
    # Answers (for auto-save during exam)
    answers = models.JSONField(default=dict, blank=True, help_text="Student answers during exam (auto-saved)")
    
    # Generated artifacts
    answer_sheet_pdf = models.FileField(upload_to='answer_sheets/', null=True, blank=True)
    answer_sheet_generated_at = models.DateTimeField(null=True, blank=True)
    
    # Proctoring settings
    proctoring_enabled = models.BooleanField(default=True)
    max_violations_allowed = models.IntegerField(default=5)
    fullscreen_required = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ['exam', 'student', 'attempt_number']
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.student.get_full_name()} - {self.exam.title} (Attempt {self.attempt_number})"

    @property
    def is_completed(self):
        return self.status in ['submitted', 'auto_submitted']

    @property
    def time_remaining(self):
        if not self.started_at:
            return None
        
        now = dj_timezone.now()
        elapsed = now - self.started_at
        
        # 1. Remaining time based on attempt duration
        remaining_by_duration = (self.exam.duration_minutes * 60) - elapsed.total_seconds()
        
        # 2. Remaining time based on absolute exam end date
        remaining_by_end_date = (self.exam.end_date - now).total_seconds()
        
        # Use the stricter (smaller) remaining time
        remaining_seconds = min(remaining_by_duration, remaining_by_end_date)
        
        return max(0, remaining_seconds)


class ExamResult(models.Model):
    """Detailed results for an exam attempt"""
    attempt = models.OneToOneField(ExamAttempt, on_delete=models.CASCADE, related_name='result')
    
    # Section-wise scores
    section_scores = models.JSONField(default=dict)
    
    # Statistics
    total_questions_attempted = models.IntegerField(default=0)
    total_correct_answers = models.IntegerField(default=0)
    total_wrong_answers = models.IntegerField(default=0)
    total_unattempted = models.IntegerField(default=0)
    
    # Detailed answers
    answers = models.JSONField(default=dict)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Result for {self.attempt}"


class ExamViolation(models.Model):
    """Track security violations during exam attempts"""
    VIOLATION_TYPES = [
        ('tab_switch', 'Tab Switch'),
        ('window_blur', 'Window Lost Focus'),
        ('multiple_faces', 'Multiple Faces Detected'),
        ('no_face', 'No Face Detected'),
        ('looking_away', 'Looking Away'),
        ('mobile_detected', 'Mobile Phone Detected'),
        ('copy_paste', 'Copy/Paste Attempt'),
        ('fullscreen_exit', 'Exited Fullscreen'),
        ('fullscreen_error', 'Fullscreen Error'),
        ('right_click', 'Right Click Attempt'),
        ('keyboard_shortcut', 'Keyboard Shortcut Attempt'),
    ]
    
    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name='violations')
    violation_type = models.CharField(max_length=20, choices=VIOLATION_TYPES)
    timestamp = models.DateTimeField(auto_now_add=True)
    screenshot = models.ImageField(upload_to='violations/', null=True, blank=True)
    metadata = models.JSONField(default=dict, help_text="Store detection confidence, etc.")
    
    class Meta:
        ordering = ['-timestamp']
    
    def __str__(self):
        return f"{self.attempt.student.get_full_name()} - {self.get_violation_type_display()}"


class ExamProctoring(models.Model):
    """Proctoring data for exam attempts"""
    attempt = models.OneToOneField(ExamAttempt, on_delete=models.CASCADE, related_name='proctoring')
    webcam_enabled = models.BooleanField(default=False)
    snapshots = models.JSONField(default=list, help_text="List of snapshot metadata with timestamps (Base64 images removed)")
    incidents = models.JSONField(default=list, help_text="Client-side proctoring incidents")
    face_verification_passed = models.BooleanField(default=False)
    total_violations = models.IntegerField(default=0)
    auto_disqualified = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Proctoring for {self.attempt}"


class ProctoringSnapshot(models.Model):
    """Separate model to store proctoring images as files in S3/Spaces"""
    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name='proctoring_snapshot_files')
    image = models.ImageField(upload_to='proctoring_snapshots/')
    timestamp = models.DateTimeField()
    metadata = models.JSONField(default=dict)
    analysis = models.JSONField(default=dict)
    has_violations = models.BooleanField(default=False)
    stored_reason = models.CharField(max_length=50, default='monitoring')
    
    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"Snapshot for {self.attempt} at {self.timestamp}"


class ExamInvitation(models.Model):
    """Invitations for specific users to take an exam"""
    INVITATION_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
        ('expired', 'Expired'),
    ]
    
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='invitations')
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='exam_invitations', db_constraint=False)
    student = models.ForeignKey(User, on_delete=models.CASCADE, related_name='student_invitations', null=True, blank=True, db_constraint=False)
    invited_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_invitations', db_constraint=False)
    status = models.CharField(max_length=20, choices=INVITATION_STATUS_CHOICES, default='pending')
    invited_at = models.DateTimeField(auto_now_add=True)
    is_accepted = models.BooleanField(default=False)
    accepted_at = models.DateTimeField(null=True, blank=True)
    declined_at = models.DateTimeField(null=True, blank=True)
    custom_message = models.TextField(blank=True)
    decline_reason = models.TextField(blank=True)
    invitation_token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    
    # Access control
    access_code = models.CharField(max_length=20, unique=True, null=True, blank=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    max_attempts = models.IntegerField(default=1)
    used_attempts = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ['exam', 'user']

    def __str__(self):
        return f"Invitation: {self.user.email} -> {self.exam.title}"
    
    def is_valid_now(self):
        """Check if invitation is valid at current time"""
        now = dj_timezone.now()
        
        if not self.is_active:
            return False
            
        if self.valid_from and now < self.valid_from:
            return False
            
        if self.valid_until and now > self.valid_until:
            return False
            
        return True
    
    def can_attempt(self):
        """Check if user can make another attempt"""
        return self.used_attempts < self.max_attempts


class ExamAnalytics(models.Model):
    """Analytics data for exams"""
    exam = models.OneToOneField(Exam, on_delete=models.CASCADE, related_name='analytics')
    
    # Participation stats
    total_invited = models.IntegerField(default=0)
    total_started = models.IntegerField(default=0)
    total_completed = models.IntegerField(default=0)
    completion_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    
    # Performance stats
    average_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    highest_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    lowest_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    
    # Time stats
    average_time_spent = models.IntegerField(default=0)
    
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Analytics for {self.exam.title}"


class QuestionAnalytics(models.Model):
    """Analytics for individual questions"""
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='question_analytics')
    question_number = models.IntegerField()
    question_text = models.TextField()
    
    # Statistics
    total_attempts = models.IntegerField(default=0)
    correct_attempts = models.IntegerField(default=0)
    wrong_attempts = models.IntegerField(default=0)
    unattempted = models.IntegerField(default=0)
    average_score = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    max_marks = models.DecimalField(max_digits=5, decimal_places=2, default=1.00)
    
    # Difficulty metrics
    difficulty_level = models.CharField(max_length=20, choices=[
        ('easy', 'Easy'),
        ('medium', 'Medium'),
        ('hard', 'Hard'),
    ], default='medium')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['exam', 'question_number']
        ordering = ['question_number']
    
    def __str__(self):
        return f"Q{self.question_number} - {self.exam.title}"
    
    @property
    def success_rate(self):
        if self.total_attempts == 0:
            return 0
        return (self.correct_attempts / self.total_attempts) * 100


# Evaluation System Models
class QuestionEvaluation(models.Model):
    """Individual question evaluation for an exam attempt"""
    
    EVALUATION_STATUS_CHOICES = [
        ('pending', 'Pending Evaluation'),
        ('auto_evaluated', 'Auto Evaluated'),
        ('manually_evaluated', 'Manually Evaluated'),
        ('ai_evaluated', 'AI Evaluated'),
        ('reviewed', 'Reviewed'),
    ]
    
    EVALUATION_TYPE_CHOICES = [
        ('auto', 'Automatic'),
        ('manual', 'Manual'),
        ('ai', 'AI-Powered'),
        ('mixed', 'Mixed'),
    ]
    
    attempt = models.ForeignKey(ExamAttempt, on_delete=models.CASCADE, related_name='question_evaluations')
    question = models.ForeignKey('questions.Question', on_delete=models.CASCADE)
    question_number = models.IntegerField()
    
    # Student's answer
    student_answer = models.TextField(blank=True)
    is_answered = models.BooleanField(default=False)
    
    # Evaluation details
    evaluation_type = models.CharField(max_length=10, choices=EVALUATION_TYPE_CHOICES, default='auto')
    evaluation_status = models.CharField(max_length=20, choices=EVALUATION_STATUS_CHOICES, default='pending')
    
    # Scoring
    marks_obtained = models.DecimalField(max_digits=5, decimal_places=2, default=0.00)
    max_marks = models.DecimalField(max_digits=5, decimal_places=2)
    is_correct = models.BooleanField(default=False)
    
    # Evaluation metadata
    evaluated_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='evaluated_questions', db_constraint=False)
    evaluated_at = models.DateTimeField(null=True, blank=True)
    evaluation_notes = models.TextField(blank=True)
    
    # AI evaluation specific
    ai_confidence_score = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True, 
                                           validators=[MinValueValidator(0), MaxValueValidator(1)])
    ai_feedback = models.TextField(blank=True)
    
    # Manual evaluation specific
    manual_feedback = models.TextField(blank=True)
    requires_review = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['attempt', 'question']
        ordering = ['question_number']
    
    def __str__(self):
        return f"Q{self.question_number} - {self.attempt.student.get_full_name()} - {self.evaluation_status}"


class EvaluationBatch(models.Model):
    """Batch evaluation for multiple questions"""
    
    BATCH_STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]
    
    BATCH_TYPE_CHOICES = [
        ('auto', 'Automatic'),
        ('manual', 'Manual'),
        ('ai', 'AI-Powered'),
    ]
    
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='evaluation_batches')
    batch_type = models.CharField(max_length=10, choices=BATCH_TYPE_CHOICES)
    status = models.CharField(max_length=20, choices=BATCH_STATUS_CHOICES, default='pending')
    
    # Batch details
    questions_count = models.IntegerField(default=0)
    evaluated_count = models.IntegerField(default=0)
    failed_count = models.IntegerField(default=0)
    
    # Processing details
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    processed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    
    # AI specific
    ai_model_used = models.CharField(max_length=100, blank=True)
    ai_processing_time = models.DurationField(null=True, blank=True)
    
    # Error handling
    error_message = models.TextField(blank=True)
    retry_count = models.IntegerField(default=0)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.batch_type.title()} Evaluation Batch - {self.exam.title}"


class EvaluationSettings(models.Model):
    """Settings for evaluation system"""
    
    exam = models.OneToOneField(Exam, on_delete=models.CASCADE, related_name='evaluation_settings')
    
    # Auto-evaluation settings
    enable_auto_evaluation = models.BooleanField(default=True)
    auto_evaluate_mcq = models.BooleanField(default=True)
    auto_evaluate_numerical = models.BooleanField(default=True)
    auto_evaluate_true_false = models.BooleanField(default=True)
    auto_evaluate_fill_blank = models.BooleanField(default=True)
    
    # Manual evaluation settings
    enable_manual_evaluation = models.BooleanField(default=True)
    require_manual_review = models.BooleanField(default=False)
    manual_evaluation_deadline = models.DateTimeField(null=True, blank=True)
    
    # AI evaluation settings
    enable_ai_evaluation = models.BooleanField(default=False)
    ai_model_preference = models.CharField(max_length=50, default='gpt-3.5-turbo')
    ai_confidence_threshold = models.DecimalField(max_digits=3, decimal_places=2, default=0.7,
                                                validators=[MinValueValidator(0), MaxValueValidator(1)])
    ai_fallback_to_manual = models.BooleanField(default=True)
    
    # Mixed evaluation settings
    enable_mixed_evaluation = models.BooleanField(default=False)
    auto_first_then_manual = models.BooleanField(default=True)
    ai_first_then_manual = models.BooleanField(default=False)
    
    # Notification settings
    notify_evaluators = models.BooleanField(default=True)
    notify_students_on_completion = models.BooleanField(default=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Evaluation Settings - {self.exam.title}"

    def save(self, *args, **kwargs):
        # Sync with Exam model
        if self.exam:
            self.exam.ai_evaluation_enabled = self.enable_ai_evaluation
            # Only update the specific field to avoid side effects
            self.exam.save(update_fields=['ai_evaluation_enabled'])
        super().save(*args, **kwargs)


class EvaluationProgress(models.Model):
    """Track evaluation progress for an exam"""
    
    exam = models.OneToOneField(Exam, on_delete=models.CASCADE, related_name='evaluation_progress')
    
    # Progress counts
    total_questions = models.IntegerField(default=0)
    auto_evaluated = models.IntegerField(default=0)
    manually_evaluated = models.IntegerField(default=0)
    ai_evaluated = models.IntegerField(default=0)
    pending_evaluation = models.IntegerField(default=0)
    
    # Completion status
    is_fully_evaluated = models.BooleanField(default=False)
    evaluation_completed_at = models.DateTimeField(null=True, blank=True)
    
    # Statistics
    average_auto_confidence = models.DecimalField(max_digits=3, decimal_places=2, null=True, blank=True)
    manual_evaluation_time = models.DurationField(null=True, blank=True)
    ai_evaluation_time = models.DurationField(null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Evaluation Progress - {self.exam.title}"
    
    @property
    def completion_percentage(self):
        if self.total_questions == 0:
            return 0
        evaluated = self.auto_evaluated + self.manually_evaluated + self.ai_evaluated
        return (evaluated / self.total_questions) * 100


class EvaluationRubric(models.Model):
    """Rubric for manual evaluation of subjective questions"""
    
    question = models.ForeignKey('questions.Question', on_delete=models.CASCADE, related_name='evaluation_rubrics')
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='evaluation_rubrics')
    
    # Rubric details
    rubric_name = models.CharField(max_length=200)
    description = models.TextField()
    
    # Scoring criteria
    max_marks = models.DecimalField(max_digits=5, decimal_places=2)
    criteria = models.JSONField(default=list)  # List of criteria with marks distribution
    
    # Usage
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='created_rubrics')
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Rubric for Q{self.question.id} - {self.rubric_name}"


class PublicExamAccessLog(models.Model):
    ACCESS_STATUS_CHOICES = [
        ('granted', 'Granted'),
        ('denied', 'Denied'),
    ]

    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='public_access_logs')
    access_token = models.UUIDField()
    status = models.CharField(max_length=20, choices=ACCESS_STATUS_CHOICES)
    reason = models.TextField(blank=True)
    student_email = models.CharField(max_length=255, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    accessed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-accessed_at']

    def __str__(self):
        return f"{self.exam.title} ({self.status}) @ {self.accessed_at}"