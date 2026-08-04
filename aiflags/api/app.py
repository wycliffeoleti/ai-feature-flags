"""Management API for the control plane.

Operators and the rollout controller both drive flags through this surface. Every
mutating endpoint requires an actor and a reason, and answers with the snapshot
version it produced so a caller can tell when its change is live.

``/snapshot`` is the data plane's read path — the SDK polls it (directly, or via
the Redis-published copy) and evaluates locally. It is deliberately the only
endpoint on the request path of a user-facing application.
"""

# No `from __future__ import annotations` here on purpose: FastAPI resolves
# dependency annotations at decoration time, and the `Repo` alias below is local
# to the factory. Deferring annotations to strings would leave it unresolvable.

from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Path, Query
from fastapi.responses import JSONResponse

from aiflags.core.models import FlagSnapshot, FlagStatus
from aiflags.store.base import (
    AuditEvent,
    FlagAlreadyExists,
    FlagNotFound,
    FlagRepository,
    flag_from_dict,
    flag_to_dict,
)
from aiflags.api.schemas import (
    Attribution,
    AuditEventResponse,
    CreateFlagRequest,
    MutationResponse,
    ReplaceFlagRequest,
    RolloutRequest,
    SnapshotResponse,
)

FlagKey = Annotated[str, Path(min_length=1, max_length=200)]


def create_app(
    repository: FlagRepository, quality_store=None
) -> FastAPI:
    """Build the API around a repository.

    Taking the stores as arguments rather than reaching for module-level
    singletons is what lets the test suite drive the whole surface against the
    in-memory implementations with no database running.

    ``quality_store`` is optional: without it the API serves flag management
    only, which is all the Phase 1 surface needs. Supplying one mounts the
    dashboard, which reads quality evidence as well as configuration.
    """
    app = FastAPI(
        title="ai-feature-flags",
        version="0.1.0",
        summary="Gradual rollout of AI features with quality-gated automatic rollback",
    )

    def get_repository() -> FlagRepository:
        return repository

    Repo = Annotated[FlagRepository, Depends(get_repository)]

    @app.exception_handler(FlagNotFound)
    async def _not_found(_request, exc: FlagNotFound) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(FlagAlreadyExists)
    async def _conflict(_request, exc: FlagAlreadyExists) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # -- flags ------------------------------------------------------------- #

    @app.post("/flags", response_model=MutationResponse, status_code=201)
    def create_flag(request: CreateFlagRequest, repo: Repo) -> MutationResponse:
        flag = _decode(request.flag.to_domain_dict())
        version = repo.create_flag(flag, actor=request.actor, reason=request.reason)
        return MutationResponse(snapshot_version=version, flag_key=flag.key)

    @app.get("/flags")
    def list_flags(repo: Repo) -> list[dict[str, Any]]:
        return [flag_to_dict(flag) for flag in repo.list_flags()]

    @app.get("/flags/{key}")
    def get_flag(key: FlagKey, repo: Repo) -> dict[str, Any]:
        flag = repo.get_flag(key)
        if flag is None:
            raise FlagNotFound(key)
        return flag_to_dict(flag)

    @app.put("/flags/{key}", response_model=MutationResponse)
    def replace_flag(
        key: FlagKey, request: ReplaceFlagRequest, repo: Repo
    ) -> MutationResponse:
        if request.flag.key != key:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"path key {key!r} does not match body key {request.flag.key!r}; "
                    "refusing to guess which one was intended"
                ),
            )
        flag = _decode(request.flag.to_domain_dict())
        version = repo.replace_flag(flag, actor=request.actor, reason=request.reason)
        return MutationResponse(snapshot_version=version, flag_key=key)

    # -- rollout control ---------------------------------------------------- #

    @app.post("/flags/{key}/rollout", response_model=MutationResponse)
    def set_rollout(
        key: FlagKey, request: RolloutRequest, repo: Repo
    ) -> MutationResponse:
        """Set the rollout percentage."""
        version = repo.set_rollout_percentage(
            key, request.percentage, actor=request.actor, reason=request.reason
        )
        return MutationResponse(snapshot_version=version, flag_key=key)

    @app.post("/flags/{key}/pause", response_model=MutationResponse)
    def pause(key: FlagKey, request: Attribution, repo: Repo) -> MutationResponse:
        """Halt the rollout at its current percentage.

        Traffic keeps being served exactly as it is. Pausing stops the ramp; it
        does not stop the quality gates, which can still roll the flag back.
        """
        version = repo.set_status(
            key, FlagStatus.PAUSED, actor=request.actor, reason=request.reason
        )
        return MutationResponse(snapshot_version=version, flag_key=key)

    @app.post("/flags/{key}/resume", response_model=MutationResponse)
    def resume(key: FlagKey, request: Attribution, repo: Repo) -> MutationResponse:
        """Return a paused or rolled-back flag to an active rollout.

        This is the only way out of ROLLED_BACK, and it is deliberately manual:
        automation that can undo its own rollback will flap against whatever
        caused it.
        """
        version = repo.set_status(
            key, FlagStatus.ROLLING_OUT, actor=request.actor, reason=request.reason
        )
        return MutationResponse(snapshot_version=version, flag_key=key)

    @app.post("/flags/{key}/rollback", response_model=MutationResponse)
    def rollback(key: FlagKey, request: Attribution, repo: Repo) -> MutationResponse:
        """Switch all traffic back to baseline immediately."""
        version = repo.rollback(key, actor=request.actor, reason=request.reason)
        return MutationResponse(snapshot_version=version, flag_key=key)

    # -- data plane and audit ------------------------------------------------ #

    @app.get("/snapshot", response_model=SnapshotResponse)
    def snapshot(repo: Repo) -> SnapshotResponse:
        return _encode_snapshot(repo.snapshot())

    @app.get("/audit", response_model=list[AuditEventResponse])
    def audit(
        repo: Repo, flag_key: Annotated[str | None, Query()] = None
    ) -> list[AuditEventResponse]:
        return [_encode_audit(event) for event in repo.audit_events(flag_key)]

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    if quality_store is not None:
        from aiflags.dashboard.views import create_dashboard_router

        app.include_router(create_dashboard_router(repository, quality_store))

    return app


def _decode(payload: dict[str, Any]):
    """Turn a validated payload into a domain flag, mapping errors to 400.

    Pydantic covers field-level validity; the domain model owns the cross-field
    rules (non-decreasing stages, matching variant kinds) that Pydantic cannot
    express as cleanly.
    """
    try:
        return flag_from_dict(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _encode_snapshot(snapshot: FlagSnapshot) -> SnapshotResponse:
    return SnapshotResponse(
        version=snapshot.version,
        published_at=snapshot.published_at.isoformat(),
        flags={key: flag_to_dict(flag) for key, flag in snapshot.flags.items()},
    )


def _encode_audit(event: AuditEvent) -> AuditEventResponse:
    return AuditEventResponse(
        flag_key=event.flag_key,
        action=event.action,
        actor=event.actor,
        reason=event.reason,
        at=event.at.isoformat(),
        snapshot_version=event.snapshot_version,
        detail=event.detail,
    )
