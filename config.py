import os

# Try python-decouple first (in requirements.txt)
try:
    from decouple import config
    get_config = config
except ImportError:
    # Fallback to python-dotenv
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    
    # Simple fallback using os.getenv
    def get_config(key, default='', cast=None):
        """Get config from environment variables"""
        value = os.getenv(key, default)
        if cast == bool:
            return str(value).lower() in ('true', '1', 'yes', 'on')
        return value

SECRET_KEY = get_config('SECRET_KEY', default='django-insecure-change-this-in-production')
DEBUG = get_config('DEBUG', default='False', cast=bool)

# Build DATABASE_URL from components or use full URL
DB_NAME = get_config('DB_NAME', 'timetable_db')
DB_USER = get_config('DB_USER', 'timetable_user')
DB_PASSWORD = get_config('DB_PASSWORD', '')
DB_HOST = get_config('DB_HOST', 'localhost')
DB_PORT = get_config('DB_PORT', '5432')

# Construct DATABASE_URL
DATABASE_URL = get_config('DATABASE_URL', 
    default=f'postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}')

ALLOWED_HOSTS_RAW = [h.strip() for h in get_config('ALLOWED_HOSTS', default='localhost,127.0.0.1,128.199.17.132,exams.dashoapp.com,exam.dashoapp.com,timetable.dashoapp.com,dashoapp.com,.exams.dashoapp.com').split(',')]
ALLOWED_HOSTS = ALLOWED_HOSTS_RAW

CORS_ALLOWED_ORIGINS = get_config('CORS_ALLOWED_ORIGINS', 
    default='http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173,http://128.199.17.132,http://exams.dashoapp.com,http://exam.dashoapp.com,https://exams.dashoapp.com,https://exam.dashoapp.com,http://timetable.dashoapp.com,https://timetable.dashoapp.com').split(',')

# AI Configuration - Disabled for low-memory deployment
OPENAI_API_KEY = get_config('OPENAI_API_KEY', default='')
# Prefer GOOGLE_GEMINI_API_KEY and keep GEMINI_API_KEY as backward-compatible fallback.
GOOGLE_GEMINI_API_KEY = get_config('GOOGLE_GEMINI_API_KEY', default='')
GEMINI_API_KEY = GOOGLE_GEMINI_API_KEY or get_config('GEMINI_API_KEY', default='')
GEMINI_MODEL = get_config('GEMINI_MODEL', default='gemini-2.5-flash')
GEMINI_TEMPERATURE = get_config('GEMINI_TEMPERATURE', default='0.7', cast=float)
GEMINI_TOP_P = get_config('GEMINI_TOP_P', default='0.95', cast=float)
GEMINI_MAX_TOKENS = get_config('GEMINI_MAX_TOKENS', default='8192', cast=int)

# Azure OpenAI Configuration
AZURE_OPENAI_API_KEY = get_config('AZURE_OPENAI_API_KEY', default='')
AZURE_OPENAI_ENDPOINT = get_config('AZURE_OPENAI_ENDPOINT', default='')
AZURE_OPENAI_VERSION = get_config('AZURE_OPENAI_VERSION', default='2024-02-15-preview')
AZURE_OPENAI_MODEL_NAME = get_config('AZURE_OPENAI_MODEL_NAME', default='gpt-4o')

USE_OLLAMA = get_config('USE_OLLAMA', default='false')
OLLAMA_BASE_URL = get_config('OLLAMA_BASE_URL', default='http://localhost:11434')

# Mathpix OCR Configuration (for PDF extraction)
MATHPIX_APP_ID = get_config('MATHPIX_APP_ID', default='diracai_cae940_4314c2')
MATHPIX_APP_KEY = get_config('MATHPIX_APP_KEY', default='a884a06e50c036d948bad5a335efa754819b65d80a213c02359a04c7e162358d')

# Email Configuration
EMAIL_HOST = get_config('EMAIL_HOST', default='smtp.gmail.com')
EMAIL_PORT = get_config('EMAIL_PORT', default='587')
EMAIL_USE_TLS = get_config('EMAIL_USE_TLS', default='True', cast=bool)
EMAIL_HOST_USER = get_config('EMAIL_HOST_USER', default='diracai.info@gmail.com')
EMAIL_HOST_PASSWORD = get_config('EMAIL_HOST_PASSWORD', default='fibmduvwoxsjtjvh')
DEFAULT_FROM_EMAIL = get_config('DEFAULT_FROM_EMAIL', default='Exam Flow System <diracai.info@gmail.com>')

# Mailgun Email Configuration
MAILGUN_API_KEY = get_config('MAILGUN_API_KEY', default='')
MAILGUN_DOMAIN = get_config('MAILGUN_DOMAIN', default='sandbox5cb8f0e439cc4e82a4021d2dd56925d8.mailgun.org')

# Redis Configuration
REDIS_PASSWORD = get_config('REDIS_PASSWORD', default='')

# Google Auth Configuration
GOOGLE_CLIENT_ID = get_config('GOOGLE_CLIENT_ID', default='')
