"""/api/magic — PRISM's connection to a headless Magic Cloud backend.

Configure/clear the stored credential (fingerprint-only responses —
the secret never leaves the server), bootstrap a fresh instance, and
proxy admin operations (execute Hyperlambda, list endpoints) through
services/magic_client. Lane 1 of the LOB-factory epic.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from prism_service.services import magic_client as mc
from prism_service.services import magic_app_builder as ab
from prism_service.services import magic_ai
from prism_service.services import magic_spec_gaps

router = APIRouter()


class CheckSpecBody(BaseModel):
    spec: dict


@router.post("/check-spec")
def check_spec(body: CheckSpecBody) -> dict:
    """The gap detector: PRISM inspects a PI-produced spec for missing or
    ambiguous rules and returns follow-up questions for the SLM to voice on
    the PI screen. `complete` is False while a blocking gap remains."""
    return magic_spec_gaps.check_spec(body.spec)


class InterviewBody(BaseModel):
    spec: dict = {}          # start a new interview from this draft spec
    session: dict = {}       # OR resume a serialized interview
    answers: list = []       # [{question, answer}] to apply this turn


@router.post("/interview")
def interview(body: InterviewBody) -> dict:
    """Reverse-engineering interview, one turn. Start with `spec`, or resume
    with `session` + `answers`. Returns the state machine's state + the next
    questions + the running spec + the serialized session (persist it in
    memory / re-post it next turn). Consistency is the state machine's job."""
    from prism_service.services import magic_interview as mi
    import json as _json
    import re as _re

    def refine(spec: dict, answered: list) -> dict:
        qa = "\n".join(f"Q: {a['question']}\nA: {a['answer']}" for a in answered)
        prompt = (f"Current app spec (JSON):\n{_json.dumps(spec)}\n\n"
                  f"Clarifications from the customer:\n{qa}\n\nOutput the "
                  "UPDATED app spec incorporating the clarifications, same JSON "
                  "shape. Output ONLY JSON.")
        try:
            text, _ = magic_ai.local_complete(
                [{"role": "system", "content": "You update a JSON app spec."},
                 {"role": "user", "content": prompt}])
            m = _re.search(r"\{.*\}", _re.sub(r"<think>.*?</think>", "", text,
                                              flags=_re.DOTALL), _re.DOTALL)
            return _json.loads(m.group(0)) if m else spec
        except Exception:
            return spec  # SLM hiccup: leave spec unchanged, state machine copes

    if body.session:
        iv = mi.SpecInterview.from_dict(body.session)
        if body.answers:
            iv.answer(body.answers, refine)
    else:
        iv = mi.SpecInterview(body.spec)
    return {"state": iv.state, "domain": iv.domain(),
            "questions": iv.questions(), "spec": iv.spec,
            "round": iv.round, "session": iv.to_dict()}


class ConfigureBody(BaseModel):
    url: str
    user: str = "root"
    password: str


class SetupBody(BaseModel):
    url: str
    password: str
    name: str = ""
    email: str = ""


class ExecuteBody(BaseModel):
    hyperlambda: str


@router.get("/status")
def status() -> dict:
    return mc.status()


@router.post("/configure")
def configure(body: ConfigureBody) -> dict:
    try:
        mc.authenticate(body.url, body.user, body.password)
    except mc.MagicError as e:
        code = 400 if e.status in (400, 401, 403) else 502
        raise HTTPException(code, f"magic connection failed: {e}")
    try:
        mc.save_connection(body.url, body.user, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return mc.status()


@router.post("/clear")
def clear() -> dict:
    mc.clear_connection()
    return mc.status()


@router.post("/setup")
def setup(body: SetupBody) -> dict:
    """Bootstrap a fresh headless instance — PRISM owns the root/root
    window so nobody else can claim it after provisioning."""
    try:
        out = mc.bootstrap(body.url, body.password,
                           name=body.name, email=body.email)
    except mc.MagicError as e:
        raise HTTPException(502, f"magic bootstrap failed: {e}")
    resp = mc.status()
    resp.update(out)
    return resp


@router.post("/execute")
def execute(body: ExecuteBody) -> dict:
    try:
        return mc.execute(body.hyperlambda)
    except mc.MagicError as e:
        raise HTTPException(502, f"magic execute failed: {e}")


@router.get("/endpoints")
def endpoints():
    try:
        return mc.endpoints()
    except mc.MagicError as e:
        raise HTTPException(502, f"magic endpoints failed: {e}")


class AiBody(BaseModel):
    query: str
    project: str = ""


@router.post("/ai")
def ai(body: AiBody) -> dict:
    """OpenAI-free AI generate: PRISM's Brain is the memory substrate and a
    local model (ollama) is the generator. Zero OpenAI/Anthropic tokens."""
    def retrieve(query: str, k: int = 5) -> list[str]:
        try:
            from prism_service.api._context import get_project  # type: ignore
            ctx = get_project(body.project) if body.project else None
            hits = ctx.brain_svc.search(query, limit=k) if ctx else []
            return magic_ai.make_brain_retriever(lambda q, lim: hits)(query, k)
        except Exception:
            return []
    try:
        return magic_ai.ai_generate(body.query, retrieve)
    except magic_ai.LocalLLMError as e:
        raise HTTPException(502, f"local model failed: {e}")


class BuildAppBody(BaseModel):
    spec: dict


@router.post("/build-app")
def build_app(body: BuildAppBody) -> dict:
    """PRISM as expert user of Magic: render a structured app spec into
    whitespace-perfect Hyperlambda and deploy it to the connected tenant.
    The spec is what a small model can reliably produce; the render is
    deterministic so the built app always parses."""
    try:
        return ab.deploy_app(body.spec)
    except mc.MagicError as e:
        raise HTTPException(502, f"magic build-app failed: {e}")
    except (KeyError, TypeError) as e:
        raise HTTPException(400, f"invalid app spec: {e}")
