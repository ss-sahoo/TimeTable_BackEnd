from django.conf import settings
from django.utils import timezone
from rest_framework import serializers
from .models import (
    Exam, ExamAttempt, ExamResult, ExamInvitation, ExamAnalytics, ExamViolation, ExamProctoring, QuestionAnalytics,
    QuestionEvaluation, EvaluationBatch, EvaluationSettings, EvaluationProgress, EvaluationRubric, ExamReschedule
)
from patterns.serializers import ExamPatternSerializer
from accounts.serializers import UserSerializer


class ExamSerializer(serializers.ModelSerializer):
    pattern = serializers.SerializerMethodField()
    pattern_id = serializers.IntegerField(write_only=True)
    program_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    center_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    institute_name = serializers.CharField(source='institute.name', read_only=True)
    is_active = serializers.ReadOnlyField()
    total_questions = serializers.ReadOnlyField()
    total_marks = serializers.ReadOnlyField()
    allowed_users_data = UserSerializer(source='allowed_users', many=True, read_only=True)
    questions_added = serializers.ReadOnlyField()
    questions_required = serializers.ReadOnlyField()
    questions_remaining = serializers.ReadOnlyField()
    question_completion_percent = serializers.ReadOnlyField()
    is_question_complete = serializers.ReadOnlyField()
    share_url = serializers.SerializerMethodField()
    
    # Visibility scope fields
    center_ids = serializers.ListField(child=serializers.CharField(), write_only=True, required=False)
    batch_ids = serializers.ListField(child=serializers.CharField(), write_only=True, required=False)
    allowed_centers_data = serializers.SerializerMethodField()
    allowed_batches_data = serializers.SerializerMethodField()

    def get_allowed_centers_data(self, obj):
        """Get list of allowed center IDs and names"""
        return [{'id': str(c.id), 'name': c.name} for c in obj.allowed_centers.all()]
    
    def get_allowed_batches_data(self, obj):
        """Get list of allowed batch IDs and names"""
        return [{'id': str(b.id), 'name': b.name, 'code': b.code} for b in obj.allowed_batches.all()]

    def get_pattern(self, obj):
        """Serialize pattern with exam_id context for section question counts"""
        if obj.pattern:
            context = self.context.copy()
            context['exam_id'] = obj.id
            return ExamPatternSerializer(obj.pattern, context=context).data
        return None

    class Meta:
        model = Exam
        fields = [
            'id', 'title', 'description', 'institute', 'institute_name', 'program', 'program_id',
            'center', 'center_id', 'pattern', 'pattern_id',
            'status', 'start_date', 'end_date', 'duration_minutes', 'max_attempts',
            'allow_late_submission', 'late_submission_penalty', 'require_fullscreen',
            'disable_copy_paste', 'disable_right_click', 'enable_webcam_proctoring',
            'allow_tab_switching',
            # Shuffle settings
            'shuffle_questions', 'shuffle_within_sections', 'shuffle_sections',
            'shuffle_subjects', 'shuffle_options', 'shuffle_seed_per_student',
            # Access control
            'is_public', 'public_access_token', 'public_token_expires_at',
            'public_allowed_ip_ranges', 'public_allow_multiple_devices', 'public_link_created_at',
            'public_link_last_used_at', 'public_link_usage_count',
            'allowed_users', 'allowed_users_data',
            # Visibility scope
            'visibility_scope', 'center_ids', 'batch_ids',
            'allowed_centers_data', 'allowed_batches_data',
            'created_by', 'created_by_name', 'is_active', 'total_questions', 'total_marks',
            'timezone', 'grace_period_minutes', 'buffer_time_minutes', 'auto_start', 'auto_end',
            'reschedule_allowed', 'max_reschedules', 'reschedule_deadline',
            'created_at', 'updated_at',
            'questions_added', 'questions_required', 'questions_remaining',
            'question_completion_percent', 'is_question_complete', 'share_url',
            'exam_mode', 'omr_config', 'omr_metadata', 'omr_sheet_generated', 'omr_sheet_file',
            'ai_evaluation_enabled', 'marking_strictness', 'show_result_after_exam_end', 'is_flexible'
        ]
        read_only_fields = [
            'id', 'created_by', 'created_at', 'updated_at',
            'questions_added', 'questions_required', 'questions_remaining',
            'question_completion_percent', 'is_question_complete',
            'public_access_token', 'public_link_created_at', 'public_link_last_used_at',
            'public_link_usage_count'
        ]

    def validate(self, attrs):
        start_date = attrs.get('start_date') or getattr(self.instance, 'start_date', None)
        end_date = attrs.get('end_date') or getattr(self.instance, 'end_date', None)

        if start_date and end_date and start_date >= end_date:
            raise serializers.ValidationError("Start date must be before end date")
        return attrs

    def validate_status(self, value):
        if value in ['published', 'active']:
            exam = self.instance

            # Only enforce question count when transitioning TO published/active,
            # not when the exam is already in that status and being edited.
            current_status = getattr(exam, 'status', None) if exam else None
            if current_status == value:
                return value

            if not exam:
                raise serializers.ValidationError(
                    "Add all required questions before publishing the exam."
                )

            required = exam.questions_required
            added = exam.questions_added

            if required <= 0:
                raise serializers.ValidationError(
                    "Exam pattern does not define any questions."
                )

            if added < required and exam.exam_mode == 'online':
                raise serializers.ValidationError(
                    f"Add all {required} questions before publishing (currently {added})."
                )

        return value

    def create(self, validated_data):
        # Extract visibility scope related data
        center_ids = validated_data.pop('center_ids', [])
        batch_ids = validated_data.pop('batch_ids', [])
        
        # Handle program_id and center_id if provided as write_only fields
        program_id = validated_data.pop('program_id', None)
        center_id = validated_data.pop('center_id', None)
        if program_id:
            validated_data['program_id'] = program_id
        if center_id:
            validated_data['center_id'] = center_id
        # Get the pattern object and set duration_minutes from it
        pattern_id = validated_data.pop('pattern_id', None)
        from patterns.models import ExamPattern
        try:
            pattern = ExamPattern.objects.get(id=pattern_id)
        except (ExamPattern.DoesNotExist, TypeError):
            raise serializers.ValidationError({'pattern_id': 'Valid exam pattern is required'})

        validated_data['pattern'] = pattern
        validated_data['duration_minutes'] = pattern.total_duration
        
        if 'exam_mode' not in validated_data or validated_data['exam_mode'] == 'online':
            validated_data['exam_mode'] = getattr(pattern, 'exam_mode', 'online')
        
        if 'omr_config' not in validated_data or not validated_data['omr_config']:
            validated_data['omr_config'] = getattr(pattern, 'omr_config', {})
        
        exam = super().create(validated_data)
        
        # Handle visibility scope relationships
        if center_ids and exam.visibility_scope == 'centers':
            from accounts.models import Center
            centers = Center.objects.filter(id__in=center_ids)
            exam.allowed_centers.set(centers)
        
        if batch_ids and exam.visibility_scope == 'batches':
            from accounts.models import Batch
            batches = Batch.objects.filter(id__in=batch_ids)
            exam.allowed_batches.set(batches)
        
        return exam

    def update(self, instance, validated_data):
        # Extract visibility scope related data
        center_ids = validated_data.pop('center_ids', None)
        batch_ids = validated_data.pop('batch_ids', None)
        
        # Handle program_id and center_id
        program_id = validated_data.pop('program_id', None)
        center_id = validated_data.pop('center_id', None)
        if program_id is not None:
            instance.program_id = program_id
        if center_id is not None:
            instance.center_id = center_id
            
        # Pattern cannot be changed after exam creation
        if 'pattern_id' in validated_data:
            pattern_id = validated_data.pop('pattern_id')
            if str(pattern_id) != str(instance.pattern_id):
                raise serializers.ValidationError(
                    {"pattern_id": "Exam pattern cannot be changed after creation."}
                )
        
        exam = super().update(instance, validated_data)
        
        # Handle visibility scope relationships
        if center_ids is not None and exam.visibility_scope == 'centers':
            from accounts.models import Center
            centers = Center.objects.filter(id__in=center_ids)
            exam.allowed_centers.set(centers)
        
        if batch_ids is not None and exam.visibility_scope == 'batches':
            from accounts.models import Batch
            batches = Batch.objects.filter(id__in=batch_ids)
            exam.allowed_batches.set(batches)
        
        return exam

    def get_share_url(self, obj):
        frontend_url = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
        if not frontend_url:
            return None
        return f"{frontend_url}/public-exam/{obj.public_access_token}"


class ExamCreateSerializer(serializers.ModelSerializer):
    pattern_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    program_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    center_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    copy_from_exam_id = serializers.IntegerField(write_only=True, required=False, allow_null=True)
    id = serializers.IntegerField(read_only=True)
    public_access_token = serializers.UUIDField(read_only=True)
    public_token_expires_at = serializers.DateTimeField(read_only=True)
    public_allowed_ip_ranges = serializers.ListField(child=serializers.CharField(), read_only=True)
    public_allow_multiple_devices = serializers.BooleanField(read_only=True)
    public_link_created_at = serializers.DateTimeField(read_only=True)
    public_link_last_used_at = serializers.DateTimeField(read_only=True)
    public_link_usage_count = serializers.IntegerField(read_only=True)
    share_url = serializers.SerializerMethodField()
    
    # Visibility scope fields
    center_ids = serializers.ListField(child=serializers.CharField(), write_only=True, required=False)
    batch_ids = serializers.ListField(child=serializers.CharField(), write_only=True, required=False)
    
    class Meta:
        model = Exam
        fields = [
            'id', 'title', 'description', 'pattern_id', 'program_id', 'center_id', 'copy_from_exam_id', 'start_date', 'end_date',
            'max_attempts', 'allow_late_submission', 'late_submission_penalty',
            'require_fullscreen', 'disable_copy_paste', 'disable_right_click',
            'enable_webcam_proctoring', 'allow_tab_switching',
            # Shuffle settings
            'shuffle_questions', 'shuffle_within_sections', 'shuffle_sections',
            'shuffle_subjects', 'shuffle_options', 'shuffle_seed_per_student',
            # Access control
            'is_public', 'allowed_users',
            'visibility_scope', 'center_ids', 'batch_ids',
            'status', 'timezone', 'grace_period_minutes', 'buffer_time_minutes', 'auto_start',
            'auto_end', 'reschedule_allowed', 'max_reschedules', 'reschedule_deadline',
            'public_access_token', 'public_token_expires_at', 'public_allowed_ip_ranges',
            'public_allow_multiple_devices', 'public_link_created_at', 'public_link_last_used_at',
            'public_link_usage_count', 'share_url', 'exam_mode', 'ai_evaluation_enabled', 'marking_strictness',
            'show_result_after_exam_end', 'is_flexible'
        ]
        read_only_fields = [
            'id', 'public_access_token', 'public_token_expires_at', 'public_allowed_ip_ranges',
            'public_allow_multiple_devices', 'public_link_created_at', 'public_link_last_used_at',
            'public_link_usage_count', 'share_url'
        ]
        extra_kwargs = {
            'pattern_id': {'required': False, 'allow_null': True}
        }

    def create(self, validated_data):
        # Extract visibility scope related data
        center_ids = validated_data.pop('center_ids', [])
        batch_ids = validated_data.pop('batch_ids', [])
        
        # Handle program_id and center_id
        program_id = validated_data.pop('program_id', None)
        center_id = validated_data.pop('center_id', None)
        if program_id:
            validated_data['program_id'] = program_id
        if center_id:
            validated_data['center_id'] = center_id
            
        # Extract copy source if provided
        copy_from_exam_id = validated_data.pop('copy_from_exam_id', None)
        
        # Get the pattern object and set duration_minutes from it
        pattern_id = validated_data.pop('pattern_id', None)
        from patterns.models import ExamPattern
        
        source_exam = None
        if copy_from_exam_id:
            try:
                source_exam = Exam.objects.get(id=copy_from_exam_id)
                # If pattern_id not provided, use source exam's pattern
                if not pattern_id:
                    pattern_id = source_exam.pattern_id
            except Exam.DoesNotExist:
                raise serializers.ValidationError({'copy_from_exam_id': 'Source exam not found'})
        
        if not pattern_id:
            raise serializers.ValidationError({'pattern_id': 'Exam pattern or copy_from_exam_id is required'})

        try:
            pattern = ExamPattern.objects.get(id=pattern_id)
        except ExamPattern.DoesNotExist:
            raise serializers.ValidationError({'pattern_id': 'Exam pattern not found'})

        validated_data['pattern'] = pattern
        validated_data['duration_minutes'] = pattern.total_duration
        
        # Copy mode and config from pattern if not provided
        if 'exam_mode' not in validated_data or validated_data['exam_mode'] == 'online':
            validated_data['exam_mode'] = getattr(pattern, 'exam_mode', 'online')
        if 'omr_config' not in validated_data or not validated_data['omr_config']:
            validated_data['omr_config'] = getattr(pattern, 'omr_config', {})

        # Set status to 'draft' if not provided
        validated_data.setdefault('status', 'draft')
        
        exam = super().create(validated_data)
        
        # Handle copying if requested
        if source_exam:
            from .copy_utils import clone_exam_assets
            clone_exam_assets(source_exam, exam, user=self.context['request'].user)
        
        # Handle visibility scope relationships
        if center_ids and exam.visibility_scope == 'centers':
            from accounts.models import Center
            centers = Center.objects.filter(id__in=center_ids)
            exam.allowed_centers.set(centers)
        
        if batch_ids and exam.visibility_scope == 'batches':
            from accounts.models import Batch
            batches = Batch.objects.filter(id__in=batch_ids)
            exam.allowed_batches.set(batches)
        
        return exam

    def get_share_url(self, obj):
        frontend_url = getattr(settings, 'FRONTEND_URL', '').rstrip('/')
        if not frontend_url:
            return None
        return f"{frontend_url}/public-exam/{obj.public_access_token}"


class ExamAttemptSerializer(serializers.ModelSerializer):
    student_name = serializers.CharField(source='student.get_full_name', read_only=True)
    exam_title = serializers.CharField(source='exam.title', read_only=True)
    exam = ExamSerializer(read_only=True)  # Include full exam object
    is_completed = serializers.ReadOnlyField()
    time_remaining = serializers.ReadOnlyField()

    class Meta:
        model = ExamAttempt
        fields = [
            'id', 'exam', 'exam_title', 'student', 'student_name', 'attempt_number',
            'status', 'started_at', 'submitted_at', 'time_spent', 'score', 'percentage',
            'rank', 'ip_address', 'violations_count', 'proctoring_enabled', 
            'max_violations_allowed', 'fullscreen_required', 'is_completed', 'time_remaining',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'student', 'created_at', 'updated_at']

    def to_representation(self, instance):
        representation = super().to_representation(instance)
        request = self.context.get('request')
        
        # Mask score, percentage, and rank for students if results are hidden
        if request and request.user and request.user.role in ['student', 'STUDENT']:
            exam = instance.exam
            if exam.show_result_after_exam_end and timezone.now() < exam.end_date:
                representation['score'] = None
                representation['percentage'] = None
                representation['rank'] = None
                
        return representation


class ExamResultSerializer(serializers.ModelSerializer):
    attempt = ExamAttemptSerializer(read_only=True)

    class Meta:
        model = ExamResult
        fields = [
            'id', 'attempt', 'section_scores', 'total_questions_attempted',
            'total_correct_answers', 'total_wrong_answers', 'total_unattempted',
            'answers', 'created_at'
        ]


class ExamInvitationSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.get_full_name', read_only=True)
    user_email = serializers.CharField(source='user.email', read_only=True)
    exam_title = serializers.CharField(source='exam.title', read_only=True)
    invited_by_name = serializers.CharField(source='invited_by.get_full_name', read_only=True)
    is_valid_now = serializers.ReadOnlyField()
    can_attempt = serializers.ReadOnlyField()

    class Meta:
        model = ExamInvitation
        fields = [
            'id', 'exam', 'exam_title', 'user', 'user_name', 'user_email',
            'invited_by', 'invited_by_name', 'invited_at', 'is_accepted', 'accepted_at',
            'access_code', 'valid_from', 'valid_until', 'max_attempts', 'used_attempts',
            'is_active', 'is_valid_now', 'can_attempt'
        ]
        read_only_fields = ['id', 'invited_by', 'invited_at']


class ExamViolationSerializer(serializers.ModelSerializer):
    attempt_student = serializers.CharField(source='attempt.student.get_full_name', read_only=True)
    attempt_exam = serializers.CharField(source='attempt.exam.title', read_only=True)
    violation_type_display = serializers.CharField(source='get_violation_type_display', read_only=True)

    class Meta:
        model = ExamViolation
        fields = [
            'id', 'attempt', 'attempt_student', 'attempt_exam', 'violation_type',
            'violation_type_display', 'timestamp', 'screenshot', 'metadata'
        ]
        read_only_fields = ['id', 'timestamp']


class ExamProctoringSerializer(serializers.ModelSerializer):
    attempt_student = serializers.CharField(source='attempt.student.get_full_name', read_only=True)
    attempt_exam = serializers.CharField(source='attempt.exam.title', read_only=True)

    class Meta:
        model = ExamProctoring
        fields = [
            'id', 'attempt', 'attempt_student', 'attempt_exam', 'webcam_enabled',
            'snapshots', 'incidents', 'face_verification_passed', 'total_violations',
            'auto_disqualified', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ProctoringIncidentSerializer(serializers.Serializer):
    event_type = serializers.CharField(max_length=64)
    timestamp = serializers.DateTimeField(required=False)
    severity = serializers.ChoiceField(choices=['info', 'low', 'medium', 'high'], default='info')
    details = serializers.JSONField(default=dict)

    def validate_timestamp(self, value):
        return value or timezone.now()


class ExamAnalyticsSerializer(serializers.ModelSerializer):
    exam_title = serializers.CharField(source='exam.title', read_only=True)

    class Meta:
        model = ExamAnalytics
        fields = [
            'id', 'exam', 'exam_title', 'total_invited', 'total_started',
            'total_completed', 'completion_rate', 'average_score', 'highest_score',
            'lowest_score', 'average_time_spent', 'last_updated'
        ]


class ExamStartSerializer(serializers.Serializer):
    exam_id = serializers.IntegerField()

    def validate_exam_id(self, value):
        try:
            exam = Exam.objects.get(id=value)
            if not exam.is_active:
                raise serializers.ValidationError("Exam is not currently active")
            return value
        except Exam.DoesNotExist:
            raise serializers.ValidationError("Exam not found")


class ExamSubmitSerializer(serializers.Serializer):
    attempt_id = serializers.IntegerField()
    answers = serializers.JSONField()
    is_disqualified = serializers.BooleanField(default=False, required=False)

    def validate_attempt_id(self, value):
        try:
            attempt = ExamAttempt.objects.get(id=value)
            if attempt.status != 'in_progress':
                raise serializers.ValidationError("Exam attempt is not in progress")
            return value
        except ExamAttempt.DoesNotExist:
            raise serializers.ValidationError("Exam attempt not found")


class ViolationLogSerializer(serializers.Serializer):
    violation_type = serializers.ChoiceField(choices=ExamViolation.VIOLATION_TYPES)
    screenshot = serializers.ImageField(required=False)
    metadata = serializers.JSONField(default=dict)
    
    def create(self, validated_data):
        attempt = self.context.get('attempt')
        if not attempt:
            raise serializers.ValidationError("Attempt context is required")
        
        return ExamViolation.objects.create(
            attempt=attempt,
            violation_type=validated_data['violation_type'],
            screenshot=validated_data.get('screenshot'),
            metadata=validated_data.get('metadata', {})
        )


class ExamAccessSerializer(serializers.Serializer):
    access_code = serializers.CharField(max_length=20, required=False)
    exam_id = serializers.IntegerField(required=False)
    
    def validate(self, attrs):
        access_code = attrs.get('access_code')
        exam_id = attrs.get('exam_id')
        
        if not access_code and not exam_id:
            raise serializers.ValidationError("Either access_code or exam_id must be provided")
        
        return attrs


class SnapshotUploadSerializer(serializers.Serializer):
    image_data = serializers.CharField(help_text="Base64 encoded image data")
    timestamp = serializers.CharField()  # Accept string timestamp from frontend
    metadata = serializers.JSONField(default=dict)
    
    def validate_timestamp(self, value):
        """Convert string timestamp to datetime"""
        from datetime import datetime
        try:
            return datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            raise serializers.ValidationError("Invalid timestamp format")


class QuestionAnalyticsSerializer(serializers.ModelSerializer):
    success_rate = serializers.ReadOnlyField()
    
    class Meta:
        model = QuestionAnalytics
        fields = [
            'id', 'exam', 'question_number', 'question_text', 'total_attempts',
            'correct_attempts', 'wrong_attempts', 'unattempted', 'average_score',
            'max_marks', 'difficulty_level', 'success_rate', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class ExamAnalyticsDetailSerializer(serializers.ModelSerializer):
    question_analytics = QuestionAnalyticsSerializer(many=True, read_only=True)
    
    class Meta:
        model = ExamAnalytics
        fields = [
            'id', 'exam', 'total_invited', 'total_started', 'total_completed',
            'completion_rate', 'average_score', 'highest_score', 'lowest_score',
            'average_time_spent', 'last_updated', 'question_analytics'
        ]


# Evaluation System Serializers
class QuestionEvaluationSerializer(serializers.ModelSerializer):
    """Serializer for QuestionEvaluation model"""
    
    question = serializers.SerializerMethodField()
    evaluated_by_name = serializers.SerializerMethodField()
    
    class Meta:
        model = QuestionEvaluation
        fields = [
            'id', 'attempt', 'question', 'question_number',
            'student_answer', 'is_answered', 'evaluation_type', 'evaluation_status',
            'marks_obtained', 'max_marks', 'is_correct', 'evaluated_by', 'evaluated_by_name',
            'evaluated_at', 'evaluation_notes', 'ai_confidence_score', 'ai_feedback',
            'manual_feedback', 'requires_review', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_question(self, obj):
        from questions.serializers import QuestionSerializer
        return QuestionSerializer(obj.question).data
    
    def get_evaluated_by_name(self, obj):
        if obj.evaluated_by:
            return obj.evaluated_by.get_full_name()
        return None


class EvaluationBatchSerializer(serializers.ModelSerializer):
    """Serializer for EvaluationBatch model"""
    
    processed_by_name = serializers.SerializerMethodField()
    exam_title = serializers.SerializerMethodField()
    
    class Meta:
        model = EvaluationBatch
        fields = [
            'id', 'exam', 'exam_title', 'batch_type', 'status', 'questions_count',
            'evaluated_count', 'failed_count', 'started_at', 'completed_at',
            'processed_by', 'processed_by_name', 'ai_model_used', 'ai_processing_time',
            'error_message', 'retry_count', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_processed_by_name(self, obj):
        if obj.processed_by:
            return obj.processed_by.get_full_name()
        return None
    
    def get_exam_title(self, obj):
        return obj.exam.title


class EvaluationSettingsSerializer(serializers.ModelSerializer):
    """Serializer for EvaluationSettings model"""
    
    class Meta:
        model = EvaluationSettings
        fields = [
            'id', 'exam', 'enable_auto_evaluation', 'auto_evaluate_mcq',
            'auto_evaluate_numerical', 'auto_evaluate_true_false', 'auto_evaluate_fill_blank',
            'enable_manual_evaluation', 'require_manual_review', 'manual_evaluation_deadline',
            'enable_ai_evaluation', 'ai_model_preference', 'ai_confidence_threshold',
            'ai_fallback_to_manual', 'enable_mixed_evaluation', 'auto_first_then_manual',
            'ai_first_then_manual', 'notify_evaluators', 'notify_students_on_completion',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class EvaluationProgressSerializer(serializers.ModelSerializer):
    """Serializer for EvaluationProgress model"""
    
    completion_percentage = serializers.ReadOnlyField()
    
    class Meta:
        model = EvaluationProgress
        fields = [
            'id', 'exam', 'total_questions', 'auto_evaluated', 'manually_evaluated',
            'ai_evaluated', 'pending_evaluation', 'is_fully_evaluated',
            'evaluation_completed_at', 'average_auto_confidence', 'manual_evaluation_time',
            'ai_evaluation_time', 'completion_percentage', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class EvaluationRubricSerializer(serializers.ModelSerializer):
    """Serializer for EvaluationRubric model"""
    
    created_by_name = serializers.SerializerMethodField()
    question_text = serializers.SerializerMethodField()
    
    class Meta:
        model = EvaluationRubric
        fields = [
            'id', 'question', 'exam', 'rubric_name', 'description', 'max_marks',
            'criteria', 'is_active', 'created_by', 'created_by_name', 'question_text',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name()
        return None
    
    def get_question_text(self, obj):
        return obj.question.question_text


class ManualEvaluationRequestSerializer(serializers.Serializer):
    """Serializer for manual evaluation requests"""
    
    marks_obtained = serializers.DecimalField(max_digits=5, decimal_places=2)
    is_correct = serializers.BooleanField()
    feedback = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    
    def validate_marks_obtained(self, value):
        if value < 0:
            raise serializers.ValidationError("Marks cannot be negative")
        return value


class AIEvaluationRequestSerializer(serializers.Serializer):
    """Serializer for AI evaluation requests"""
    
    force_evaluation = serializers.BooleanField(default=False)
    ai_model = serializers.CharField(max_length=50, required=False)
    confidence_threshold = serializers.DecimalField(
        max_digits=3, decimal_places=2, required=False,
        min_value=0, max_value=1
    )


class BatchEvaluationRequestSerializer(serializers.Serializer):
    """Serializer for batch evaluation requests"""
    
    evaluation_type = serializers.ChoiceField(choices=['manual', 'ai'])
    question_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=False
    )
    force_evaluation = serializers.BooleanField(default=False)


class EvaluationSummarySerializer(serializers.Serializer):
    """Serializer for evaluation summary"""
    
    total_questions = serializers.IntegerField()
    auto_evaluated = serializers.IntegerField()
    manually_evaluated = serializers.IntegerField()
    ai_evaluated = serializers.IntegerField()
    pending_evaluation = serializers.IntegerField()
    completion_percentage = serializers.DecimalField(max_digits=5, decimal_places=2)
    is_fully_evaluated = serializers.BooleanField()
    evaluation_completed_at = serializers.DateTimeField(allow_null=True)
    
    # Question details
    question_details = serializers.ListField(
        child=serializers.DictField(),
        required=False
    )


class QuestionEvaluationUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating question evaluations"""
    
    class Meta:
        model = QuestionEvaluation
        fields = [
            'marks_obtained', 'is_correct', 'evaluation_notes',
            'manual_feedback', 'requires_review'
        ]
    
    def validate_marks_obtained(self, value):
        if value < 0:
            raise serializers.ValidationError("Marks cannot be negative")
        if value > self.instance.max_marks:
            raise serializers.ValidationError(
                f"Marks cannot exceed maximum marks ({self.instance.max_marks})"
            )
        return value


class EvaluationStatisticsSerializer(serializers.Serializer):
    """Serializer for evaluation statistics"""
    
    total_attempts = serializers.IntegerField()
    fully_evaluated_attempts = serializers.IntegerField()
    partially_evaluated_attempts = serializers.IntegerField()
    pending_evaluation_attempts = serializers.IntegerField()
    
    # Time statistics
    average_evaluation_time = serializers.DurationField(allow_null=True)
    average_manual_evaluation_time = serializers.DurationField(allow_null=True)
    average_ai_evaluation_time = serializers.DurationField(allow_null=True)
    
    # Accuracy statistics
    average_ai_confidence = serializers.DecimalField(max_digits=3, decimal_places=2, allow_null=True)
    manual_review_rate = serializers.DecimalField(max_digits=5, decimal_places=2, allow_null=True)
    
    # Question type breakdown
    question_type_breakdown = serializers.DictField()
    evaluation_type_breakdown = serializers.DictField()


class ExamRescheduleSerializer(serializers.ModelSerializer):
    """Serializer for exam reschedule requests"""
    student_name = serializers.CharField(source='student.get_full_name', read_only=True)
    student_email = serializers.CharField(source='student.email', read_only=True)
    exam_title = serializers.CharField(source='exam.title', read_only=True)
    reviewed_by_name = serializers.CharField(source='reviewed_by.get_full_name', read_only=True)
    timezone_info = serializers.SerializerMethodField()
    
    class Meta:
        model = ExamReschedule
        fields = [
            'id', 'exam', 'exam_title', 'student', 'student_name', 'student_email',
            'original_start_date', 'original_end_date', 'new_start_date', 'new_end_date',
            'reason', 'status', 'reviewed_by', 'reviewed_by_name', 'review_notes',
            'timezone_info', 'created_at', 'reviewed_at'
        ]
        read_only_fields = ['id', 'created_at', 'reviewed_at']
    
    def get_timezone_info(self, obj):
        """Get timezone information for the exam"""
        return obj.exam.get_timezone_aware_dates()


class ExamRescheduleRequestSerializer(serializers.Serializer):
    """Serializer for creating reschedule requests"""
    new_start_date = serializers.DateTimeField()
    new_end_date = serializers.DateTimeField()
    reason = serializers.CharField(max_length=1000)
    
    def validate(self, attrs):
        if attrs['new_start_date'] >= attrs['new_end_date']:
            raise serializers.ValidationError("New start date must be before new end date")
        return attrs


class ExamRescheduleReviewSerializer(serializers.Serializer):
    """Serializer for reviewing reschedule requests"""
    status = serializers.ChoiceField(choices=ExamReschedule.RESCHEDULE_STATUS_CHOICES)
    review_notes = serializers.CharField(max_length=1000, required=False)


class TimezoneListSerializer(serializers.Serializer):
    """Serializer for timezone list"""
    value = serializers.CharField()
    label = serializers.CharField()
    offset = serializers.CharField()
    utc_offset = serializers.CharField()


class ExamInvitationSerializer(serializers.ModelSerializer):
    """Serializer for exam invitations"""
    student_name = serializers.CharField(source='student.get_full_name', read_only=True)
    student_email = serializers.CharField(source='student.email', read_only=True)
    exam_title = serializers.CharField(source='exam.title', read_only=True)
    invited_by_name = serializers.CharField(source='invited_by.get_full_name', read_only=True)
    
    class Meta:
        model = ExamInvitation
        fields = [
            'id', 'exam', 'exam_title', 'student', 'student_name', 'student_email',
            'invited_by', 'invited_by_name', 'status', 'invited_at', 'accepted_at',
            'declined_at', 'custom_message', 'decline_reason', 'invitation_token'
        ]
        read_only_fields = ['id', 'invited_at', 'accepted_at', 'declined_at', 'invitation_token']


class ExamInvitationCreateSerializer(serializers.Serializer):
    """Serializer for creating exam invitations"""
    student_emails = serializers.ListField(
        child=serializers.EmailField(),
        min_length=1,
        max_length=100
    )
    custom_message = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    send_reminder = serializers.BooleanField(default=False)


class ExamInvitationBulkSerializer(serializers.Serializer):
    """Serializer for bulk invitation operations"""
    student_emails = serializers.ListField(
        child=serializers.EmailField(),
        min_length=1,
        max_length=100
    )
    custom_message = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    send_reminder = serializers.BooleanField(default=False)


class EmailTemplateSerializer(serializers.Serializer):
    """Serializer for email templates"""
    template_type = serializers.ChoiceField(choices=[
        ('invitation', 'Exam Invitation'),
        ('reminder', 'Reminder'),
        ('accepted', 'Invitation Accepted'),
        ('declined', 'Invitation Declined'),
    ])
    subject = serializers.CharField(max_length=200)
    text_content = serializers.CharField()
    html_content = serializers.CharField()


# Geolocation Serializers
class GeolocationCaptureSerializer(serializers.Serializer):
    """Serializer for capturing geolocation data"""
    attempt_id = serializers.IntegerField()
    latitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    longitude = serializers.DecimalField(max_digits=9, decimal_places=6, required=False, allow_null=True)
    permission_denied = serializers.BooleanField(default=False)
    
    def validate(self, attrs):
        """Validate that coordinates are provided when permission is not denied"""
        permission_denied = attrs.get('permission_denied', False)
        latitude = attrs.get('latitude')
        longitude = attrs.get('longitude')
        
        if not permission_denied:
            if latitude is None or longitude is None:
                raise serializers.ValidationError(
                    "Latitude and longitude are required when permission is granted"
                )
        
        return attrs


class GeolocationDataSerializer(serializers.Serializer):
    """Serializer for geolocation data response"""
    captured = serializers.BooleanField()
    permission_denied = serializers.BooleanField(required=False)
    latitude = serializers.FloatField(required=False, allow_null=True)
    longitude = serializers.FloatField(required=False, allow_null=True)
    captured_at = serializers.DateTimeField(required=False, allow_null=True)
    message = serializers.CharField()
