"""The architect's proposed task contract, before implementation or proof."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]

class AcceptanceCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Text
    given: Text
    when: Text
    then: Text
    verification: Text
    evidence_refs: list[Text] = Field(min_length=1, max_length=12)

class TaskContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    oracle: Text
    proof_type: Literal["test", "demo", "artifact", "metric", "review", "source_backed_answer", "decision"]
    acceptance_criteria: list[AcceptanceCriterion] = Field(min_length=1, max_length=12)
    allowed_files: list[Text] = Field(min_length=1, max_length=30)
    verify: list[Text] = Field(min_length=1, max_length=20)
    stop_if: list[Text] = Field(min_length=1, max_length=12)
    likely_misfire: Text

    @model_validator(mode="after")
    def demonstrated(self):
        ids = [ac.id for ac in self.acceptance_criteria]
        if len(ids) != len(set(ids)):
            raise ValueError("Acceptance criterion identities must be unique")
        if any(ac.verification not in self.verify for ac in self.acceptance_criteria):
            raise ValueError("Each acceptance criterion needs a verification target in verify")
        if set(self.verify) != {ac.verification for ac in self.acceptance_criteria}:
            raise ValueError("Each verification target must demonstrate an acceptance criterion")
        return self

class IntakeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract: TaskContract | None = None
    unresolved_questions: list[Text] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def has_resolution(self):
        if self.contract is None and not self.unresolved_questions:
            raise ValueError("Return a task contract or name the unresolved questions")
        return self
