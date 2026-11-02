from datetime import date
from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, model_validator

class OptimizerRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    budget_share_pct: float = Field(default=100, ge=0, le=100)
    mowing_deadline_date: date | None = None
    brigades_count: int = Field(default=185, ge=0, le=5000)
    target_risk_capture: float = Field(default=.99, ge=0, le=1)
    target_risk_capture_pct: float | None = Field(default=None, ge=0, le=100)
    strategy: Literal['target_risk_capture','area_or_budget_cap','brigade_capacity'] = 'target_risk_capture'
    max_area_ha: float | None = Field(default=None, ge=0)
    max_budget_rub: float | None = Field(default=None, ge=0)
    daily_norm_ha: float = Field(default=.5, gt=0, le=100)
    work_start_date: date | None = None

    @model_validator(mode='after')
    def validate_strategy(self):
        if self.strategy == 'area_or_budget_cap' and self.max_area_ha is None and self.max_budget_rub is None:
            raise ValueError('Provide max_area_ha or max_budget_rub')
        return self

class PolygonGeometry(BaseModel):
    type: Literal['Polygon']
    coordinates: list[list[list[float]]]

class Feature(BaseModel):
    type: Literal['Feature']
    geometry: PolygonGeometry
    properties: dict

class FeatureCollection(BaseModel):
    type: Literal['FeatureCollection']
    features: list[Feature]
