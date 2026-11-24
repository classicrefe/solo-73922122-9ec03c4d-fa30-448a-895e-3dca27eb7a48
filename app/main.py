"""HTTP surface.  Thin handlers over :mod:`app.store`."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import Settings
from .db import initialise
from .errors import ApiError
from .months import InvalidMonth
from .schemas import (
    BasketCreate,
    ContinuationRequest,
    PriceBatch,
    ReplacementCreate,
    WeightUpdate,
)
from . import store


def create_app(settings: Settings | None = None) -> FastAPI:
    active = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        initialise(active.db_path, active.busy_timeout_ms)
        yield

    app = FastAPI(
        title="Monthly Price Index API",
        version="1.0.0",
        description=(
            "Fixed-weight chained monthly price index with an auditable, "
            "versioned basket.  Publish validates the whole basket and is "
            "all-or-nothing; revisions recompute only from the earliest "
            "affected month."
        ),
        lifespan=lifespan,
    )

    def db() -> Any:
        return active

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.payload()})

    @app.exception_handler(InvalidMonth)
    async def _bad_month(_: Request, exc: InvalidMonth) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "invalid_month",
                    "message": str(exc),
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        issues = []
        for error in exc.errors():
            issues.append(
                {
                    "location": [str(part) for part in error.get("loc", [])],
                    "message": str(error.get("msg", "invalid value")),
                    "type": str(error.get("type", "value_error")),
                }
            )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "request_invalid",
                    "message": "request body or parameters failed validation",
                    "details": {"issues": issues},
                }
            },
        )

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": "unexpected server error",
                }
            },
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # -- baskets ---------------------------------------------------------

    @app.post("/baskets", status_code=status.HTTP_201_CREATED)
    def create_basket(payload: BasketCreate, cfg: Settings = Depends(db)) -> dict:
        return store.create_basket(
            cfg.db_path,
            payload.model_dump(),
            cfg.busy_timeout_ms,
        )

    @app.get("/baskets")
    def list_baskets(cfg: Settings = Depends(db)) -> dict:
        return {"baskets": store.list_baskets(cfg.db_path, cfg.busy_timeout_ms)}

    @app.get("/baskets/{basket_id}")
    def get_basket(basket_id: int, cfg: Settings = Depends(db)) -> dict:
        return store.get_basket(cfg.db_path, basket_id, cfg.busy_timeout_ms)

    # -- draft editing ---------------------------------------------------

    @app.post("/baskets/{basket_id}/revisions", status_code=status.HTTP_201_CREATED)
    def create_revision(basket_id: int, cfg: Settings = Depends(db)) -> dict:
        return store.create_revision(cfg.db_path, basket_id, cfg.busy_timeout_ms)

    @app.get("/baskets/{basket_id}/draft")
    def get_draft(basket_id: int, cfg: Settings = Depends(db)) -> dict:
        return store.get_draft(cfg.db_path, basket_id, cfg.busy_timeout_ms)

    @app.put("/baskets/{basket_id}/draft/slots/{slot_key}/weight")
    def set_weight(
        basket_id: int,
        slot_key: str,
        payload: WeightUpdate,
        cfg: Settings = Depends(db),
    ) -> dict:
        return store.set_weight(
            cfg.db_path, basket_id, slot_key, payload.weight_bp, cfg.busy_timeout_ms
        )

    @app.put("/baskets/{basket_id}/draft/prices")
    def upsert_prices(
        basket_id: int, payload: PriceBatch, cfg: Settings = Depends(db)
    ) -> dict:
        return store.upsert_prices(
            cfg.db_path,
            basket_id,
            [entry.model_dump() for entry in payload.entries],
            cfg.busy_timeout_ms,
        )

    @app.delete("/baskets/{basket_id}/draft/prices/{item_code}/{month}")
    def delete_price(
        basket_id: int,
        item_code: str,
        month: str,
        cfg: Settings = Depends(db),
    ) -> dict:
        return store.delete_price(
            cfg.db_path, basket_id, item_code, month, cfg.busy_timeout_ms
        )

    @app.post("/baskets/{basket_id}/draft/replacements", status_code=status.HTTP_201_CREATED)
    def replace_item(
        basket_id: int, payload: ReplacementCreate, cfg: Settings = Depends(db)
    ) -> dict:
        return store.replace_item(
            cfg.db_path,
            basket_id,
            payload.slot_key,
            payload.month,
            payload.new_code,
            payload.new_name,
            payload.old_price_cents,
            payload.new_price_cents,
            cfg.busy_timeout_ms,
        )

    # -- publishing ------------------------------------------------------

    @app.post("/baskets/{basket_id}/draft/continuation", status_code=status.HTTP_201_CREATED)
    def continue_slot(
        basket_id: int, payload: ContinuationRequest, cfg: Settings = Depends(db)
    ) -> dict:
        return store.continue_slot_quotes(
            cfg.db_path,
            basket_id,
            payload.model_dump(),
            cfg.busy_timeout_ms,
        )

    @app.post("/baskets/{basket_id}/publish", status_code=status.HTTP_201_CREATED)
    def publish(basket_id: int, cfg: Settings = Depends(db)) -> dict:
        return store.publish(cfg.db_path, basket_id, cfg.busy_timeout_ms)

    # -- queries ---------------------------------------------------------

    @app.get("/baskets/{basket_id}/snapshot")
    def current_snapshot(
        basket_id: int,
        response: Response,
        cfg: Settings = Depends(db),
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return store.get_current_snapshot(cfg.db_path, basket_id, cfg.busy_timeout_ms)

    @app.get("/baskets/{basket_id}/versions/{version_no}/snapshot")
    def version_snapshot(
        basket_id: int,
        version_no: int,
        response: Response,
        cfg: Settings = Depends(db),
    ) -> dict:
        response.headers["Cache-Control"] = "no-store"
        return store.get_snapshot(
            cfg.db_path, basket_id, version_no, cfg.busy_timeout_ms
        )

    @app.get("/baskets/{basket_id}/versions/{version_no}/index")
    def version_index(
        basket_id: int,
        version_no: int,
        cfg: Settings = Depends(db),
        from_month: Annotated[str | None, Query(alias="from")] = None,
        to_month: Annotated[str | None, Query(alias="to")] = None,
    ) -> dict:
        return store.get_index(
            cfg.db_path,
            basket_id,
            version_no,
            from_month,
            to_month,
            cfg.busy_timeout_ms,
        )

    return app


app = create_app()
