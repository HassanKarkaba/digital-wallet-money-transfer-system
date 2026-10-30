"""Daemon transport recovery without a database, clock sleeps, or business writes."""

from contextlib import ExitStack
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management.base import CommandError
from django.db import IntegrityError, InterfaceError, OperationalError
from django.test import SimpleTestCase

from simulator.management.commands import run_simulator
from work.management import reconnect
from work.management.commands import run_worker


class ControlledStop:
    def __init__(self, stop_on_wait=False, wait_results=None):
        self.stopped = False
        self.stop_on_wait = stop_on_wait
        self.wait_results = iter(wait_results) if wait_results is not None else None
        self.waited = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waited.append(seconds)
        if self.wait_results is not None:
            return next(self.wait_results)
        if self.stop_on_wait:
            self.stopped = True
        return self.stopped


class DaemonTransportRecoveryTests(SimpleTestCase):
    def command(self, module):
        output, errors = StringIO(), StringIO()
        return module.Command(stdout=output, stderr=errors), errors

    def worker_dependencies(self, stack):
        mocks = {}
        for name in [
            'close_old_connections',
            'heartbeat',
            'observe_clock',
            'discover_work',
            'claim_jobs',
            'close_owned_connections',
        ]:
            mocks[name] = stack.enter_context(patch.object(run_worker, name))
        mocks['claim_jobs'].return_value = []
        stack.enter_context(patch('accounts.services.process_security_mail'))
        stack.enter_context(patch.object(run_worker.threading, 'Thread'))
        return mocks

    def simulator_dependencies(self, stack):
        mocks = {}
        for name in [
            'close_old_connections',
            'heartbeat',
            'finalize_pending',
            'send_events',
            'close_owned_connections',
        ]:
            mocks[name] = stack.enter_context(patch.object(run_simulator, name))
        mocks['finalize_pending'].return_value = 3
        mocks['send_events'].return_value = 2
        return mocks

    def test_worker_recovers_from_heartbeat_timeout_before_claiming(self):
        command, errors = self.command(run_worker)
        stopping = ControlledStop()
        with ExitStack() as stack:
            deps = self.worker_dependencies(stack)
            deps['heartbeat'].side_effect = [OperationalError('private connection value'), None]
            batches = command.run_loop(
                stopping, {'limit': 20, 'once': True, 'max_batches': None}, 'same-worker'
            )
        self.assertEqual(batches, 0)
        self.assertEqual(stopping.waited, [1])
        self.assertEqual(deps['heartbeat'].call_args_list[0], deps['heartbeat'].call_args_list[1])
        deps['claim_jobs'].assert_called_once_with(20, reconciliation_first=False)
        deps['close_owned_connections'].assert_called_once_with()
        self.assertNotIn('private connection value', errors.getvalue())

    def test_simulator_primary_heartbeat_failure_does_not_block_independent_effects(self):
        command, errors = self.command(run_simulator)
        stopping = ControlledStop()
        with ExitStack() as stack:
            deps = self.simulator_dependencies(stack)
            deps['heartbeat'].side_effect = InterfaceError('private connection value')
            result = command.run_loop(stopping, {'limit': 20, 'once': True}, 'same-simulator')
        self.assertEqual(result, (3, 2))
        self.assertEqual(stopping.waited, [])
        deps['finalize_pending'].assert_called_once_with(20, stop_event=stopping)
        deps['send_events'].assert_called_once_with(20, stop_event=stopping)
        deps['close_owned_connections'].assert_called_once_with(aliases=('default',))
        self.assertNotIn('private connection value', errors.getvalue())

    def test_simulator_reconnects_provider_store_and_retries_same_bounded_pass(self):
        command, _ = self.command(run_simulator)
        stopping = ControlledStop()
        with ExitStack() as stack:
            deps = self.simulator_dependencies(stack)
            deps['finalize_pending'].side_effect = [OperationalError('timeout'), 3]
            result = command.run_loop(stopping, {'limit': 20, 'once': True}, 'same-simulator')
        self.assertEqual(result, (3, 2))
        self.assertEqual(stopping.waited, [1])
        self.assertEqual(deps['finalize_pending'].call_count, 2)
        deps['send_events'].assert_called_once()
        deps['close_owned_connections'].assert_called_once_with()

    def test_simulator_resumes_primary_heartbeat_while_provider_keeps_progressing(self):
        command, _ = self.command(run_simulator)
        stopping = ControlledStop()
        sent = 0

        def send(*args, **kwargs):
            nonlocal sent
            sent += 1
            if sent == 2:
                stopping.set()
            return 2

        with ExitStack() as stack:
            deps = self.simulator_dependencies(stack)
            stack.enter_context(patch.object(run_simulator.time, 'monotonic', side_effect=[0, 0, 1]))
            deps['heartbeat'].side_effect = [OperationalError('timeout'), None]
            deps['send_events'].side_effect = send
            result = command.run_loop(stopping, {'limit': 20, 'once': False}, 'same-simulator')
        self.assertEqual(result, (6, 4))
        self.assertEqual(deps['heartbeat'].call_count, 2)
        self.assertEqual(deps['finalize_pending'].call_count, 2)
        deps['close_owned_connections'].assert_called_once_with(aliases=('default',))

    def test_worker_transport_loss_preserves_unstarted_claim_and_stops_lease_keeper(self):
        command, _ = self.command(run_worker)
        stopping = ControlledStop(stop_on_wait=True)
        first = SimpleNamespace(pk='first', generation=7, kind='LOOKUP')
        second = SimpleNamespace(pk='unstarted', generation=3, kind='LOOKUP')
        with ExitStack() as stack:
            deps = self.worker_dependencies(stack)
            deps['claim_jobs'].return_value = [first, second]
            deps['heartbeat'].side_effect = [None, OperationalError('timeout')]
            perform = stack.enter_context(patch.object(run_worker, 'perform'))
            finish = stack.enter_context(patch.object(run_worker, 'finish_job'))
            thread = stack.enter_context(patch.object(run_worker.threading, 'Thread'))
            result = command.run_loop(
                stopping, {'limit': 20, 'once': False, 'max_batches': None}, 'same-worker'
            )
        self.assertEqual(result, 1)
        perform.assert_called_once_with(first)
        finish.assert_called_once_with('first', 7)
        self.assertTrue(thread.call_args.kwargs['args'][1].is_set())
        thread.return_value.join.assert_called_once_with(timeout=1)

    def test_single_pass_reconnect_is_bounded_and_never_claims_unobserved_work(self):
        for module in [run_worker, run_simulator]:
            with self.subTest(module=module.__name__), ExitStack() as stack:
                command, _ = self.command(module)
                stopping = ControlledStop()
                if module is run_worker:
                    deps = self.worker_dependencies(stack)
                    deps['heartbeat'].side_effect = OperationalError('timeout')
                else:
                    deps = self.simulator_dependencies(stack)
                    deps['finalize_pending'].side_effect = OperationalError('timeout')
                with self.assertRaisesMessage(CommandError, 'six bounded single-pass reconnect attempts'):
                    command.run_loop(stopping, {'limit': 20, 'once': True, 'max_batches': None}, 'stable')
                self.assertEqual(stopping.waited, [1, 2, 5, 10, 30])
                self.assertEqual(deps['close_owned_connections'].call_count, 6)
                if module is run_worker:
                    deps['claim_jobs'].assert_not_called()
                else:
                    deps['send_events'].assert_not_called()

    def test_stop_during_reconnect_prevents_another_claim_or_provider_pass(self):
        for module in [run_worker, run_simulator]:
            with self.subTest(module=module.__name__), ExitStack() as stack:
                command, _ = self.command(module)
                stopping = ControlledStop(stop_on_wait=True)
                if module is run_worker:
                    deps = self.worker_dependencies(stack)
                    deps['heartbeat'].side_effect = OperationalError('timeout')
                else:
                    deps = self.simulator_dependencies(stack)
                    deps['finalize_pending'].side_effect = OperationalError('timeout')
                command.run_loop(stopping, {'limit': 20, 'once': False, 'max_batches': None}, 'stable')
                self.assertEqual(stopping.waited, [1])
                if module is run_worker:
                    deps['claim_jobs'].assert_not_called()
                    self.assertEqual(deps['heartbeat'].call_count, 1)
                else:
                    self.assertEqual(deps['finalize_pending'].call_count, 1)
                    deps['send_events'].assert_not_called()

    def test_interrupt_and_permanent_database_errors_are_not_retried(self):
        for module in [run_worker, run_simulator]:
            for error in [KeyboardInterrupt, IntegrityError]:
                with self.subTest(module=module.__name__, error=error), ExitStack() as stack:
                    command, _ = self.command(module)
                    stopping = ControlledStop()
                    deps = (
                        self.worker_dependencies(stack)
                        if module is run_worker
                        else self.simulator_dependencies(stack)
                    )
                    deps['heartbeat'].side_effect = error
                    with self.assertRaises(error):
                        command.run_loop(
                            stopping, {'limit': 20, 'once': False, 'max_batches': None}, 'stable'
                        )
                    self.assertEqual(stopping.waited, [])
                    deps['close_owned_connections'].assert_not_called()

    def test_optional_stopped_heartbeat_failure_preserves_original_interrupt(self):
        for module in [run_worker, run_simulator]:
            with self.subTest(module=module.__name__), ExitStack() as stack:
                command, _ = self.command(module)
                stack.enter_context(patch.object(module.signal, 'signal'))
                stack.enter_context(patch.object(command, 'run_loop', side_effect=KeyboardInterrupt))
                stopped = stack.enter_context(
                    patch.object(module, 'heartbeat', side_effect=OperationalError('timeout'))
                )
                close = stack.enter_context(patch.object(module, 'close_owned_connections'))
                with self.assertRaises(KeyboardInterrupt):
                    command.handle(limit=20, once=False, max_batches=None)
                self.assertEqual(stopped.call_args.kwargs['state'], 'STOPPED')
                close.assert_called_once_with()

    def test_lease_keeper_reconnects_without_changing_claim_generation(self):
        stopping = ControlledStop(wait_results=[False, False, False, True])
        job = SimpleNamespace(pk='stable-job', generation=7)
        with (
            patch.object(run_worker, 'heartbeat', side_effect=[OperationalError('timeout'), None]),
            patch.object(run_worker, 'extend_lease') as extend,
            patch.object(run_worker, 'close_old_connections'),
            patch.object(run_worker, 'close_owned_connections') as close,
        ):
            run_worker.lease_heartbeat([job], stopping, 'stable-worker')
        self.assertEqual(stopping.waited, [10, 1, 10, 10])
        extend.assert_called_once_with('stable-job', 7)
        self.assertEqual(close.call_count, 2)

    def test_connection_cleanup_reaches_other_aliases_and_can_isolate_primary(self):
        primary, provider = Mock(alias='default'), Mock(alias='simulator')
        primary.close.side_effect = InterfaceError('already unavailable')
        with patch.object(reconnect.connections, 'all', return_value=[primary, provider]):
            reconnect.close_owned_connections()
        primary.close.assert_called_once()
        provider.close.assert_called_once()
        primary.reset_mock()
        provider.reset_mock()
        with patch.object(reconnect.connections, 'all', return_value=[primary, provider]):
            reconnect.close_owned_connections(aliases=('default',))
        primary.close.assert_called_once()
        provider.close.assert_not_called()

    def test_reconnect_delay_is_capped(self):
        self.assertEqual([reconnect.reconnect_delay(i) for i in range(1, 8)], [1, 2, 5, 10, 30, 30, 30])
