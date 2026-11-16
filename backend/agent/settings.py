"""Configuration with repository-root .env values taking precedence."""
from dataclasses import dataclass
import os

from ..config import load_environment


@dataclass(frozen=True)
class Settings:
    database_url: str | None = None
    openai_api_key: str | None = None
    model: str | None = None
    max_tool_calls: int = 6
    max_rows: int = 100
    max_output_tokens: int = 2500
    llm_timeout_seconds: float = 25
    request_timeout_seconds: float = 120
    session_database_url: str | None = None
    google_places_api_key: str | None = None
    google_maps_browser_api_key: str | None = None
    independent_poi_url: str | None = None
    independent_poi_attribution: str | None = None
    independent_poi_user_agent: str | None = None

    @classmethod
    def from_env(cls):
        load_environment()
        return cls(database_url=os.getenv('AGENT_DATABASE_URL'),
                   google_places_api_key=os.getenv('GOOGLE_PLACES_API_KEY'),
                   google_maps_browser_api_key=os.getenv('GOOGLE_MAPS_BROWSER_API_KEY'),
                   independent_poi_url=os.getenv('CITYLENS_INDEPENDENT_SEARCH_URL'),
                   independent_poi_attribution=os.getenv('CITYLENS_INDEPENDENT_SEARCH_ATTRIBUTION'),
                   independent_poi_user_agent=os.getenv('CITYLENS_INDEPENDENT_SEARCH_USER_AGENT'),
                   session_database_url=os.getenv('SESSION_DATABASE_URL'),
                   openai_api_key=os.getenv('OPENAI_API_KEY'), model=os.getenv('OPENAI_MODEL'),
                   max_tool_calls=int(os.getenv('AGENT_MAX_TOOL_CALLS','6')),
                   max_rows=int(os.getenv('AGENT_MAX_ROWS','100')),
                   max_output_tokens=int(os.getenv('AGENT_MAX_OUTPUT_TOKENS','2500')),
                   llm_timeout_seconds=float(os.getenv('AGENT_LLM_TIMEOUT_SECONDS','25')),
                   request_timeout_seconds=float(os.getenv('AGENT_REQUEST_TIMEOUT_SECONDS','120')))

    def __post_init__(self):
        if not (1 <= self.max_tool_calls <= 6 and 1 <= self.max_rows <= 1000
                and 500 <= self.max_output_tokens <= 8000
                and 1 <= self.llm_timeout_seconds <= 60
                and 5 <= self.request_timeout_seconds <= 180):
            raise ValueError('Agent budget settings outside allowed ranges')
