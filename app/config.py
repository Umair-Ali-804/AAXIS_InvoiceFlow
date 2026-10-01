"""Runtime configuration, read from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader so the project has no hard dependency on python-dotenv."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    app_name: str = "AAXIS InvoiceFlow"
    company_name: str = os.getenv("COMPANY_NAME", "Harbor & Pine Property Management")
    base_currency: str = os.getenv("BASE_CURRENCY", "USD")

    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", BASE_DIR / "data")).resolve())

    # Review policy
    confidence_threshold: float = _f("CONFIDENCE_THRESHOLD", 0.85)
    math_tolerance: float = _f("MATH_TOLERANCE", 0.02)
    second_approval_limit: float = _f("SECOND_APPROVAL_LIMIT", 10000)
    duplicate_window_days: int = int(_f("DUPLICATE_WINDOW_DAYS", 10))

    # Optional LLM extraction (rules + OCR work fully offline without these)
    llm_provider: str = os.getenv("LLM_PROVIDER", "").lower()  # "anthropic" | "openai" | ""
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "")

    # QuickBooks Online (sandbox). Without credentials the built-in simulator is used.
    qbo_client_id: str = os.getenv("QBO_CLIENT_ID", "")
    qbo_client_secret: str = os.getenv("QBO_CLIENT_SECRET", "")
    qbo_realm_id: str = os.getenv("QBO_REALM_ID", "")
    qbo_refresh_token: str = os.getenv("QBO_REFRESH_TOKEN", "")
    qbo_redirect_uri: str = os.getenv("QBO_REDIRECT_URI", "http://localhost:8000/integrations/quickbooks/callback")
    qbo_environment: str = os.getenv("QBO_ENVIRONMENT", "sandbox")
    qbo_expense_account_id: str = os.getenv("QBO_EXPENSE_ACCOUNT_ID", "")
    qbo_tax_account_id: str = os.getenv("QBO_TAX_ACCOUNT_ID", "")

    # Email ingestion (IMAP, e.g. a Gmail ap@ inbox with an app password)
    imap_host: str = os.getenv("IMAP_HOST", "")
    imap_user: str = os.getenv("IMAP_USER", "")
    imap_password: str = os.getenv("IMAP_PASSWORD", "")
    imap_folder: str = os.getenv("IMAP_FOLDER", "INBOX")

    tesseract_cmd: str = os.getenv("TESSERACT_CMD", "")

    # Hosting (e.g. Render free plan)
    # ACCESS_PASSWORD: when set, the whole app asks for this password (browser sign-in box, any username).
    access_password: str = os.getenv("ACCESS_PASSWORD", "")
    # AUTO_LOAD_DEMO: load the 5 demo invoices when the database is empty (free hosts reset their disk).
    auto_load_demo: bool = os.getenv("AUTO_LOAD_DEMO", "0").strip().lower() in {"1", "true", "yes"}

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def pages_dir(self) -> Path:
        return self.data_dir / "pages"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "invoiceflow.db"

    @property
    def llm_enabled(self) -> bool:
        if self.llm_provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.llm_provider == "openai":
            return bool(self.openai_api_key)
        return False

    @property
    def qbo_configured(self) -> bool:
        return bool(self.qbo_client_id and self.qbo_client_secret)

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.uploads_dir, self.pages_dir):
            d.mkdir(parents=True, exist_ok=True)


settings = Settings()
