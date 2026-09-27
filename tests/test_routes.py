from __future__ import annotations


def test_all_registered_static_get_endpoints_return_a_response(admin_client, app):
    ignored = {"static"}
    rules = [
        rule
        for rule in app.url_map.iter_rules()
        if "GET" in rule.methods and not rule.arguments and rule.endpoint not in ignored
    ]

    assert rules
    failures = []
    for rule in rules:
        response = admin_client.get(rule.rule)
        if response.status_code >= 500 or not response.data:
            failures.append((rule.endpoint, rule.rule, response.status_code))

    assert failures == []


def test_health_endpoint_is_ready_in_local_test_mode(client):
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "disabled"},
    }
