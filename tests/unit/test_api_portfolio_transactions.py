from __future__ import annotations

from auspex.api.auth import AuthenticatedUser, get_current_user
from auspex.api.deps import get_portfolio_ledger_service
from auspex.api.routes import portfolio
from tests.unit.conftest import make_router_app


class Service:
    def __init__(self) -> None:
        self.created = []

    async def list_transactions(self, user_id):
        return []

    async def create_transaction(self, user_id, payload):
        self.created.append((user_id, payload))
        return {
            "transaction_id": "txn-1",
            "transaction_type": "DEPOSIT",
            "event_date": "2026-08-11",
            "currency": "CHF",
            "cash_amount": "1000",
            "fees": "0",
            "created_at": "2026-08-11T00:00:00Z",
        }


def client(service=None):
    service = service or Service()
    return make_router_app(
        portfolio.router,
        {
            get_current_user: lambda: AuthenticatedUser(user_id="owner-1", claims={}),
            get_portfolio_ledger_service: lambda: service,
        },
    )


def test_lists_transactions() -> None:
    response = client().get("/api/portfolio/transactions")
    assert response.status_code == 200
    assert response.json() == []


def test_creates_owner_scoped_transaction() -> None:
    service = Service()
    response = client(service).post(
        "/api/portfolio/transactions",
        json={
            "client_request_id": "request-1",
            "transaction_type": "DEPOSIT",
            "event_date": "2026-08-11",
            "currency": "CHF",
            "amount": "1000",
            "fees": "0",
        },
    )
    assert response.status_code == 201
    assert service.created[0][0] == "owner-1"
