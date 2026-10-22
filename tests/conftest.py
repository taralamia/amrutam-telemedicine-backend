import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-long-enough-for-local-tests")
os.environ.setdefault("DATA_ENCRYPTION_KEY", "test-encryption-secret-long-enough-for-tests")
os.environ.setdefault("PAYMENT_WEBHOOK_SECRET", "test-payment-webhook-secret-long-enough")
