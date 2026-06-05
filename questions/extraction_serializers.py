"""
Serializers for question extraction API
"""
from rest_framework import serializers
from questions.models import ExtractionJob, ExtractedQuestion


class ExtractionJobSerializer(serializers.ModelSerializer):
    """Serializer for ExtractionJob model"""
    
    exam_title = serializers.CharField(source='exam.title', read_only=True)
    pattern_name = serializers.CharField(source='pattern.name', read_only=True)
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    success_rate = serializers.FloatField(read_only=True)
    
    class Meta:
        model = ExtractionJob
        fields = [
            'id',
            'exam',
            'exam_title',
            'pattern',
            'pattern_name',
            'created_by',
            'created_by_name',
            'file_name',
            'file_type',
            'file_size',
            'status',
            'progress_percent',
            'total_questions_found',
            'questions_extracted',
            'questions_imported',
            'questions_failed',
            'ai_model_used',
            'tokens_used',
            'processing_time_seconds',
            'error_message',
            'retry_count',
            'success_rate',
            'created_at',
            'completed_at',
        ]
        read_only_fields = [
            'id',
            'created_by',
            'status',
            'progress_percent',
            'total_questions_found',
            'questions_extracted',
            'questions_imported',
            'questions_failed',
            'ai_model_used',
            'tokens_used',
            'processing_time_seconds',
            'error_message',
            'retry_count',
            'created_at',
            'completed_at',
        ]


class ExtractedQuestionSerializer(serializers.ModelSerializer):
    """Serializer for ExtractedQuestion model"""
    
    job_id = serializers.UUIDField(source='job.id', read_only=True)
    
    class Meta:
        model = ExtractedQuestion
        fields = [
            'id',
            'job',
            'job_id',
            'question_text',
            'question_type',
            'options',
            'correct_answer',
            'solution',
            'explanation',
            'difficulty',
            'confidence_score',
            'requires_review',
            'suggested_subject',
            'suggested_section_id',
            'assigned_subject',
            'assigned_section_id',
            'is_validated',
            'is_imported',
            'import_error',
            'imported_question',
            'created_at',
        ]
        read_only_fields = [
            'id',
            'job',
            'job_id',
            'confidence_score',
            'suggested_subject',
            'suggested_section_id',
            'is_validated',
            'is_imported',
            'import_error',
            'imported_question',
            'created_at',
        ]
    
    def validate(self, data):
        """Validate extracted question data"""
        # Validate question type
        valid_types = [
            'single_mcq',
            'multiple_mcq',
            'numerical',
            'subjective',
            'true_false',
            'fill_blank'
        ]
        
        question_type = data.get('question_type')
        if question_type and question_type not in valid_types:
            raise serializers.ValidationError({
                'question_type': f'Invalid question type. Must be one of: {", ".join(valid_types)}'
            })
        
        # Validate MCQ options
        if question_type in ['single_mcq', 'multiple_mcq']:
            options = data.get('options', [])
            if not options or len(options) < 2:
                raise serializers.ValidationError({
                    'options': 'MCQ questions must have at least 2 options'
                })
        
        return data


class ExtractionJobCreateSerializer(serializers.Serializer):
    """Serializer for creating extraction job"""

    file = serializers.FileField(required=True)
    exam_id = serializers.IntegerField(required=True)
    pattern_id = serializers.IntegerField(required=True)
    subject = serializers.CharField(required=False, allow_blank=True)

    # Magic-byte signatures for accepted binary formats. The declared
    # content_type header is client-supplied and easy to spoof, so for
    # binary types we also peek at the first bytes and reject mismatches.
    _MAGIC_BYTES = {
        'application/pdf': (b'%PDF-',),
        'image/jpeg': (b'\xff\xd8\xff',),
        'image/png': (b'\x89PNG\r\n\x1a\n',),
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document': (b'PK\x03\x04',),
        'application/msword': (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1', b'PK\x03\x04'),
    }

    def validate_file(self, value):
        """Validate uploaded file"""
        from django.conf import settings

        # Check file size
        if value.size > settings.MAX_UPLOAD_SIZE:
            raise serializers.ValidationError(
                f'File size exceeds maximum allowed size of '
                f'{settings.MAX_UPLOAD_SIZE / 1024 / 1024:.0f} MB'
            )

        # Check file type
        content_type = value.content_type
        if content_type not in settings.ALLOWED_EXTRACTION_FILE_TYPES:
            raise serializers.ValidationError(
                f'File type {content_type} is not supported. '
                f'Allowed types: {", ".join(settings.EXTRACTION_FILE_EXTENSIONS)}'
            )

        # Magic-byte check for binary formats. text/plain is skipped because
        # plain text has no signature.
        expected_signatures = self._MAGIC_BYTES.get(content_type)
        if expected_signatures:
            try:
                value.seek(0)
                head = value.read(16)
                value.seek(0)
            except Exception:
                head = b''
            if not any(head.startswith(sig) for sig in expected_signatures):
                raise serializers.ValidationError(
                    'File contents do not match the declared type. '
                    'Re-upload the original file.'
                )

        return value
    
    def validate_exam_id(self, value):
        """Validate exam exists"""
        from exams.models import Exam
        
        try:
            Exam.objects.get(id=value)
        except Exam.DoesNotExist:
            raise serializers.ValidationError(f'Exam with ID {value} does not exist')
        
        return value
    
    def validate_pattern_id(self, value):
        """Validate pattern exists"""
        from patterns.models import ExamPattern
        
        try:
            ExamPattern.objects.get(id=value)
        except ExamPattern.DoesNotExist:
            raise serializers.ValidationError(f'Pattern with ID {value} does not exist')
        
        return value


class BulkImportSerializer(serializers.Serializer):
    """Serializer for bulk import request"""
    
    job_id = serializers.UUIDField(required=True)
    question_ids = serializers.ListField(
        child=serializers.IntegerField(),
        required=True,
        allow_empty=False
    )
    mappings = serializers.ListField(
        child=serializers.DictField(),
        required=True,
        allow_empty=False
    )
    
    def validate_mappings(self, value):
        """Validate mapping structure"""
        required_fields = ['extracted_question_id', 'subject', 'question_number']
        
        for i, mapping in enumerate(value):
            for field in required_fields:
                if field not in mapping:
                    raise serializers.ValidationError(
                        f'Mapping at index {i} is missing required field: {field}'
                    )
        
        return value
    
    def validate(self, data):
        """Validate that question_ids match mappings"""
        question_ids = set(data['question_ids'])
        mapping_ids = set(m['extracted_question_id'] for m in data['mappings'])
        
        if question_ids != mapping_ids:
            raise serializers.ValidationError(
                'question_ids and mapping IDs must match'
            )
        
        return data


class ExtractionStatusSerializer(serializers.Serializer):
    """Serializer for extraction status response"""
    
    job_id = serializers.UUIDField()
    status = serializers.CharField()
    progress_percent = serializers.IntegerField()
    total_questions_found = serializers.IntegerField()
    questions_extracted = serializers.IntegerField()
    estimated_time_remaining = serializers.IntegerField(required=False)
    error_message = serializers.CharField(required=False, allow_blank=True)
