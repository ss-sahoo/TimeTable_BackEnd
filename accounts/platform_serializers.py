from rest_framework import serializers
from .models import Institute, User

class PlatformInstituteSerializer(serializers.ModelSerializer):
    """
    Detailed institute view for Platform Owners, including super admin info.
    """
    super_admin = serializers.SerializerMethodField()
    user_count = serializers.IntegerField(source='get_user_count', read_only=True)
    active_user_count = serializers.IntegerField(source='get_active_user_count', read_only=True)
    center_count = serializers.IntegerField(source='centers.count', read_only=True)

    class Meta:
        model = Institute
        fields = [
            'id', 'name', 'subdomain', 'domain', 'description', 'address', 
            'contact_email', 'contact_phone', 'website', 'is_verified',
            'is_active', 'created_at', 'super_admin', 'user_count', 
            'active_user_count', 'center_count', 'db_name'
        ]

    def get_super_admin(self, obj):
        # find the creator or one of the super admins
        admin = User.objects.filter(institute=obj, role='super_admin').first()
        if admin:
            return {
                'id': admin.id,
                'full_name': admin.get_full_name(),
                'email': admin.email,
                'username': admin.username
            }
        return None

class CreateInstituteWithAdminSerializer(serializers.Serializer):
    """
    Atomic payload to create both an institute and its first super admin.
    """
    # Institute details
    name = serializers.CharField(max_length=255)
    subdomain = serializers.SlugField(max_length=100, required=False, allow_blank=True, help_text="URL-safe slug, e.g. 'iitmadras'")
    domain = serializers.CharField(max_length=100, required=False, allow_blank=True)
    contact_email = serializers.EmailField()
    
    # Super Admin details
    admin_email = serializers.EmailField()
    admin_username = serializers.CharField(max_length=150)
    admin_first_name = serializers.CharField(max_length=150)
    admin_last_name = serializers.CharField(max_length=150)
    admin_password = serializers.CharField(write_only=True)

class PlatformUserSerializer(serializers.ModelSerializer):
    """
    User view for Platform Owners, including institute name.
    """
    institute_name = serializers.CharField(source='institute.name', read_only=True)
    full_name = serializers.CharField(source='get_full_name', read_only=True)

    class Meta:
        model = User
        fields = [
            'id', 'email', 'username', 'full_name', 'first_name', 'last_name', 
            'role', 'institute', 'institute_name', 'is_active', 'created_at'
        ]
