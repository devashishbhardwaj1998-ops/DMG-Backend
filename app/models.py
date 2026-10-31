from datetime import datetime, date
from sqlalchemy import String, Integer, Boolean, Date, DateTime, Text, ForeignKey, Index, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

class UserRegion(Base):   # which regions an employee belongs to (one or many)
    __tablename__ = "user_regions"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    region: Mapped[str] = mapped_column(String(40), primary_key=True)

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(255), unique=True)
    title: Mapped[str] = mapped_column(String(30), default="Analyst")  # Junior Analyst | Analyst | Senior Analyst | Associate | Senior Associate
    is_reviewer: Mapped[bool] = mapped_column(Boolean, default=False)  # the "reviewer tag": can see and pick the review pipeline
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)     # global access, manages team and settings
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_pipeline: Mapped[int | None] = mapped_column(Integer, nullable=True)
    profile_types: Mapped[str] = mapped_column(String(200), default="")  # eligibility by profile type (blank = all)
    availability: Mapped[str] = mapped_column(String(20), default="Available")   # Available | Not available | On leave
    availability_note: Mapped[str] = mapped_column(String(200), default="")
    availability_by: Mapped[str] = mapped_column(String(120), default="")
    availability_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    region_links: Mapped[list[UserRegion]] = relationship(cascade="all, delete-orphan", lazy="selectin")

    @property
    def role(self) -> str:
        return "Admin" if self.is_admin else "Reviewer" if self.is_reviewer else "Analyst"

    @property
    def regions(self) -> list[str]:
        return [l.region for l in self.region_links]

    def set_regions(self, regions: list[str]) -> None:
        self.region_links = [UserRegion(region=r) for r in regions]

class Asset(Base):
    __tablename__ = "assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    asset_id: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    region: Mapped[str] = mapped_column(String(20), default="")
    profile_type: Mapped[str] = mapped_column(String(30), default="")
    update_reason: Mapped[str] = mapped_column(String(80), default="")
    request_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    ownership: Mapped[str] = mapped_column(String(60), default="Others")
    source: Mapped[str] = mapped_column(String(30), default="Morning CSV")
    status: Mapped[str] = mapped_column(String(20), default="Available")  # Available|Assigned|In Progress|Pending Review|Reviewed
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    reviewer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    review_status: Mapped[str] = mapped_column(String(20), default="")
    review_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    own_diff: Mapped[int | None] = mapped_column(Integer, nullable=True)
    type_diff: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tx_diff: Mapped[int | None] = mapped_column(Integer, nullable=True)
    difficulty_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    accuracy: Mapped[int | None] = mapped_column(Integer, nullable=True)
    coverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consistency: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qa_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cms_notes: Mapped[str] = mapped_column(Text, default="")
    comments: Mapped[str] = mapped_column(Text, default="")
    cms_url: Mapped[str] = mapped_column(String(500), default="")
    upd_own_diff: Mapped[int | None] = mapped_column(Integer, nullable=True)    # the UPDATER's own difficulty reading (ownership / type / deals)
    upd_type_diff: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upd_tx_diff: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upd_difficulty_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)   # optional per-asset CMS link (overrides the Settings template)
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewer_id])
    __table_args__ = (Index("ix_queue", "status", "ownership", "request_date"), Index("ix_assignee", "assignee_id", "status"), Index("ix_region", "region", "status"))

class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    user_name: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(60))
    asset_ref: Mapped[str] = mapped_column(String(50), default="")
    detail: Mapped[str] = mapped_column(Text, default="")

class Config(Base):
    __tablename__ = "config"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)

class Project(Base):   # separate project workflow (does not count towards the pipeline limit)
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(255), default="")
    project: Mapped[str] = mapped_column(String(120), default="General")
    priority: Mapped[str] = mapped_column(String(20), default="Normal")
    request_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Available")   # Available|Assigned|In Progress|Completed
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])
    __table_args__ = (UniqueConstraint("project", "ref"),)

class Leave(Base):
    __tablename__ = "leaves"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    day: Mapped[date] = mapped_column(Date)
    type: Mapped[str] = mapped_column(String(30), default="Leave")
    note: Mapped[str] = mapped_column(String(200), default="")
    __table_args__ = (UniqueConstraint("user_id", "day"),)

class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    kind: Mapped[str] = mapped_column(String(30))
    message: Mapped[str] = mapped_column(Text)
    ref: Mapped[str] = mapped_column(String(100), default="")
    read: Mapped[bool] = mapped_column(Boolean, default=False)

class ExportProfile(Base):   # saved export recipe, shared by all admins
    __tablename__ = "export_profiles"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    filters: Mapped[str] = mapped_column(Text, default="{}")
    columns: Mapped[str] = mapped_column(Text, default="[]")
    fmt: Mapped[str] = mapped_column(String(10), default="csv")
    tab_per_region: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)

class ExportLog(Base):
    __tablename__ = "export_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    user_name: Mapped[str] = mapped_column(String(120))
    profile_name: Mapped[str] = mapped_column(String(120), default="")
    filters: Mapped[str] = mapped_column(Text, default="{}")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    fmt: Mapped[str] = mapped_column(String(10), default="csv")
