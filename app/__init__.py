# app/__init__.py
import os
from flask import Flask
from .services.gcs import GcsStore

def create_app():
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")

    # env
    app.config["GCS_BUCKET"] = os.environ["GCS_BUCKET"]
    app.config["USER_ID"] = os.environ.get("USER_ID", "default")

    # shared store
    app.gcs = GcsStore(app.config["GCS_BUCKET"])

    # register blueprints
    from .blueprints.onboarding import bp as onboarding_bp
    from .blueprints.plan import bp as plan_bp
    app.register_blueprint(onboarding_bp)
    app.register_blueprint(plan_bp)

    return app

# expose a module-level app for Gunicorn/Flask
app = create_app()
