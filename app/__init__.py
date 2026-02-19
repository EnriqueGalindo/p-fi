import os
import time
from flask import Flask, session
from .services.gcs import GcsStore
from .services.utils import user_id_for_email, canonicalize_email



def create_app():
    app = Flask(__name__)

    # --- Security / session cookie config ---
    # True on Cloud Run (K_SERVICE is set), False locally (HTTP).
    is_cloud = bool(os.getenv("K_SERVICE"))
    app.config.update(
        SECRET_KEY=os.getenv("FLASK_SECRET", "dev-secret"),  # same as your current secret
        SESSION_COOKIE_SECURE=is_cloud,   # send only over HTTPS in prod
        SESSION_COOKIE_SAMESITE="Lax",    # blocks most CSRF cross-site sends
        SESSION_COOKIE_HTTPONLY=True,     # JS can't read the cookie
        # optional: give your cookie a custom name, and/or lifetime
        # SESSION_COOKIE_NAME="pf_session",
        # PERMANENT_SESSION_LIFETIME=60*60*24*30,  # 30 days
    )

    app.config["GCS_BUCKET"] = os.environ["GCS_BUCKET"]
    app.config["USER_ID"]    = os.environ.get("USER_ID", "default")

    app.config["SYS_ADMIN_BUCKET"] = os.environ.get("SYS_ADMIN_BUCKET", "gmoney_sys_admin")
    app.config["TYPE_CONFIG_PATH"] = os.environ.get("TYPE_CONFIG_PATH", "type_config.json")

    app.config["AUTH_DISABLED"] = os.getenv("AUTH_DISABLED", "").lower() in ("1", "true", "yes")
    app.config["DEV_EMAIL"] = os.getenv("DEV_EMAIL", "dev@gmoney.me")

    if app.config["AUTH_DISABLED"]:
        @app.before_request
        def _inject_dev_session():
            # create a fake, consistent user once per browser session
            if "user_id" not in session:
                email = canonicalize_email(app.config["DEV_EMAIL"])
                uid = user_id_for_email(email)
                session.update({
                    "user_email": email,
                    "user_id": uid,
                    "auth_at": int(time.time()),
                })

    # shared store
    app.gcs = GcsStore(app.config["GCS_BUCKET"])
    app.config_store = GcsStore(app.config["SYS_ADMIN_BUCKET"])

    # register blueprints
    from .blueprints.onboarding import bp as onboarding_bp
    from .blueprints.plan        import bp as plan_bp
    from .blueprints.ledger      import bp as ledger_bp
    from .blueprints.auth        import bp as auth_bp
    from .blueprints.ledger_upload import bp as ledger_upload
    from .blueprints.receipt import bp as receipts_bp
    from .blueprints.rental_admin import bp as rental_admin_bp

    from .blueprints.rental_tenant import bp as rental_tenant_bp
    from .blueprints.stripe import bp as stripe_webhook_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(ledger_bp)
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(plan_bp)
    app.register_blueprint(ledger_upload)
    app.register_blueprint(receipts_bp)
    app.register_blueprint(rental_admin_bp)

    app.register_blueprint(rental_tenant_bp)
    app.register_blueprint(stripe_webhook_bp)

    return app

# expose a module-level app for Gunicorn/Flask
app = create_app()
