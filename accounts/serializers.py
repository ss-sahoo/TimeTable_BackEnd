from rest_framework import serializers
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone
from datetime import timedelta
from .models import User, Institute, UserPermission, InstituteSettings, InstituteInvitation, DeviceSession, ActivityLog
from .utils import generate_user_code, generate_password, send_credentials_email


class InstituteSerializer(serializers.ModelSerializer):
    user_count = serializers.SerializerMethodField()
    active_user_count = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    
    class Meta:
        model = Institute
        fields = [
            'id', 'name', 'domain', 'description', 'address', 'contact_email', 
            'contact_phone', 'website', 'logo', 'is_active', 'is_verified',
            'created_by', 'created_by_name', 'user_count', 'active_user_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'user_count', 'active_user_count', 'created_by_name']
    
    def get_user_count(self, obj):
        return obj.get_user_count()
    
    def get_active_user_count(self, obj):
        return obj.get_active_user_count()
    
    def get_created_by_name(self, obj):
        return obj.created_by.get_full_name() if obj.created_by else None


class InstituteCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating new institutes"""
    class Meta:
        model = Institute
        fields = [
            'name', 'domain', 'description', 'address', 'contact_email',
            'contact_phone', 'website', 'logo'
        ]
    
    def validate_domain(self, value):
        """Validate that domain is unique if provided"""
        if value and Institute.objects.filter(domain=value).exists():
            raise serializers.ValidationError("An institute with this domain already exists.")
        return value.lower() if value else None
    
    def create(self, validated_data):
        """Create institute and set the creator as super_admin"""
        user = self.context['request'].user
        validated_data['created_by'] = user
        validated_data['is_verified'] = True  # Auto-verify user-created institutes
        institute = Institute.objects.create(**validated_data)
        
        # Update user's institute and role - make them super_admin
        user.institute = institute
        user.role = 'super_admin'
        user.is_staff = True  # Give staff access
        user.save()
        
        # Auto-create 2 default exam patterns for this new institute
        try:
            from patterns.default_patterns import create_default_patterns_for_institute
            create_default_patterns_for_institute(institute, user)
        except Exception:
            pass  # Don't block registration if pattern creation fails
        
        return institute


class InstituteInvitationSerializer(serializers.ModelSerializer):
    institute_name = serializers.CharField(source='institute.name', read_only=True)
    invited_by_name = serializers.CharField(source='invited_by.get_full_name', read_only=True)
    is_expired = serializers.SerializerMethodField()
    
    class Meta:
        model = InstituteInvitation
        fields = [
            'id', 'institute', 'institute_name', 'email', 'role', 'invited_by',
            'invited_by_name', 'status', 'message', 'expires_at', 'is_expired',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'invited_by', 'status', 'created_at', 'updated_at']
        extra_kwargs = {
            'expires_at': {'required': False, 'allow_null': True}
        }
    
    def get_is_expired(self, obj):
        return obj.is_expired()
    
    def create(self, validated_data):
        """Create invitation with default 7-day expiration"""
        validated_data['invited_by'] = self.context['request'].user
        if not validated_data.get('expires_at'):
            validated_data['expires_at'] = timezone.now() + timedelta(days=7)
        return super().create(validated_data)


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, validators=[validate_password])
    password_confirm = serializers.CharField(write_only=True)
    institute_id = serializers.IntegerField(required=False, allow_null=True)
    center_id = serializers.CharField(required=False, allow_null=True)
    role = serializers.CharField(required=False, allow_null=True)

    class Meta:
        model = User
        fields = [
            'email', 'username', 'first_name', 'last_name', 'password', 
            'password_confirm', 'phone', 'institute_id', 'center_id', 'role'
        ]

    def validate(self, attrs):
        if attrs['password'] != attrs['password_confirm']:
            raise serializers.ValidationError("Passwords don't match")
        return attrs

    def create(self, validated_data):
        validated_data.pop('password_confirm')
        # Extract institute_id, center_id and role if provided
        institute_id = validated_data.pop('institute_id', None)
        center_id = validated_data.pop('center_id', None)
        role = validated_data.pop('role', 'student')  # Default to student (lowercase) if not provided

        # ALWAYS normalize role to lowercase to prevent uppercase roles
        role = role.lower() if role else 'student'

        # Resolve center object from tenant DB to avoid cross-DB FK issues
        center = None
        if center_id:
            try:
                from accounts.utils import get_current_db
                from accounts.models import Center
                current_db = get_current_db() or 'default'
                center = Center.objects.using(current_db).get(id=center_id)
            except Exception:
                center = None

        # Create user with institute, center and role
        user = User.objects.create_user(
            **validated_data,
            institute_id=institute_id,
            role=role
        )
        if center:
            user.center = center
            user.save()

        # Record usage if it's a student
        if user.role == 'student' and user.institute:
            from billing.utils import record_usage
            record_usage(user.institute, 'student_onboarding', reference_id=user.id)

        return user


class UserCreationSerializer(serializers.ModelSerializer):
    """
    Serializer for Admins creating users.
    Password is optional (auto-generated if missing).
    """
    password = serializers.CharField(write_only=True, required=False)
    password_confirm = serializers.CharField(write_only=True, required=False)
    institute_id = serializers.IntegerField(required=False, allow_null=True)
    center_id = serializers.CharField(required=False, allow_null=True)
    role = serializers.CharField(required=False, allow_null=True)

    class Meta:
        model = User
        fields = [
            'email', 'username', 'first_name', 'last_name', 'password', 
            'password_confirm', 'phone', 'institute_id', 'center_id', 'role'
        ]

    def validate(self, attrs):
        if attrs.get('password') and attrs.get('password') != attrs.get('password_confirm'):
            raise serializers.ValidationError("Passwords don't match")
        return attrs

    def create(self, validated_data):
        if 'password_confirm' in validated_data:
            validated_data.pop('password_confirm')
        
        # Extract institute_id, center_id and role if provided
        institute_id = validated_data.pop('institute_id', None)
        center_id = validated_data.pop('center_id', None)
        role = validated_data.pop('role', 'student')
        
        # Always use lowercase role
        role = role.lower() if role else 'student'
        
        # Handle password - generate unique password for each user
        password = validated_data.pop('password', None)
        if not password:
            # Generate unique password using utility function
            password = generate_password(role, center_code=None, batch_code=None)
        
        # Create user with institute, center and role
        user = User.objects.create_user(
            username=validated_data.get('username'),
            email=validated_data.get('email'),
            password=password,
            first_name=validated_data.get('first_name'),
            last_name=validated_data.get('last_name'),
            phone=validated_data.get('phone'),
            institute_id=institute_id,
            center_id=center_id,
            role=role
        )
        
        # Send credential email only to the created user
        if user.email:
            send_credentials_email(user, password)
        
        # Store raw password on instance for the response
        user._raw_password = password
        
        # Record usage if it's a student
        if user.role == 'student' and user.institute:
            from billing.utils import record_usage
            record_usage(user.institute, 'student_onboarding', reference_id=user.id)

        return user

    def to_representation(self, instance):
        ret = super().to_representation(instance)
        # If we have a raw password (just created), return it
        if hasattr(instance, '_raw_password'):
            ret['password'] = instance._raw_password
        return ret


class UserSerializer(serializers.ModelSerializer):
    institute = InstituteSerializer(read_only=True)
    institute_id = serializers.IntegerField(read_only=True)  # Add institute_id for frontend
    full_name = serializers.CharField(source='get_full_name', read_only=True)
    center_id = serializers.CharField(required=False, allow_null=True)
    center_name = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            'id', 'email', 'username', 'first_name', 'last_name', 'full_name',
            'role', 'institute', 'institute_id', 'center_id', 'center_name', 
            'phone', 'profile_picture', 'is_verified', 'is_active', 'created_at',
            'teacher_subjects'
        ]
        read_only_fields = ['id', 'email', 'created_at', 'institute_id', 'center_name']
    
    def get_center_id(self, obj):
        """Get center ID - either from direct assignment or from admin_centers"""
        if obj.center_id:
            return str(obj.center_id)
        # Check if user is admin of any center
        admin_center = obj.admin_centers.first()
        if admin_center:
            return str(admin_center.id)
        return None
    
    def get_center_name(self, obj):
        """Get center name - either from direct assignment or from admin_centers"""
        if obj.center:
            return obj.center.name
        # Check if user is admin of any center
        admin_center = obj.admin_centers.first()
        if admin_center:
            return admin_center.name
        return None

    def to_representation(self, instance):
        """Ensure center_id and center_name are correctly populated in output"""
        ret = super().to_representation(instance)
        # Use our custom logic for center_id and center_name to handle fallbacks
        ret['center_id'] = self.get_center_id(instance)
        ret['center_name'] = self.get_center_name(instance)
        return ret

    def update(self, instance, validated_data):
        """Allow updating center_id directly"""
        # Capture center_id if it's in the data, even if it's None/empty
        if 'center_id' in validated_data:
            center_id = validated_data.pop('center_id')
            instance.center_id = center_id if center_id else None
        
        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
            
        instance.save()
        return instance


class UserLoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField()

    def validate(self, attrs):
        email = attrs.get('email')
        password = attrs.get('password')

        if email and password:
            user = authenticate(username=email, password=password)
            if not user:
                raise serializers.ValidationError('Invalid credentials')
            if not user.is_active:
                raise serializers.ValidationError('User account is disabled')
            attrs['user'] = user
        else:
            raise serializers.ValidationError('Must include email and password')

        return attrs


class UserPermissionSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)
    granted_by = UserSerializer(read_only=True)

    class Meta:
        model = UserPermission
        fields = ['id', 'user', 'permission_type', 'granted_by', 'granted_at', 'expires_at', 'is_active']


class InstituteSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstituteSettings
        fields = '__all__'
        read_only_fields = ['institute']


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField()
    new_password = serializers.CharField(validators=[validate_password])
    new_password_confirm = serializers.CharField()

    def validate(self, attrs):
        if attrs['new_password'] != attrs['new_password_confirm']:
            raise serializers.ValidationError("New passwords don't match")
        return attrs

    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError("Old password is incorrect")
        return value



class DeviceSessionSerializer(serializers.ModelSerializer):
    """
    Serializer for DeviceSession model.
    
    **Feature: exam-security-enhancements, Property 3: Device information completeness**
    **Validates: Requirements 1.3**
    """
    user_email = serializers.EmailField(source='user.email', read_only=True)
    login_timestamp = serializers.DateTimeField(source='created_at', read_only=True)
    
    class Meta:
        model = DeviceSession
        fields = [
            'id',
            'user',
            'user_email',
            'device_fingerprint',
            'device_type',
            'browser',
            'os',
            'screen_resolution',
            'timezone',
            'ip_address',
            'user_agent',
            'is_active',
            'last_activity',
            'login_timestamp',
            'created_at',
            'expires_at'
        ]
        read_only_fields = [
            'id',
            'user',
            'user_email',
            'is_active',
            'last_activity',
            'login_timestamp',
            'created_at',
            'expires_at'
        ]


class DeviceCheckRequestSerializer(serializers.Serializer):
    """Serializer for device check request"""
    user_agent = serializers.CharField()
    screen_resolution = serializers.CharField()
    timezone = serializers.CharField()
    device_type = serializers.CharField()
    browser = serializers.CharField()
    os = serializers.CharField()
    ip_address = serializers.IPAddressField(required=False)


class DeviceCheckResponseSerializer(serializers.Serializer):
    """
    Serializer for device check response.
    
    **Feature: exam-security-enhancements, Property 3: Device information completeness**
    **Validates: Requirements 1.3**
    """
    has_conflict = serializers.BooleanField()
    conflict_info = serializers.DictField(required=False, allow_null=True)
    device_fingerprint = serializers.CharField()
    
    def to_representation(self, instance):
        """
        Ensure conflict_info contains all required fields when present.
        
        **Feature: exam-security-enhancements, Property 3: Device information completeness**
        **Validates: Requirements 1.3**
        """
        data = super().to_representation(instance)
        
        # Validate that conflict_info has all required fields
        if data.get('has_conflict') and data.get('conflict_info'):
            conflict_info = data['conflict_info']
            required_fields = ['device_type', 'browser', 'login_timestamp', 'last_activity']
            
            for field in required_fields:
                if field not in conflict_info:
                    raise serializers.ValidationError(
                        f"Conflict info missing required field: {field}"
                    )
        
        return data


class LogoutDeviceRequestSerializer(serializers.Serializer):
    """Serializer for logout device request"""
    device_fingerprint = serializers.CharField()


class ActivityLogSerializer(serializers.ModelSerializer):
    user_name = serializers.CharField(source='user.get_full_name', read_only=True)
    user_email = serializers.EmailField(source='user.email', read_only=True)
    log_type_display = serializers.CharField(source='get_log_type_display', read_only=True)

    class Meta:
        model = ActivityLog
        fields = [
            'id', 'log_type', 'log_type_display', 'title', 'description', 
            'timestamp', 'user', 'user_name', 'user_email', 'status', 
            'metadata', 'ip_address'
        ]
