import os

# Set before the app is imported: it reads its settings once at import.
os.environ.setdefault("GAEB_API_KEY", "test-key, next-key")
