from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# Imported here so Alembic always sees the complete metadata.
from app.db import models as models  # noqa: E402, F401
