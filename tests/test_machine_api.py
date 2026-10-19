"""Infrastructure snapshots are opt-in, cached, primary-only and lifecycle-owned."""
import threading
import time

import pytest

from ccfleetd import api
from ccfleetd.config import Config, ConfigError
from ccfleetd.monitor import Monitor
from ccfleetd.notify import LogNotifier
from ccfleetd.store import Store
from tests.test_admin_host import PRODUCT, Browser


class NoRecurringContractsStore:
    """Metrics-only Context fixture with an explicit empty billing contract."""

    def slot_subscription_processing_required(self):
        return False


class NoopMonitor:
    """Context may attach its inactive recurring service; no worker is started."""

    def __init__(self):
        self._recurring = None

    def run_forever(self, stop):
        pass


def test_infrastructure_collection_is_an_explicit_operator_choice():
    assert Config.from_env({}).public_machine_metrics_enabled is False
    assert Config.from_env({'CCFLEET_PUBLIC_MACHINE_METRICS_ENABLED': 'true'}).public_machine_metrics_enabled
    with pytest.raises(ConfigError, match='PUBLIC_MACHINE_METRICS_ENABLED'):
        Config.from_env({'CCFLEET_PUBLIC_MACHINE_METRICS_ENABLED': 'maybe'})


@pytest.mark.parametrize('enabled,broker,expected', [(False, False, False),
                                                   (True, False, True), (True, True, False)])
def test_only_opted_in_primary_constructs_an_observer(monkeypatch, enabled, broker, expected):
    made = []
    monkeypatch.setattr(api.machine_health, 'Observer', lambda: made.append(object()) or made[-1])
    ctx = api.Context(NoRecurringContractsStore(),
                      Config(public_machine_metrics_enabled=enabled, broker_only=broker),
                      NoopMonitor())
    assert bool(made) is expected
    assert (ctx.machine_health is not None) is expected


@pytest.mark.parametrize('enabled', [False, True])
def test_status_request_reads_cache_once_and_never_collects_a_sample(enabled):
    cfg = Config(bind_port=0, public_url=f'https://{PRODUCT}',
                 public_machine_metrics_enabled=enabled)
    store = Store(':memory:')
    ctx = api.Context(store, cfg, Monitor(store, cfg, LogNotifier()))

    class Cache:
        calls = 0

        def snapshot(self, now):
            self.calls += 1
            return api.machine_health.Observer().snapshot(time.time())

        def sample_once(self):
            pytest.fail('Status must not collect kernel measurements')

    cache = Cache()
    ctx.machine_health = cache if enabled else None
    server = api.build_server(ctx, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        reply = Browser(server.server_address[1], PRODUCT).call('GET', '/status')
        assert reply.status == 200
        assert ('id="gateway-machine"' in reply.body) is enabled
        assert cache.calls == int(enabled)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        store.close()


@pytest.mark.parametrize('enabled', [False, True])
def test_serving_loop_owns_sampler_start_stop_and_join(monkeypatch, enabled):
    constructed, handlers = [], {}

    class Thread:
        def __init__(self, target, args=(), name=None, daemon=False):
            self.target, self.args, self.name = target, args, name
            self.started = False
            self.joined = None
            constructed.append(self)

        def start(self):
            self.started = True

        def join(self, timeout=None):
            self.joined = timeout

    class Server:
        closed = False

        def serve_forever(self):
            handlers[api.signal.SIGTERM](None, None)

        def shutdown(self):
            pass

        def server_close(self):
            self.closed = True

    class Monitor:
        def run_forever(self, stop):
            pass

    class Sampler:
        def run(self, stop):
            pass

    server = Server()
    ctx = api.Context(NoRecurringContractsStore(),
                      Config(public_machine_metrics_enabled=enabled), Monitor())
    ctx.machine_health = Sampler() if enabled else None
    gateway_closed = []
    close_gateway = ctx.gateway.close

    def close_output_recorder():
        gateway_closed.append(True)
        close_gateway()

    monkeypatch.setattr(ctx.gateway, 'close', close_output_recorder)
    monkeypatch.setattr(api, 'build_server', lambda context: server)
    monkeypatch.setattr(api.threading, 'Thread', Thread)
    monkeypatch.setattr(api.signal, 'signal', lambda number, handler: handlers.update({number: handler}))
    api.serve(ctx)
    metrics = [item for item in constructed if item.name == 'ccfleet-machine-metrics']
    assert len(metrics) == int(enabled)
    if enabled:
        assert metrics[0].started and metrics[0].args[0].is_set()
        assert metrics[0].joined == 2
    assert server.closed and ctx.gateway.stopping.is_set()
    assert gateway_closed == [True]
