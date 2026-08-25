from dotenv import load_dotenv, find_dotenv

# Load .env from project root if present; do not override existing environment variables.
load_dotenv(find_dotenv(), override=False)

# Expose package API as normal (module-level imports live in other modules).
