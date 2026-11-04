"""Package init: load secrets from the project-root .env file (never hard-coded, never printed)."""
from pathlib import Path

try:
    from dotenv import load_dotenv
    # override=False: real environment variables (e.g. in CI) win over the .env file
    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:      # python-dotenv not installed: fall back to plain environment variables
    pass
