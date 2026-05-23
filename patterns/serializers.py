from rest_framework import serializers
from .models import Subject, ExamPattern, PatternSection, PatternTemplate


class SubjectSerializer(serializers.ModelSerializer):
    class Meta:
        model = Subject
        fields = ['id', 'name', 'description', 'is_active', 'created_at', 'updated_at']


class PatternSectionSerializer(serializers.ModelSerializer):
    total_questions_in_section = serializers.ReadOnlyField()
    total_marks_in_section = serializers.ReadOnlyField()
    subject_name = serializers.ReadOnlyField(source='subject')
    marking_scheme = serializers.SerializerMethodField(read_only=True)
    questions_added = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = PatternSection
        fields = [
            'id', 'name', 'subject', 'subject_name', 'question_type', 
            'start_question', 'end_question', 'marks_per_question', 'negative_marking', 
            'min_questions_to_attempt', 'is_compulsory', 'order', 
            'total_questions_in_section', 'total_marks_in_section', 'marking_scheme',
            'questions_added', 'question_configurations'
        ]

    def get_questions_added(self, obj):
        """Get the count of questions added to this section for the current exam"""
        # Get exam_id from context if available
        request = self.context.get('request')
        exam_id = None
        
        if request:
            # Try to get exam_id from query params or view kwargs
            exam_id = request.query_params.get('exam_id')
            if not exam_id and hasattr(request, 'parser_context'):
                exam_id = request.parser_context.get('kwargs', {}).get('exam_id')
        
        # Also check if exam is in context directly
        if not exam_id:
            exam_id = self.context.get('exam_id')
        
        if exam_id:
            from questions.models import Question
            return Question.objects.filter(
                exam_id=exam_id,
                pattern_section_id=obj.id,
                is_active=True
            ).count()
        
        return None

    def get_marking_scheme(self, obj):
        """Convert flat fields to marking_scheme object for frontend"""
        negative_marking_percentage = 0
        if obj.marks_per_question > 0 and obj.negative_marking > 0:
            negative_marking_percentage = (float(obj.negative_marking) / float(obj.marks_per_question)) * 100
        
        return {
            'max_marks': obj.marks_per_question,
            'negative_marking_percentage': round(negative_marking_percentage, 2),
            'partial_marking': obj.question_type == 'mcq',
            'marks_per_correct_option': 0,
            'tolerance_range': 0,
            'decimal_precision': 2,
            'manual_grading': obj.question_type == 'subjective'
        }

    def validate(self, attrs):
        if attrs.get('start_question', 0) >= attrs.get('end_question', 0):
            raise serializers.ValidationError("Start question must be less than end question")
        return attrs


class ExamPatternSerializer(serializers.ModelSerializer):
    sections = serializers.SerializerMethodField()
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    institute_name = serializers.CharField(source='institute.name', read_only=True)

    class Meta:
        model = ExamPattern
        fields = [
            'id', 'name', 'description', 'institute', 'institute_name',
            'total_questions', 'total_duration', 'total_marks', 'is_active',
            'created_by', 'created_by_name', 'sections', 'exam_mode', 'omr_config',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_by', 'created_at', 'updated_at']

    def get_sections(self, obj):
        """Serialize sections with exam_id context for question counts"""
        sections = obj.sections.all()
        return PatternSectionSerializer(sections, many=True, context=self.context).data

    def validate(self, attrs):
        # Validate that sections don't overlap WITHIN THE SAME SUBJECT
        # With subject-wise numbering, different subjects can have same question numbers
        sections = self.initial_data.get('sections', [])
        if sections:
            # Group sections by subject
            subject_sections = {}
            for section in sections:
                subject = section.get('subject', '')
                if subject not in subject_sections:
                    subject_sections[subject] = []
                subject_sections[subject].append(section)
            
            # Check for overlaps within each subject
            for subject, subject_section_list in subject_sections.items():
                covered_questions = set()
                for section in subject_section_list:
                    start = section['start_question']
                    end = section['end_question']
                    
                    # Check for overlaps within this subject
                    section_questions = set(range(start, end + 1))
                    if covered_questions.intersection(section_questions):
                        raise serializers.ValidationError(
                            f"Question ranges cannot overlap within subject '{subject}'"
                        )
                    
                    covered_questions.update(section_questions)
        
        return attrs


class PatternSectionCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating sections with marking_scheme support"""
    id = serializers.IntegerField(required=False)  # Allow ID for updates
    marking_scheme = serializers.DictField(required=False, write_only=True)
    question_type = serializers.CharField()  # Override to bypass choice validation before custom mapping
    
    class Meta:
        model = PatternSection
        fields = [
            'id', 'name', 'subject', 'question_type', 'start_question', 'end_question',
            'min_questions_to_attempt', 'is_compulsory', 'order', 'marking_scheme',
            'marks_per_question', 'negative_marking', 'question_configurations'
        ]
        extra_kwargs = {
            'marks_per_question': {'required': False},
            'negative_marking': {'required': False},
            'question_configurations': {'required': False, 'default': dict}
        }

    def validate_question_type(self, value):
        """Convert frontend question type names to backend format"""
        type_mapping = {
            'Single Correct MCQ': 'single_mcq',
            'Multiple Correct MCQ': 'multiple_mcq',
            'Numerical': 'numerical',
            'Subjective': 'subjective',
            'True/False': 'true_false',
            'Fill in the Blanks': 'fill_blank',
            'single_mcq': 'single_mcq',
            'multiple_mcq': 'multiple_mcq',
            'mcq': 'single_mcq',
            'numerical': 'numerical',
            'subjective': 'subjective',
            'true_false': 'true_false',
            'fill_blank': 'fill_blank'
        }
        return type_mapping.get(value, value.lower() if hasattr(value, 'lower') else value)

    def validate(self, attrs):
        # Extract marking scheme and convert to flat fields
        marking_scheme = attrs.pop('marking_scheme', None)
        
        if marking_scheme:
            max_marks = marking_scheme.get('max_marks', 4)
            negative_percentage = marking_scheme.get('negative_marking_percentage', 0)
            
            # Calculate negative marking from percentage
            negative_marking = (max_marks * negative_percentage) / 100 if negative_percentage > 0 else 0
            
            attrs['marks_per_question'] = max_marks
            attrs['negative_marking'] = round(negative_marking, 2)
        else:
            # Set defaults if not present
            if 'marks_per_question' not in attrs:
                attrs['marks_per_question'] = 4
            if 'negative_marking' not in attrs:
                attrs['negative_marking'] = 1.0

        # Subjective questions should have no negative marking
        if attrs.get('question_type') == 'subjective':
            attrs['negative_marking'] = 0
        
        # Validate question range
        if attrs.get('start_question', 0) >= attrs.get('end_question', 0):
            raise serializers.ValidationError("Start question must be less than end question")
        
        return attrs


class ExamPatternCreateSerializer(serializers.ModelSerializer):
    sections = PatternSectionCreateSerializer(many=True)

    class Meta:
        model = ExamPattern
        fields = [
            'name', 'description', 'total_questions', 'total_duration', 
            'total_marks', 'exam_mode', 'omr_config', 'sections'
        ]

    def create(self, validated_data):
        sections_data = validated_data.pop('sections')
        pattern = ExamPattern.objects.create(**validated_data)
        
        for section_data in sections_data:
            # Remove id if present in creation
            section_data.pop('id', None)
            PatternSection.objects.create(pattern=pattern, **section_data)
        
        return pattern

    def update(self, instance, validated_data):
        sections_data = validated_data.pop('sections', None)
        
        # Update pattern fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if sections_data is not None:
            # Get existing sections
            existing_sections = {section.id: section for section in instance.sections.all()}
            updated_section_ids = []
            
            for section_data in sections_data:
                section_id = section_data.get('id')
                if section_id and section_id in existing_sections:
                    # Update existing section
                    section_instance = existing_sections[section_id]
                    for attr, value in section_data.items():
                        if attr != 'id':
                            setattr(section_instance, attr, value)
                    section_instance.save()
                    updated_section_ids.append(section_id)
                else:
                    # Create new section
                    # Remove id if it's a new section (might be 0 or null from frontend)
                    section_data.pop('id', None)
                    new_section = PatternSection.objects.create(pattern=instance, **section_data)
                    updated_section_ids.append(new_section.id)
            
            # Delete sections that were not included in the update
            instance.sections.exclude(id__in=updated_section_ids).delete()
            
        return instance


class PatternTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = PatternTemplate
        fields = '__all__'
