# app/models/prospect_model.py
from sqlalchemy import Column, String, Boolean, TIMESTAMP, ForeignKey, DateTime, func
from sqlalchemy.ext.declarative import declarative_base
import uuid

Base = declarative_base()

class Prospect(Base):
    __tablename__ = "prospects"

    prospect_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(36), ForeignKey("tenants.tenant_id"), nullable=False)
    
    research_month = Column(String(20), nullable=True)
    research_by = Column(String(100), nullable=True)
    source_sheet = Column(String(255), nullable=True)
    disposition = Column(String(100), nullable=True)
    
    first_name = Column(String(100), nullable=True)
    last_name = Column(String(100), nullable=True)
    designation = Column(String(150), nullable=True)
    company_name = Column(String(255), nullable=True)
    
    email = Column(String(255), nullable=False)
    linkedin_url = Column(String(500), nullable=True)
    
    poc_city = Column(String(100), nullable=True)
    poc_state = Column(String(100), nullable=True)
    
    emp_band = Column(String(50), nullable=True)
    industry = Column(String(100), nullable=True)
    
    consent_status = Column(String(50), nullable=True)
    is_valid_email = Column(Boolean, nullable=True)
    is_anonymized = Column(Boolean, default=False)
    
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        {'mysql_engine': 'InnoDB'},
    )
