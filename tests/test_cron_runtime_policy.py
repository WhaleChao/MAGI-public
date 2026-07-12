from skills.ops.cron_runtime_policy import cron_job_timeout


def test_cron_timeout_policy_preserves_custom_values():
    assert cron_job_timeout({"id": "job_x", "timeout_sec": 123}) == 123


def test_cron_timeout_policy_uses_shared_long_job_limits():
    assert cron_job_timeout({"id": "job_nightly_regression"}) == 7200
    assert cron_job_timeout({"id": "job_nightly_autopilot"}) == 28800
    assert cron_job_timeout({"id": "job_x", "long_job": True}) == 7200


def test_cron_timeout_policy_defaults_to_ten_minutes():
    assert cron_job_timeout({"id": "job_x"}) == 600
