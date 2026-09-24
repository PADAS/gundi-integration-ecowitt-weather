from app.settings.base import env

ECOWITT_API_BASE_URL = env.str("ECOWITT_API_BASE_URL", "https://api.ecowitt.net")
