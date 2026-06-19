from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import DateTime, Integer, String, Text, create_engine, desc
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    sessionmaker,
)

load_dotenv()

DATABASE_URL = "postgresql://postgres:agentforge123@127.0.0.1:5433/agentforge"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class AgentMemory(Base):
    __tablename__ = "agent_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent_name: Mapped[str] = mapped_column(String(100), index=True)
    summary: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow
    )


Base.metadata.create_all(engine)


class LongTermMemory:
    def save(self, agent_name: str, summary: str) -> None:
        with SessionLocal() as session:
            session.add(AgentMemory(agent_name=agent_name, summary=summary))
            session.commit()

    def get(self, agent_name: str) -> list:
        with SessionLocal() as session:
            rows = (
                session.query(AgentMemory)
                .filter(AgentMemory.agent_name == agent_name)
                .order_by(desc(AgentMemory.created_at))
                .limit(10)
                .all()
            )
            return [
                {
                    "id": row.id,
                    "agent_name": row.agent_name,
                    "summary": row.summary,
                    "created_at": row.created_at.isoformat(),
                }
                for row in rows
            ]
