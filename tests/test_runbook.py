from src.core import plan_recovery


def test_fresh_model_does_nothing():
    result = plan_recovery({"overall_is_fresh": True, "sources": []})
    assert result.actions == []
    assert result.escalate is False


def test_stale_source_with_failing_loader_retriggers():
    status = {
        "overall_is_fresh": False,
        "sources": [
            {
                "name": "shopify.orders",
                "is_fresh": False,
                "loader": {
                    "loader_type": "fivetran",
                    "loader_id": "shopify_connector_id",
                    "succeeded": False,
                },
            }
        ],
    }
    result = plan_recovery(status)
    assert result.escalate is False
    assert len(result.actions) == 1
    a = result.actions[0]
    assert a.kind == "retrigger_loader"
    assert (a.loader_type, a.loader_id) == ("fivetran", "shopify_connector_id")


def test_stale_source_but_loader_succeeded_escalates():
    status = {
        "overall_is_fresh": False,
        "sources": [
            {
                "name": "shopify.orders",
                "is_fresh": False,
                "loader": {
                    "loader_type": "fivetran",
                    "loader_id": "shopify_connector_id",
                    "succeeded": True,
                },
            }
        ],
    }
    result = plan_recovery(status)
    assert result.escalate is True


def test_static_source_ignored():
    status = {
        "overall_is_fresh": False,
        "sources": [
            {"name": "ref.dates", "is_fresh": True, "is_static": True, "loader": {}},
        ],
    }
    result = plan_recovery(status)
    # nothing actionable -> escalate rather than guess
    assert result.actions == []
    assert result.escalate is True
