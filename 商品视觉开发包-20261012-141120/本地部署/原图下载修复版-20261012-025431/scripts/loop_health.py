import sys
from sqlalchemy import create_engine, text
from app.core.config import get_settings

engine = create_engine(get_settings().database_url.get_secret_value())
try:
    with engine.connect() as conn:
        good = conn.scalar(text("SELECT last_safe_error_code IS NULL AND successful_iteration_count > 0 AND last_heartbeat_at > clock_timestamp() - interval '90 seconds' FROM operations_loop_heartbeats WHERE component=:component"), {'component': sys.argv[1]})
    raise SystemExit(0 if good else 1)
except Exception:
    raise SystemExit(1)
finally:
    engine.dispose()
