from flask_limiter import Limiter
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf import CSRFProtect
from sqlalchemy import MetaData

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)
db = SQLAlchemy(metadata=metadata)
migrate = Migrate()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Entre para continuar."
csrf = CSRFProtect()


def limit_key() -> str:
    from flask import request
    from flask_login import current_user

    user_id = current_user.get_id() if current_user.is_authenticated else "anonymous"
    return f"{user_id}:{request.remote_addr or 'unknown'}"


def sensitive_request_limit() -> str:
    from flask import current_app
    from flask_login import current_user

    default = int(current_app.config["SENSITIVE_REQUEST_LIMIT_DEFAULT"])
    configured = (
        getattr(current_user, "request_limit_per_10_minutes", default)
        if current_user.is_authenticated
        else default
    )
    return f"{min(100, max(10, int(configured or default)))} per 10 minutes"


limiter = Limiter(key_func=limit_key, default_limits=[], storage_uri="memory://")
