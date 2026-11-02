from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix='', extra='ignore')
    database_url: str = 'sqlite:///./data/pollinoz_kuban.db'
    offline: bool = False
    forecast_url: str = 'https://api.open-meteo.com/v1/forecast'
    archive_url: str = 'https://archive-api.open-meteo.com/v1/archive'
    clinical_threshold: float = 10.0
    budget_rub: float = 198_000_000
    implementation_cost_rub: float = 900_000
    workers: int = 185
    demo_mode: bool = True
    jwt_secret_key: str = ''
    jwt_secret: str = ''  # legacy demo setting only
    cors_allowed_origins: str = ''
    demo_accounts_enabled: bool = True
    telegram_bot_token: str = ''
    enable_live_telegram: bool = False
    edds_mis_api_key: str = ''
    upload_dir: str = './data/uploads'
    model_path: str = './data/phenology.joblib'

    @model_validator(mode='after')
    def require_production_secrets(self):
        if not self.demo_mode:
            if not self.jwt_secret_key.strip() or not self.edds_mis_api_key.strip():
                raise ValueError('DEMO_MODE=false requires non-empty JWT_SECRET_KEY and EDDS_MIS_API_KEY')
        return self


settings = Settings()
