"""Circuit breaker + replica failover config (REPLICATION_FAILOVER.md)."""
from convert_search_ai.failover import CircuitBreaker, DegradedReadOnly


def test_circuit_breaker_transitions():
    t = {"v": 0.0}
    b = CircuitBreaker(cooldown_s=10, clock=lambda: t["v"])

    # healthy: try primary, not degraded
    assert b.should_try_primary() and not b.is_degraded()

    b.trip()
    assert b.is_degraded() and not b.should_try_primary()  # down for the cooldown

    t["v"] = 9.9
    assert b.is_degraded()                                 # still within cooldown
    t["v"] = 10.0
    assert b.should_try_primary() and not b.is_degraded()  # cooldown elapsed -> re-probe

    b.trip()
    b.reset()
    assert b.should_try_primary() and not b.is_degraded()  # explicit recovery


def test_degraded_read_only_is_runtime_error():
    assert issubclass(DegradedReadOnly, RuntimeError)


def test_config_replica_defaults_off_and_opt_in(monkeypatch):
    from convert_search_ai.config import Config

    for k in ("CSAI_PG_REPLICA_HOST", "CSAI_PG_REPLICA_ENABLED", "FILEENGINE_LDAP_ENDPOINT_REPLICA"):
        monkeypatch.delenv(k, raising=False)
    c = Config()
    assert c.pg_replica_enabled is False
    assert c.ldap_replica_enabled is False
    assert c.failover_cooldown_s == 30


def test_config_replica_enabled_defaults_localhost(monkeypatch):
    from convert_search_ai.config import Config

    monkeypatch.delenv("CSAI_PG_REPLICA_HOST", raising=False)
    monkeypatch.setenv("CSAI_PG_REPLICA_ENABLED", "true")
    monkeypatch.setenv("CSAI_PG_PORT", "5432")
    c = Config()
    assert c.pg_replica_enabled is True
    assert c.pg_replica_host == "localhost"
    # replica creds default to the master's
    assert f"dbname={c.pg_database}" in c.pg_replica_dsn
    assert "host=localhost" in c.pg_replica_dsn


def test_config_explicit_replica_host(monkeypatch):
    from convert_search_ai.config import Config

    monkeypatch.setenv("CSAI_PG_REPLICA_HOST", "10.0.0.9")
    monkeypatch.setenv("FILEENGINE_LDAP_ENDPOINT_REPLICA", "ldap://10.0.0.9:1389")
    c = Config()
    assert c.pg_replica_enabled and "host=10.0.0.9" in c.pg_replica_dsn
    assert c.ldap_replica_enabled and c.ldap_uri_replica == "ldap://10.0.0.9:1389"
