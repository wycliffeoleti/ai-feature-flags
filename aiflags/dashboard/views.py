"""Dashboard routes, mounted onto the management API.

Read-mostly, with one write: the confirmed rollback button. It posts a form and
redirects, so a rollback is never something a browser can do by following a link
or prefetching one.
"""

from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from aiflags.dashboard.data import build_analytics, build_overview, build_overviews
from aiflags.dashboard.render import (
    render_analytics,
    render_flag_detail,
    render_overview,
)
from aiflags.store.base import FlagNotFound, FlagRepository
from aiflags.store.quality import QualityStore

DASHBOARD_ACTOR = "dashboard"
DEFAULT_ROLLBACK_REASON = "manual rollback from the dashboard"


def create_dashboard_router(
    repository: FlagRepository, quality: QualityStore
) -> APIRouter:
    router = APIRouter(prefix="/dashboard", tags=["dashboard"])

    def get_repository() -> FlagRepository:
        return repository

    def get_quality() -> QualityStore:
        return quality

    Repo = Annotated[FlagRepository, Depends(get_repository)]
    Quality = Annotated[QualityStore, Depends(get_quality)]
    FlagKey = Annotated[str, Path(min_length=1, max_length=200)]

    @router.get("", response_class=HTMLResponse)
    def overview(repo: Repo, quality_store: Quality) -> HTMLResponse:
        return HTMLResponse(render_overview(build_overviews(repo, quality_store)))

    @router.get("/analytics", response_class=HTMLResponse)
    def analytics(repo: Repo, quality_store: Quality) -> HTMLResponse:
        return HTMLResponse(render_analytics(build_analytics(repo, quality_store)))

    @router.get("/flags/{key}", response_class=HTMLResponse)
    def flag_detail(
        key: FlagKey, repo: Repo, quality_store: Quality
    ) -> HTMLResponse:
        flag = repo.get_flag(key)
        if flag is None:
            raise FlagNotFound(key)
        return HTMLResponse(
            render_flag_detail(
                build_overview(flag, quality_store),
                quality_store.decisions(key, limit=50),
                repo.audit_events(key),
            )
        )

    @router.post("/flags/{key}/rollback")
    async def rollback(key: FlagKey, request: Request, repo: Repo) -> RedirectResponse:
        # The body is parsed here rather than with FastAPI's `Form()`, which
        # pulls in python-multipart. An HTML form posts urlencoded by default,
        # and the stdlib parses that in one call — no dependency needed for the
        # single field this endpoint accepts.
        body = (await request.body()).decode("utf-8", errors="replace")
        fields = parse_qs(body)
        reason = fields.get("reason", [DEFAULT_ROLLBACK_REASON])[0].strip()
        repo.rollback(
            key, actor=DASHBOARD_ACTOR, reason=reason or DEFAULT_ROLLBACK_REASON
        )
        # 303 so the browser re-issues a GET; a refresh must not repeat the POST.
        return RedirectResponse(url="/dashboard", status_code=303)

    return router
