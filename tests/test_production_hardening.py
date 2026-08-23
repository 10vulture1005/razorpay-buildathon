"""Production-hardening suite (P0/P1): authn/authz, demo-endpoint gating,
rate limiting, HMAC webhooks, contact-hours compliance, kill switch,
audit reproducibility fields."""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.policy.policy_engine import PolicyEngine
from tests.conftest import ADMIN_HEADERS, TEST_ADMIN_KEY, TEST_READ_KEY, TEST_RUN_KEY

READ_HEADERS = {"X-API-Key": TEST_READ_KEY}
RUN_HEADERS = {"X-API-Key": TEST_RUN_KEY}
NO_AUTH = {}


@pytest.fixture(scope="module")
def client():
    from app.main import app

    return TestClient(app)


@pytest.fixture(scope="module")
def seeded_case(client):
    """Create one case through the public API for webhook/auth flows."""
    r = client.post("/events/invoice-overdue", json={
        "invoice_id": "inv_prodsec_1", "customer_id": "cust_prodsec_1", "amount": 150000.0,
    }, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    return r.json()["case_id"]


# ---- P0-2: authentication ----


class TestAuth:
    def test_unauthenticated_get_cases_is_401(self, client):
        r = client.get("/cases", headers=NO_AUTH)
        assert r.status_code == 401

    def test_invalid_key_is_401(self, client):
        r = client.get("/cases", headers={"X-API-Key": "totally-wrong-key"})
        assert r.status_code == 401

    @pytest.mark.parametrize("path", ["/cases", "/metrics/recovery", "/metrics/funnel"])
    def test_read_scope_grants_read_endpoints(self, client, path):
        assert client.get(path, headers=READ_HEADERS).status_code == 200

    def test_read_scope_cannot_run_agent(self, client, seeded_case):
        r = client.post(f"/agent/run/{seeded_case}", headers=READ_HEADERS)
        assert r.status_code == 403

    def test_read_scope_cannot_ingest_events(self, client):
        r = client.post("/events/invoice-overdue", json={
            "invoice_id": "inv_x", "customer_id": "cust_x", "amount": 1000}, headers=READ_HEADERS)
        assert r.status_code == 403

    def test_run_scope_can_run_agent(self, client, seeded_case):
        r = client.post(f"/agent/run/{seeded_case}", headers=RUN_HEADERS)
        assert r.status_code == 200

    def test_health_and_readyz_need_no_auth(self, client):
        assert client.get("/health").status_code == 200
        assert client.get("/readyz").status_code == 200


# ---- P0-3: demo endpoint double gate ----


class TestSimulatePaymentGating:
    def _sim(self, client, seeded_case, headers):
        return client.post(f"/cases/{seeded_case}/simulate-payment", headers=headers)

    def test_requires_admin_scope_even_in_dev(self, client, seeded_case):
        assert self._sim(client, seeded_case, READ_HEADERS).status_code == 403
        assert self._sim(client, seeded_case, RUN_HEADERS).status_code == 403

    def test_admin_in_dev_allowed(self, client, seeded_case):
        assert self._sim(client, seeded_case, ADMIN_HEADERS).status_code == 200

    def test_blocked_in_prod_even_with_admin(self, client, seeded_case, monkeypatch):
        import app.config as config

        monkeypatch.setattr(config, "IS_PROD", True)
        r = self._sim(client, seeded_case, ADMIN_HEADERS)
        assert r.status_code == 403
        assert "production" in r.json()["detail"].lower()


# ---- P0-4: rate limiting ----


class TestRateLimiter:
    def test_sliding_window_blocks_after_limit(self):
        from app.observability.middleware import RateLimitMiddleware

        limiter = RateLimitMiddleware(app=None, limit_per_minute=3)
        for i in range(3):
            assert limiter._allow("test-bucket"), f"request {i + 1} should pass"
        assert not limiter._allow("test-bucket")

    def test_buckets_are_independent(self):
        from app.observability.middleware import RateLimitMiddleware

        limiter = RateLimitMiddleware(app=None, limit_per_minute=1)
        assert limiter._allow("a") and limiter._allow("b")
        assert not limiter._allow("a")


# ---- P1-2/P1-7: payment webhook ----


def _sign(body: bytes) -> str:
    import os

    secret = os.environ["PAYMENT_WEBHOOK_SECRET"]
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestPaymentWebhook:
    def _post(self, client, payload: dict, signature: str | None = None, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        headers = {"X-Signature": signature if signature is not None else _sign(body),
                   "Content-Type": "application/json"}
        return client.post("/webhooks/payment", content=body, headers=headers)

    def test_valid_signature_records_payment(self, client, seeded_case):
        r = self._post(client, {"event_id": "evt_001", "invoice_id": "inv_prodsec_1",
                                "amount_paid": 150000.0})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "accepted" and body["matched"] is True

    def test_replay_is_deduplicated(self, client, seeded_case):
        payload = {"event_id": "evt_replay_1", "invoice_id": "inv_prodsec_1",
                   "amount_paid": 150000.0}
        first = self._post(client, payload)
        second = self._post(client, payload)
        assert second.json() == {"status": "duplicate"}
        assert first.json()["status"] == "accepted"

    def test_bad_signature_is_401(self, client):
        r = self._post(client, {"event_id": "evt_bad", "invoice_id": "inv_prodsec_1",
                                "amount_paid": 10}, signature="deadbeef")
        assert r.status_code == 401

    def test_missing_signature_is_401(self, client):
        r = client.post("/webhooks/payment", content=b"{}",
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 401

    def test_unknown_invoice_still_dedupes(self, client):
        payload = {"event_id": "evt_unknown_1", "invoice_id": "inv_never_seen",
                   "amount_paid": 5.0}
        first = self._post(client, payload)
        assert first.json() == {"status": "accepted", "matched": False}
        assert self._post(client, payload).json() == {"status": "duplicate"}


# ---- R0: Razorpay-format webhook dedup (no reliance on the optional
# X-Razorpay-Event-Id header) ----


def _rzp_sign(body: bytes) -> str:
    import os

    secret = os.environ["RAZORPAY_WEBHOOK_SECRET"]
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _rzp_body(payment_id: str, invoice_ref: str, amount_inr: float,
              event: str = "payment_link.paid") -> bytes:
    return json.dumps({
        "event": event,
        "payload": {"payment": {"entity": {
            "id": payment_id, "amount": int(amount_inr * 100),
            "notes": {"reference_id": invoice_ref},
        }}},
    }).encode()


class TestRazorpayWebhookDedup:
    def _post(self, client, body: bytes, event_id_header: str | None = None):
        headers = {"X-Razorpay-Signature": _rzp_sign(body),
                   "Content-Type": "application/json"}
        if event_id_header is not None:
            headers["X-Razorpay-Event-Id"] = event_id_header
        return client.post("/webhooks/payment", content=body, headers=headers)

    def test_two_distinct_headerless_webhooks_both_process(self, client):
        """THE R0 bug scenario: neither webhook carries X-Razorpay-Event-Id.
        Both are different real payments and BOTH must process — under the old
        empty-string dedup key the second was silently dropped."""
        for i, inv in enumerate(["inv_rzpdup_a", "inv_rzpdup_b"]):
            client.post("/events/invoice-overdue", json={
                "invoice_id": inv, "customer_id": f"cust_rzpdup_{i}", "amount": 1000.0,
            }, headers=ADMIN_HEADERS)
        r1 = self._post(client, _rzp_body("pay_AAA", "inv_rzpdup_a", 1000.0))
        r2 = self._post(client, _rzp_body("pay_BBB", "inv_rzpdup_b", 1000.0))
        assert r1.status_code == 200 and r1.json()["matched"] is True
        assert r2.status_code == 200 and r2.json()["matched"] is True

    def test_replayed_payload_without_header_is_duplicate(self, client):
        client.post("/events/invoice-overdue", json={
            "invoice_id": "inv_rzpreplay", "customer_id": "cust_rzpreplay", "amount": 700.0,
        }, headers=ADMIN_HEADERS)
        body = _rzp_body("pay_REPLAY1", "inv_rzpreplay", 700.0)
        assert self._post(client, body).json()["status"] == "accepted"
        # Same gateway payment replayed → same entity-derived dedup key.
        assert self._post(client, body).json() == {"status": "duplicate"}

    def test_no_entity_id_and_no_header_still_processes(self, client):
        """Degraded payload (no entity id anywhere): must PROCESS, never be
        silently dropped under a constant key; mark_recovered idempotency is
        the downstream safety net."""
        client.post("/events/invoice-overdue", json={
            "invoice_id": "inv_rzpnoid", "customer_id": "cust_rzpnoid", "amount": 300.0,
        }, headers=ADMIN_HEADERS)
        body = json.dumps({
            "event": "payment_link.paid",
            "payload": {"payment": {"entity": {
                "amount": 30000, "notes": {"reference_id": "inv_rzpnoid"},
            }}},
        }).encode()
        r = self._post(client, body)
        assert r.status_code == 200 and r.json()["matched"] is True

    def test_header_event_id_still_honored_when_entity_missing(self, client):
        body = json.dumps({
            "event": "payment_link.paid",
            "payload": {"payment": {"entity": {
                "amount": 420000, "description": "inv_rzphdr",
            }}},
        }).encode()
        h1 = self._post(client, body, event_id_header="evt_hdr_1")
        replay = self._post(client, body, event_id_header="evt_hdr_1")
        assert h1.json().get("matched") in (True, False)  # may not match a case
        assert replay.json() == {"status": "duplicate"}


# ---- P1-7: India contact-hours compliance rule ----


def _state(amount=50_000, attempts=0):
    from app.models.domain import CaseState

    return CaseState(case_id="c", invoice_id="i", customer_id="cu",
                     amount_at_risk=amount, attempt_count=attempts)


def _choice(action="send_reminder"):
    from app.models.schemas import InterventionChoice

    return InterventionChoice(action=action, expected_recovery_probability=0.5,
                              channel=None, message=None, reasoning="audit only")


class TestContactHours:
    def test_outbound_send_blocked_at_night_ist(self):
        # 23:30 UTC == 05:00 IST (before 08:00) → blocked
        engine = PolicyEngine()
        night_utc = datetime(2026, 8, 20, 23, 30, tzinfo=timezone.utc)
        d = engine.check(_state(), _choice("send_reminder"), detected_at=night_utc, now=night_utc)
        assert d.allowed is False
        assert d.reason == "outside_contact_hours"
        assert d.escalate is False

    def test_outbound_send_blocked_late_evening_ist(self):
        # 14:30 UTC == 20:00 IST (after 19:00) → blocked
        engine = PolicyEngine()
        evening = datetime(2026, 8, 20, 14, 30, tzinfo=timezone.utc)
        d = engine.check(_state(), _choice("send_payment_link"), detected_at=evening, now=evening)
        assert d.allowed is False and d.reason == "outside_contact_hours"

    def test_midday_send_allowed(self):
        engine = PolicyEngine()
        midday = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)  # 10:30 IST
        d = engine.check(_state(), _choice("send_reminder"), detected_at=midday, now=midday)
        assert d.allowed is True

    def test_internal_actions_exempt_from_window(self):
        engine = PolicyEngine()
        night = datetime(2026, 8, 20, 23, 30, tzinfo=timezone.utc)
        d = engine.check(_state(amount=200_000), _choice("escalate_human"),
                         detected_at=night, now=night)
        assert d.allowed is True

    def test_rule_disabled_via_config(self, tmp_path):
        import yaml

        cfg_path = tmp_path / "policy.yaml"
        cfg_path.write_text(yaml.safe_dump({**PolicyEngine().config,
                                            "enforce_contact_hours": False}))
        engine = PolicyEngine(config_path=str(cfg_path))
        night = datetime(2026, 8, 20, 23, 30, tzinfo=timezone.utc)
        d = engine.check(_state(), _choice("send_reminder"), detected_at=night, now=night)
        assert d.allowed is True

    def test_opted_out_still_wins_over_contact_hours(self):
        from app.models.domain import CaseState

        state = CaseState(case_id="c", invoice_id="i", customer_id="cu",
                          amount_at_risk=50_000, attempt_count=0, opted_out=True)
        engine = PolicyEngine()
        midday = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)
        d = engine.check(state, _choice("send_reminder"), detected_at=midday, now=midday)
        assert d.reason == "customer_opted_out"

    def test_config_snapshot_contains_version(self):
        engine = PolicyEngine()
        midday = datetime(2026, 8, 20, 5, 0, tzinfo=timezone.utc)
        d = engine.check(_state(), _choice("send_reminder"), detected_at=midday, now=midday)
        assert d.config_snapshot.get("version") not in (None, "unset")


# ---- B3: write-tools kill switch ----


class TestKillSwitch:
    def test_disabled_write_tools_fail_retryable(self, db, monkeypatch):
        import app.config as config
        from app.db.tables import Customer, Invoice
        from app.tools.write_tools import ToolExecutionError, send_reminder

        cust = Customer(id="cust_kill", name="K", opted_out=False)
        inv = Invoice(id="inv_kill", customer_id="cust_kill", amount=1000.0,
                      currency="INR", due_date=datetime.now(timezone.utc))
        from app.db.tables import Case, CaseStatus, CaseType

        case = Case(id="case_kill", invoice_id="inv_kill", customer_id="cust_kill",
                    status=CaseStatus.AWAITING_OUTCOME, case_type=CaseType.RECEIVABLE,
                    detected_at=datetime.now(timezone.utc), amount_at_risk=1000.0)
        db.add_all([cust, inv, case])
        db.commit()

        monkeypatch.setattr(config, "WRITE_TOOLS_ENABLED", False)
        with pytest.raises(ToolExecutionError) as exc:
            send_reminder(db, "case_kill", "email", "hi", attempt_number=1)
        assert exc.value.retryable is True


# ---- P1-4: audit reproducibility ----


class TestAuditReproducibility:
    def test_diagnosis_row_carries_prompt_version_and_llm_usage(self, db):
        from app.agent import graph as agent_graph
        from app.db.tables import AuditLog, Case, CaseStatus, CaseType, Customer, Invoice
        from app.models.domain import CaseState

        cid = "case_audit_meta"
        if not db.get(Case, cid):
            db.add_all([
                Customer(id="cust_audit_meta", name="A", opted_out=False),
                Invoice(id="inv_audit_meta", customer_id="cust_audit_meta", amount=250000.0,
                        currency="INR", due_date=datetime.now(timezone.utc)),
            ])
            db.flush()
            db.add(Case(id=cid, invoice_id="inv_audit_meta", customer_id="cust_audit_meta",
                        status=CaseStatus.NEW, case_type=CaseType.RECEIVABLE,
                        detected_at=datetime.now(timezone.utc), amount_at_risk=250000.0))
            db.commit()

        agent_graph.run_case(db, cid)
        rows = db.query(AuditLog).filter(AuditLog.case_id == cid).all()
        diag = next(r for r in rows if r.event_type == "diagnosis")
        action = next(r for r in rows if r.event_type == "action_selected")
        assert isinstance(diag.payload.get("prompt_version"), str)
        assert isinstance(diag.payload.get("llm_usage"), dict)
        assert "model" in action.payload.get("llm_usage", {})
