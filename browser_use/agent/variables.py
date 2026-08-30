"""Models for variable detection in persisted agent histories."""

from pydantic import BaseModel, Field


class DetectedVariable(BaseModel):
	name: str
	original_value: str
	type: str = 'string'
	format: str | None = None


class VariableMetadata(BaseModel):
	detected_variables: dict[str, DetectedVariable] = Field(default_factory=dict)
