"""Factor is the only process that talks to cTrader. Stub mode keeps the order in memory."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

import httpx

from poc.config import Settings


class Broker(Protocol):
    def place(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        ...

    def manage(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        ...

    def cancel(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        ...

    def list_positions(self) -> list[dict[str, Any]]:
        ...


class StubBroker:
    def __init__(self) -> None:
        self.orders: list[dict[str, Any]] = []
        self.managed: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self.positions: list[dict[str, Any]] = []
        self._n = 0

    def place(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        self._n += 1
        position_id = None if payload.get("order_type") != "market" else f"pos-{self._n}"
        result = {
            "success": True,
            "order_id": f"ord-{self._n}",
            "position_id": position_id,
            "message": "stub",
        }
        self.orders.append({"run_id": run_id, "payload": payload, "result": result})
        if position_id:
            self.positions.append(
                {
                    "position_id": position_id,
                    "side": payload.get("side"),
                    "volume": payload.get("volume"),
                    "entry_price": payload.get("entry_price"),
                    "stop_loss": payload.get("stop_loss"),
                    "take_profit": payload.get("take_profit"),
                    "label": payload.get("label"),
                }
            )
        return result

    def manage(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        self.managed.append({"run_id": run_id, "payload": payload})
        if payload.get("action") == "close":
            self.positions = [
                row
                for row in self.positions
                if str(row.get("position_id")) != str(payload.get("position_id"))
            ]
        return {"success": True, "message": "stub", "position_id": payload.get("position_id")}

    def cancel(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        self.cancelled.append({"run_id": run_id, "payload": payload})
        return {"success": True, "message": "stub", "order_id": payload.get("order_id")}

    def list_positions(self) -> list[dict[str, Any]]:
        return list(self.positions)


class HttpBroker:
    def __init__(self, settings: Settings, http: httpx.Client | None = None) -> None:
        self.settings = settings
        self.http = http or httpx.Client(timeout=30)

    def _headers(self, run_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.factor_api_token}",
            "Idempotency-Key": run_id,
            "Content-Type": "application/json",
        }

    def _body(self, response: httpx.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except Exception:
            parsed = {}
        if isinstance(parsed, dict) and "success" in parsed:
            return parsed
        message = ""
        if isinstance(parsed, dict):
            message = str(parsed.get("detail") or parsed.get("message") or "")
        if not message:
            message = response.text[:300]
        return {
            "success": False,
            "order_id": None,
            "position_id": None,
            "message": message or f"http {response.status_code}",
        }

    def _post(self, path: str, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        if not self.settings.factor_api_token:
            return {"success": False, "message": "missing_factor_token"}
        response = self.http.post(
            f"{self.settings.factor_base_url}{path}",
            json=payload,
            headers=self._headers(run_id),
        )
        return self._body(response)

    def place(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        return self._post("/api/ai/orders", payload, run_id)

    def manage(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        return self._post("/api/ai/positions/manage", payload, run_id)

    def cancel(self, payload: dict[str, Any], run_id: str) -> dict[str, Any]:
        return self._post("/api/ai/orders/cancel", payload, run_id)

    def list_positions(self) -> list[dict[str, Any]]:
        if not self.settings.factor_api_token:
            raise RuntimeError("missing_factor_token")
        params: dict[str, str] = {"label": self.settings.label}
        account = self.settings.account_id_int()
        if account is not None:
            params["account_id"] = str(account)
        response = self.http.get(
            f"{self.settings.factor_base_url}/api/positions",
            params=params,
            headers={"Authorization": f"Bearer {self.settings.factor_api_token}"},
        )
        if response.status_code >= 400:
            raise RuntimeError(self._body(response).get("message") or f"http {response.status_code}")
        body = response.json()
        if isinstance(body, list):
            return body
        if isinstance(body, dict) and isinstance(body.get("positions"), list):
            return body["positions"]
        raise RuntimeError("unexpected positions payload")

    def quote(self) -> dict[str, float]:
        if not self.settings.factor_api_token:
            raise RuntimeError("missing_factor_token")
        params: dict[str, str] = {"symbol": self.settings.broker_symbol}
        account = self.settings.account_id_int()
        if account is not None:
            params["account_id"] = str(account)
        response = self.http.get(
            f"{self.settings.factor_base_url}/api/ai/quote",
            params=params,
            headers={"Authorization": f"Bearer {self.settings.factor_api_token}"},
            timeout=25,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._body(response).get("message") or f"http {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("unexpected quote payload")
        return {"bid": float(body["bid"]), "ask": float(body["ask"])}

    def list_trendbars(self, since: datetime, until: datetime | None = None) -> list[dict[str, Any]]:
        if not self.settings.factor_api_token:
            raise RuntimeError("missing_factor_token")
        params: dict[str, str] = {
            "symbol": self.settings.broker_symbol,
            "timeframe_min": "15",
            "since": since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if until is not None:
            params["until"] = until.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        account = self.settings.account_id_int()
        if account is not None:
            params["account_id"] = str(account)
        response = self.http.get(
            f"{self.settings.factor_base_url}/api/ai/trendbars",
            params=params,
            headers={"Authorization": f"Bearer {self.settings.factor_api_token}"},
            timeout=120,
        )
        if response.status_code >= 400:
            raise RuntimeError(self._body(response).get("message") or f"http {response.status_code}")
        body = response.json()
        if not isinstance(body, dict) or not isinstance(body.get("bars"), list):
            raise RuntimeError("unexpected trendbars payload")
        if body.get("success") is False:
            raise RuntimeError(str(body.get("message") or "trendbars failed"))
        return body["bars"]


def build_broker(settings: Settings) -> Broker:
    if settings.factor_mode == "stub":
        return StubBroker()
    return HttpBroker(settings)
