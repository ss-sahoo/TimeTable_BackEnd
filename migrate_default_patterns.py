"""
One-time script to update existing default patterns for all institutes.
Renames JEE pattern and adds UPSC pattern.
"""
import os
import django
import sys

sys.path.append(os.getcwd())
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'exam_flow_backend.settings')
django.setup()

from patterns.models import ExamPattern
from accounts.models import Institute, User
from patterns.default_patterns import create_default_patterns_for_institute

def migration():
    print("Starting migration of default patterns...")
    
    # 1. Rename existing JEE patterns that are marked as default
    updated_count = ExamPattern.objects.filter(
        is_default=True, 
        name="JEE-Style Full Test (90 Questions)"
    ).update(name="NTA JEE Mains Official Pattern (90 Qs)")
    
    print(f"Updated {updated_count} existing JEE patterns.")
    
    # 2. Add UPSC pattern for all institutes that don't have it yet
    institutes = Institute.objects.all()
    for inst in institutes:
        creator = inst.get_admins().first() or User.objects.filter(is_superuser=True).first()
        if creator:
            create_default_patterns_for_institute(inst, creator)
            
    print("Migration complete.")

if __name__ == '__main__':
    migration()
