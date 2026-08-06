import os

import pytest
from hypothesis import HealthCheck, settings as hyp_settings
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
import app.models  # noqa: F401  (registers tables on Base.metadata)

# --- Hypothesis profiles -----------------------------------------------------------------
# Property tests build a fresh in-memory database per generated example, so example count is
# the main cost lever. `dev` keeps the default suite fast; `deep` is the real search — run it
# when touching the calculation engine, or on a schedule:
#     HYPOTHESIS_PROFILE=deep pytest tests/test_calculation_properties.py
_COMMON = dict(deadline=None,
               suppress_health_check=[HealthCheck.too_slow,
                                      HealthCheck.function_scoped_fixture])
hyp_settings.register_profile("dev", max_examples=50, **_COMMON)
hyp_settings.register_profile("deep", max_examples=750, **_COMMON)
hyp_settings.register_profile("ci", max_examples=150, **_COMMON)
hyp_settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture
def db():
    """A fresh in-memory SQLite session per test."""
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
