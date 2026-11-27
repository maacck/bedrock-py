"""End-to-end integration tests for JSON Schema queries, FastAPI OpenAPI, and SEC-01."""

from __future__ import annotations

import json
from typing import Annotated, Any

import pytest
from bedrock.database import (
    BedrockModel,
    FieldPolicy,
    FilterNode,
    PageResult,
    QueryPolicy,
    QuerySchema,
    QuerySpec,
    compile_query_schema,
    paginate_fields,
    paginate_models,
    paginate_select,
    search_models,
)
from bedrock.exc import BadFilterFormatError, BedrockExc
from sqlalchemy import Column, ForeignKey, Integer, String, create_engine, func, select
from sqlalchemy.orm import relationship, selectinload, sessionmaker


class IntCustomer(BedrockModel):
    __tablename__ = "int_customers"
    __filterable_fields__ = ["name", "tier"]

    id = Column(Integer, primary_key=True)
    name = Column(String(80), nullable=False)
    tier = Column(String(20), nullable=False)
    ssn = Column(String(40), nullable=False)


class IntInvoice(BedrockModel):
    __tablename__ = "int_invoices"
    __searchable_columns__ = ["number"]
    __search_op__ = "like"
    __filterable_fields__ = ["number", "status", "amount", "customer"]

    id = Column(Integer, primary_key=True)
    number = Column(String(40), nullable=False)
    status = Column(String(20), nullable=False)
    amount = Column(Integer, nullable=False)
    secret_memo = Column(String(100), nullable=True)
    customer_id = Column(Integer, ForeignKey("int_customers.id"), nullable=False)
    customer = relationship(IntCustomer)


INVOICE_QUERY_SCHEMA = compile_query_schema(IntInvoice)


@pytest.fixture
def int_session():
    engine = create_engine("sqlite:///:memory:")
    BedrockModel.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    c1 = IntCustomer(name="Acme", tier="enterprise", ssn="000-00-0001")
    c2 = IntCustomer(name="Beta", tier="starter", ssn="000-00-0002")
    session.add_all(
        [
            c1,
            c2,
            IntInvoice(number="INV-100%", status="paid", amount=500, secret_memo="vip", customer=c1),
            IntInvoice(number="INV-200", status="paid", amount=200, secret_memo="std", customer=c1),
            IntInvoice(number="INV-300", status="open", amount=100, secret_memo="std", customer=c2),
        ]
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        BedrockModel.metadata.drop_all(engine)
        engine.dispose()


class TestEndToEndQueryWorkflow:
    """Verify full JSON Schema -> validate -> paginate/export -> OpenAPI -> SEC-01 pipeline."""

    def test_same_client_json_reused_for_paginated_list_and_unpaginated_export(self, int_session):
        invoice_schema: QuerySchema[IntInvoice] = compile_query_schema(IntInvoice)
        exported_schema = invoice_schema.json_schema()
        serialized = json.dumps(exported_schema)
        assert "secret_memo" not in serialized
        assert "ssn" not in serialized
        assert '"customer.name"' in serialized

        client_json = json.dumps(
            {
                "filter": {
                    "logic": "and",
                    "conditions": [
                        {"field": "customer.tier", "op": "eq", "value": "enterprise"},
                        {"field": "status", "op": "eq", "value": "paid"},
                    ],
                },
                "sort": [{"field": "amount", "direction": "desc"}],
                "pagination": {"page": 1, "limit": 1},
            }
        )

        # 1. Paginated listing respects pagination
        page: PageResult[IntInvoice] = paginate_models(
            int_session,
            IntInvoice,
            query=client_json,
            options=[selectinload(IntInvoice.customer)],
        )
        assert page.total == 2
        assert [inv.number for inv in page.items] == ["INV-100%"]
        assert page.page_info.has_more is True

        # 2. Unpaginated export reuses the exact same JSON and ignores pagination
        exported = search_models(
            int_session,
            IntInvoice,
            query=client_json,
            options=[selectinload(IntInvoice.customer)],
        )
        assert [inv.number for inv in exported] == ["INV-100%", "INV-200"]

    def test_fastapi_resource_router_annotated_openapi_and_exception_handler(self, int_session):
        fastapi = pytest.importorskip("fastapi")
        from bedrock_fastapi.exceptions import bedrock_exception_handler
        from starlette.requests import Request

        invoice_schema = INVOICE_QUERY_SCHEMA
        router = fastapi.APIRouter(prefix="/invoices")

        @router.post("/search")
        def search_invoices_route(
            query: Annotated[QuerySpec, INVOICE_QUERY_SCHEMA],
            extra_filter: Annotated[FilterNode, INVOICE_QUERY_SCHEMA] | None = None,
        ) -> dict[str, Any]:
            return {"count": len(invoice_schema.search(int_session, query=query))}

        app = fastapi.FastAPI()
        app.include_router(router)
        openapi = app.openapi()
        openapi_text = json.dumps(openapi)

        assert "/invoices/search" in openapi["paths"]
        assert "secret_memo" not in openapi_text
        assert "ssn" not in openapi_text
        assert "customer.name" in openapi_text

        # Verify SEC-01 error redaction through bedrock_exception_handler
        handler = bedrock_exception_handler({BadFilterFormatError: 400})
        scope = {"type": "http", "method": "POST", "path": "/invoices/search", "headers": []}
        req = Request(scope)

        with pytest.raises(BadFilterFormatError) as hidden_err:
            invoice_schema.validate_python({"filter": {"field": "secret_memo", "op": "eq", "value": "vip"}})
        with pytest.raises(BadFilterFormatError) as missing_err:
            invoice_schema.validate_python({"filter": {"field": "nonexistent_col", "op": "eq", "value": "vip"}})

        import asyncio

        resp_hidden = asyncio.run(handler(req, hidden_err.value))
        resp_missing = asyncio.run(handler(req, missing_err.value))
        assert resp_hidden.status_code == 400
        assert resp_missing.status_code == 400
        body_hidden = json.loads(resp_hidden.body)
        body_missing = json.loads(resp_missing.body)
        assert body_hidden["detail"] == "Field `secret_memo` is not allowed on IntInvoice."
        assert body_missing["detail"] == "Field `nonexistent_col` is not allowed on IntInvoice."
        assert isinstance(hidden_err.value, BedrockExc)

    def test_cross_table_report_with_raw_like_and_multi_group_by(self, int_session):
        report_policy = QueryPolicy(
            fields={"number": FieldPolicy(allow_raw_like=True)},
            extra_fields={"customer_tier": FieldPolicy(column=IntCustomer.tier, sortable=True)},
            sort_expressions={"total_amount": func.sum(IntInvoice.amount)},
        )
        stmt = (
            select(
                IntCustomer.tier.label("customer_tier"),
                IntInvoice.status,
                func.sum(IntInvoice.amount).label("total_amount"),
            )
            .select_from(IntInvoice)
            .join(IntCustomer, IntInvoice.customer_id == IntCustomer.id)
            .group_by(IntCustomer.tier, IntInvoice.status)
        )
        page = paginate_select(
            int_session,
            IntInvoice,
            stmt,
            query={
                "filter": {"field": "number", "op": "like", "value": "INV-%"},
                "sort": [{"field": "total_amount", "direction": "desc"}],
                "pagination": {"page": 1, "limit": 10},
            },
            policy=report_policy,
        )
        assert page.total == 2
        assert page.items == [
            {"customer_tier": "enterprise", "status": "paid", "total_amount": 700},
            {"customer_tier": "starter", "status": "open", "total_amount": 100},
        ]

        # Single-table paginate_fields with top-level search escaping "%"
        pct_page = paginate_fields(
            int_session,
            IntInvoice,
            fields=["number", "amount"],
            query={"search": "100%", "pagination": {"page": 1, "limit": 10}},
        )
        assert pct_page.total == 1
        assert pct_page.items == [{"number": "INV-100%", "amount": 500}]
