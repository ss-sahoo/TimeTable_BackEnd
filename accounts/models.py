from django.contrib.auth.models import AbstractUser
from django.db import models
from django.core.validators import RegexValidator
import uuid


class TimeStampedModel(models.Model):
    """
    Base abstract model for common timestamp fields.
    Makes all child models automatically track creation & update times.
    """
    id = models.UUIDField(
        primary_key=True,
        default=uuid.uuid4,
        editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Institute(models.Model):
    """
    Merged Institute model supporting both exam and timetable systems.
    """
    # Using BigAutoField to match database (was manually changed to bigint)
    # Note: UUID conversion would require data migration
    id = models.BigAutoField(
        primary_key=True,
    )
    
    # Basic fields from exam system
    name = models.CharField(max_length=255, unique=True)
    domain = models.CharField(max_length=100, unique=True, blank=True, null=True, help_text="Optional email domain (e.g., 'university.edu')")
    description = models.TextField(blank=True, null=True, help_text="Brief description of the institute")
    address = models.TextField(blank=True, null=True)
    contact_email = models.EmailField(blank=True, default='', null=True)
    contact_phone = models.CharField(max_length=20, blank=True, null=True)
    website = models.URLField(blank=True, null=True)
    logo = models.ImageField(upload_to='institute_logos/', blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_verified = models.BooleanField(default=False, help_text="Whether the institute is verified by super admin")
    created_by = models.ForeignKey('User', on_delete=models.SET_NULL, null=True, blank=True, related_name='created_institutes')
    
    # Timetable-specific fields
    head_office_location = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="City / address of the head office. Example: Delhi.",
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Multi-tenancy fields
    db_name = models.CharField(max_length=100, blank=True, null=True, help_text="Specific database name for this institute")
    db_host = models.CharField(max_length=255, blank=True, null=True, default='localhost')
    db_port = models.CharField(max_length=10, blank=True, null=True, default='5432')
    db_user = models.CharField(max_length=100, blank=True, null=True)
    db_password = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name
    
    def get_user_count(self):
        """Get the number of users in this institute"""
        return self.users.count()
    
    def get_active_user_count(self):
        """Get the number of active users in this institute"""
        return self.users.filter(is_active=True).count()
    
    def get_admins(self):
        """Get all admin users in this institute"""
        return self.users.filter(role__in=['institute_admin', 'super_admin', 'admin', 'ADMIN', 'SUPER_ADMIN'])
    
    def can_be_managed_by(self, user):
        """Check if a user can manage this institute"""
        if user.role in ['super_admin', 'SUPER_ADMIN']:
            return True
        return user.institute == self and user.role in ['institute_admin', 'admin', 'ADMIN']


class User(AbstractUser):
    """
    Merged User model supporting both exam and timetable systems.
    Combines fields from both systems with role compatibility.
    """
    # ===========
    # ROLE DEFINITIONS - All lowercase for consistency
    # ===========
    ROLE_PLATFORM_OWNER = 'platform_owner'
    ROLE_SUPER_ADMIN = 'super_admin'
    ROLE_INSTITUTE_ADMIN = 'institute_admin'
    ROLE_EXAM_ADMIN = 'exam_admin'
    ROLE_TEACHER = 'teacher'
    ROLE_STUDENT = 'student'
    ROLE_ADMIN = 'admin'  # Center admin (changed from 'ADMIN' to 'admin')
    ROLE_STAFF = 'staff'  # Staff (changed from 'STAFF' to 'staff')
    ROLE_MANAGER = 'manager'
    
    # Role choices - all lowercase
    ROLE_CHOICES = [
        ('platform_owner', 'Platform Owner'),
        ('super_admin', 'Super Admin'),
        ('institute_admin', 'Institute Admin'),
        ('exam_admin', 'Exam Admin'),
        ('teacher', 'Teacher'),
        ('student', 'Student'),
        ('admin', 'Center Admin'),
        ('staff', 'Staff'),
        ('manager', 'Manager'),
    ]
    
    # ===========
    # BASIC FIELDS
    # ===========
    email = models.EmailField(unique=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='student')
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='users', null=True, blank=True)
    
    # Phone number (supporting both field names)
    phone = models.CharField(
        max_length=20, 
        blank=True,
        null=True,
        validators=[RegexValidator(regex=r'^\+?1?\d{9,15}$', message="Phone number must be entered in the format: '+999999999'. Up to 15 digits allowed.")]
    )
    phone_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="Primary contact phone number (timetable system).",
    )
    
    # Profile images (supporting both field names)
    profile_picture = models.ImageField(upload_to='profiles/', blank=True, null=True)
    profile_image = models.ImageField(
        upload_to="profiles/",
        blank=True,
        null=True,
        help_text="Optional profile image (timetable system).",
    )
    
    is_verified = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # ===========
    # TIMETABLE-SPECIFIC FIELDS
    # ===========
    # Teacher fields
    teacher_code = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Readable code for a teacher. Example: 'AK-CAP', 'BTDS', etc.",
    )
    teacher_employee_id = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text="Official employee id of the teacher. Example: 'EMP-00123'.",
    )
    teacher_subjects = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text="Subjects that the teacher handles. Example: 'Physics', or 'Physics, Chemistry'.",
    )
    default_available_slots = models.JSONField(
        blank=True,
        null=True,
        help_text="Optional default weekly slot availability for this teacher.",
    )
    # Many-to-Many relationship with Institutes
    institutes = models.ManyToManyField(
        Institute, 
        through='UserInstituteMembership',
        related_name='associated_users',
        blank=True
    )
    
    # Center relation (for timetable system)
    center = models.ForeignKey(
        "Center",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="users",
        help_text="Center to which the user belongs (timetable system).",
    )
    
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username', 'first_name', 'last_name']

    class Meta:
        ordering = ['-created_at']
        verbose_name = "User"
        verbose_name_plural = "Users"

    def __str__(self):
        return f"{self.get_full_name()} ({self.email})"

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()
    
    # ===========
    # EXAM SYSTEM METHODS
    # ===========
    def is_institute_admin(self):
        return self.role in ['super_admin', 'SUPER_ADMIN', 'institute_admin', 'admin', 'ADMIN']
    
    def can_manage_exams(self):
        return self.role in ['super_admin', 'SUPER_ADMIN', 'institute_admin', 'exam_admin', 'teacher', 'TEACHER', 'admin', 'ADMIN']
    
    def can_create_exams(self):
        return self.role in ['super_admin', 'SUPER_ADMIN', 'institute_admin', 'exam_admin', 'admin', 'ADMIN']
    
    # ===========
    # CUSTOM SAVE METHOD
    # ===========
    def save(self, *args, **kwargs):
        """Override save to always normalize role to lowercase"""
        if self.role:
            self.role = self.role.lower()
        super().save(*args, **kwargs)
    
    # ===========
    # TIMETABLE SYSTEM METHODS
    # ===========
    def is_platform_owner(self) -> bool:
        """Check if user is platform owner with global access"""
        return self.role == self.ROLE_PLATFORM_OWNER

    def is_super_admin(self) -> bool:
        """Check if user is super admin"""
        return self.role == self.ROLE_SUPER_ADMIN
    
    def is_admin(self) -> bool:
        """Check if user is center admin or institute admin"""
        return self.role in [self.ROLE_ADMIN, self.ROLE_INSTITUTE_ADMIN]
    
    def is_teacher(self) -> bool:
        """Check if user is teacher"""
        return self.role == self.ROLE_TEACHER
    
    def is_student(self) -> bool:
        """Check if user is student"""
        return self.role == self.ROLE_STUDENT
    
    def is_staff_role(self) -> bool:
        """Check if user is staff"""
        return self.role == self.ROLE_STAFF
    
    def get_role_in_institute(self, institute):
        """Get the user's role in a specific institute"""
        if self.role == self.ROLE_SUPER_ADMIN:
            return self.ROLE_SUPER_ADMIN
            
        membership = self.memberships.filter(institute=institute, is_active=True).first()
        if membership:
            return membership.role
            
        # Fallback to primary institute
        if self.institute == institute:
            return self.role
            
        return None

    @property
    def current_membership(self):
        """Get membership for the currently active tenant database"""
        from .utils import get_current_db
        db_name = get_current_db()
        
        if not db_name or db_name == 'default':
            return None
            
        return self.memberships.filter(institute__db_name=db_name, is_active=True).first()

    @property
    def effective_teacher_code(self):
        """Returns the teacher code for the current institute context or primary as fallback"""
        membership = self.current_membership
        if membership and membership.teacher_code:
            return membership.teacher_code
        return self.teacher_code

    @property
    def effective_center(self):
        """Returns the center for the current institute context or primary as fallback"""
        membership = self.current_membership
        if membership and membership.center:
            return membership.center
        return self.center

# New junction model for many-to-many user-institute relationships
class UserInstituteMembership(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='memberships')
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='memberships')
    role = models.CharField(max_length=20, choices=User.ROLE_CHOICES, default='student')
    
    # Institute-specific profile data
    teacher_code = models.CharField(max_length=50, blank=True, null=True)
    center = models.ForeignKey('Center', on_delete=models.SET_NULL, null=True, blank=True)
    
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'institute')
        verbose_name = "User Institute Membership"
        verbose_name_plural = "User Institute Memberships"

    def __str__(self):
        return f"{self.user.email} - {self.institute.name} ({self.role})"
    # PROPERTY HELPERS FOR COMPATIBILITY
    # ===========
    # Removed shadowing center_id property to allow setting center_id directly
    # Django provides center_id automatically for the center ForeignKey.


class Center(TimeStampedModel):
    """
    Represents a physical or virtual center/branch of the Institute.
    Example: 'Allen - Jaipur Center', 'Allen - Mumbai Center', etc.
    """
    institute = models.ForeignKey(
        Institute,
        on_delete=models.CASCADE,
        related_name="centers",
        help_text="Parent institute of this center.",
    )
    name = models.CharField(
        max_length=255,
        help_text="Name of the center. Example: 'Allen - Jaipur Center'.",
    )
    city = models.CharField(
        max_length=100,
        help_text="City where this center is located.",
    )
    address = models.TextField(
        blank=True,
        null=True,
        help_text="Optional full address of the center.",
    )
    
    # Admin user(s) for this center
    admins = models.ManyToManyField(
        User,
        blank=True,
        related_name="admin_centers",
        help_text="Users who are admins of this center.",
    )

    def __str__(self) -> str:
        return f"{self.name} ({self.city})"


class Program(TimeStampedModel):
    """
    Represents a Program at an Institute level.
    Programs are created at Institute level and can be assigned to Batches.
    Examples: 'Super 30', 'Only Board', 'JEE Advanced', 'NEET UG'
    """
    institute = models.ForeignKey(
        Institute,
        on_delete=models.CASCADE,
        related_name="programs",
        help_text="Institute where this program is offered.",
    )
    center = models.ForeignKey(
        'Center',
        on_delete=models.CASCADE,
        related_name="programs",
        null=True,
        blank=True,
        help_text="Optional center where this program is specifically offered.",
    )
    name = models.CharField(
        max_length=255,
        help_text="Program name, e.g. 'Super 30', 'Only Board'.",
    )
    description = models.TextField(
        blank=True,
        null=True,
        help_text="Optional detailed description of the program.",
    )
    category = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text="Optional category for grouping programs.",
    )
    is_active = models.BooleanField(
        default=True,
        help_text="Programs can be deactivated instead of deleting.",
    )

    class Meta:
        unique_together = ("institute", "name")
        ordering = ["institute__name", "name"]

    def __str__(self) -> str:
        return f"{self.name} - {self.institute.name}"


class Batch(TimeStampedModel):
    """
    Represents a Batch inside a Center, optionally linked to a Program.
    - Created by Admin of that particular center.
    - Students are enrolled into Batches.
    - Teachers can be assigned to Batches as well.
    """
    center = models.ForeignKey(
        Center,
        on_delete=models.CASCADE,
        related_name="batches",
        null=True,
        blank=True,
        help_text="Center where this batch runs.",
    )
    program = models.ForeignKey(
        Program,
        on_delete=models.SET_NULL,
        related_name="batches",
        null=True,
        blank=True,
        help_text="Program under which this batch runs (optional).",
    )
    code = models.CharField(
        max_length=50,
        help_text="Short unique-like code for this batch. Example: 'S30-A-2025'.",
    )
    name = models.CharField(
        max_length=255,
        help_text="Batch name, e.g. 'Super 30 - Batch A (2025)'.",
    )
    start_date = models.DateField(
        null=True,
        blank=True,
        help_text="Optional batch start date.",
    )
    end_date = models.DateField(
        null=True,
        blank=True,
        help_text="Optional batch end date.",
    )
    
    # Teachers who teach this batch
    teachers = models.ManyToManyField(
        User,
        blank=True,
        related_name="teaching_batches",
        help_text="Teachers assigned to this batch.",
    )

    class Meta:
        ordering = ["center__name", "program__name", "name"]

    def __str__(self) -> str:
        parts = [self.name]
        if self.program:
            parts.append(f"({self.program.name})")
        if self.center:
            parts.append(f"@ {self.center.name}")
        return " ".join(parts)


class Enrollment(TimeStampedModel):
    """
    Represents the relationship between a Student and a Batch.
    """
    STATUS_ACTIVE = "ACTIVE"
    STATUS_COMPLETED = "COMPLETED"
    STATUS_DROPPED = "DROPPED"

    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_DROPPED, "Dropped"),
    ]

    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="enrollments",
        help_text="User with role=STUDENT enrolled in this batch.",
    )
    batch = models.ForeignKey(
        Batch,
        on_delete=models.CASCADE,
        related_name="enrollments",
        help_text="Batch into which the student is enrolled.",
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_ACTIVE,
        help_text="Current enrollment status of the student in this batch.",
    )
    joined_on = models.DateField(
        auto_now_add=True,
        help_text="Date when the student joined this batch.",
    )

    class Meta:
        unique_together = ("student", "batch")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.student.username} -> {self.batch.name} ({self.status})"


# Keep existing models for exam system compatibility
class InstituteInvitation(models.Model):
    """Model for inviting users to join an institute"""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
        ('expired', 'Expired'),
    ]
    
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='invitations')
    email = models.EmailField()
    role = models.CharField(max_length=20, choices=[
        ('super_admin', 'Super Admin'),
        ('institute_admin', 'Institute Admin'),
        ('exam_admin', 'Exam Admin'),
        ('teacher', 'Teacher'),
        ('student', 'Student'),
    ], default='student')
    invited_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sent_institute_invitations')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    message = models.TextField(blank=True, help_text="Optional message to include with invitation")
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ['institute', 'email']
        ordering = ['-created_at']
    
    def __str__(self):
        return f"Invitation to {self.email} for {self.institute.name}"
    
    def is_expired(self):
        from django.utils import timezone
        return timezone.now() > self.expires_at


class UserPermission(models.Model):
    """Custom permissions for users within institutes"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='permissions')
    permission_type = models.CharField(max_length=50)
    granted_by = models.ForeignKey(User, on_delete=models.CASCADE, related_name='granted_permissions')
    granted_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ['user', 'permission_type']

    def __str__(self):
        return f"{self.user.email} - {self.permission_type}"


class InstituteSettings(models.Model):
    """Institute-specific settings"""
    institute = models.OneToOneField(Institute, on_delete=models.CASCADE, related_name='settings')
    allow_student_registration = models.BooleanField(default=True)
    require_email_verification = models.BooleanField(default=True)
    max_exam_duration = models.IntegerField(default=180, help_text="Maximum exam duration in minutes")
    allow_exam_retakes = models.BooleanField(default=False)
    max_retake_attempts = models.IntegerField(default=1)
    exam_security_level = models.CharField(
        max_length=20,
        choices=[
            ('basic', 'Basic'),
            ('standard', 'Standard'),
            ('high', 'High'),
        ],
        default='standard'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Settings for {self.institute.name}"


class DeviceSession(models.Model):
    """
    Tracks device-based login sessions for users.
    Ensures students can only be logged in on one device at a time.
    """
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='device_sessions',
        help_text="User associated with this device session"
    )
    device_fingerprint = models.CharField(
        max_length=255,
        unique=True,
        db_index=True,
        help_text="Unique identifier for the device based on browser and hardware characteristics"
    )
    device_type = models.CharField(
        max_length=50,
        help_text="Type of device: mobile, desktop, tablet"
    )
    browser = models.CharField(
        max_length=100,
        help_text="Browser name and version"
    )
    os = models.CharField(
        max_length=100,
        help_text="Operating system name and version"
    )
    screen_resolution = models.CharField(
        max_length=20,
        help_text="Screen resolution (e.g., 1920x1080)"
    )
    timezone = models.CharField(
        max_length=50,
        help_text="User's timezone"
    )
    ip_address = models.GenericIPAddressField(
        help_text="IP address of the device"
    )
    user_agent = models.TextField(
        help_text="Full user agent string from the browser"
    )
    is_active = models.BooleanField(
        default=True,
        db_index=True,
        help_text="Whether this session is currently active"
    )
    last_activity = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp of last activity on this session"
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        help_text="When this session was created"
    )
    expires_at = models.DateTimeField(
        help_text="When this session expires (24 hours from last activity)"
    )

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['device_fingerprint']),
            models.Index(fields=['expires_at']),
        ]
        verbose_name = "Device Session"
        verbose_name_plural = "Device Sessions"

    def __str__(self):
        return f"{self.user.email} - {self.device_type} ({self.browser}) - {'Active' if self.is_active else 'Inactive'}"

    def is_expired(self):
        """Check if this session has expired"""
        from django.utils import timezone
        return timezone.now() > self.expires_at


class ActivityLog(models.Model):
    """
    Tracks all major activities within an institute for auditing.
    """
    LOG_TYPES = [
        ('exam', 'Exam Activity'),
        ('user', 'User Activity'),
        ('violation', 'Security Violation'),
        ('login', 'Login Activity'),
        ('system', 'System Activity'),
    ]
    
    institute = models.ForeignKey(Institute, on_delete=models.CASCADE, related_name='activity_logs')
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='activities')
    log_type = models.CharField(max_length=20, choices=LOG_TYPES)
    title = models.CharField(max_length=255)
    description = models.TextField()
    timestamp = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='info') # success, warning, error, info
    metadata = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']

    def __str__(self):
        return f"{self.title} - {self.timestamp}"
