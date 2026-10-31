import os
class S:
    database_url = os.getenv("DATABASE_URL") or "sqlite:///./dmg.db"
    # Hosts (Render, Railway, Heroku) hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the driver named.
    if database_url.startswith("postgres://"): database_url = "postgresql+psycopg2://" + database_url[len("postgres://"):]
    elif database_url.startswith("postgresql://"): database_url = "postgresql+psycopg2://" + database_url[len("postgresql://"):]
    secret_key = os.getenv("SECRET_KEY", "change-me")
    google_client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    allowed_domain = os.getenv("ALLOWED_DOMAIN", "")      # e.g. yourcompany.com (blank = any Google account listed in Users)
    admin_emails = [e.strip().lower() for e in os.getenv("ADMIN_EMAIL", "").split(",") if e.strip()]   # one or more, comma separated; created on first start
    admin_email = admin_emails[0] if admin_emails else ""
    https_only = os.getenv("HTTPS_ONLY", "false").lower() == "true"
    dev_mode = os.getenv("DEV_MODE", "false").lower() == "true"  # NEVER true in production
    timezone = os.getenv("TIMEZONE", "Asia/Kolkata")
settings = S()
