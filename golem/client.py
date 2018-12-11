# pylint: disable=too-many-lines

import collections
import datetime
import enum
import logging
import sys
import time
import uuid
from copy import copy, deepcopy
from typing import (
    Any,
    Dict,
    Hashable,
    Iterable,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    TYPE_CHECKING,
)

from ethereum.utils import denoms
from golem_messages import datastructures as msg_datastructures
from golem_task_api.envs import DOCKER_GPU_ENV_ID
from pydispatch import dispatcher
from twisted.internet.defer import (
    inlineCallbacks,
    gatherResults,
    Deferred)
from twisted.internet.threads import deferToThread

from apps.appsmanager import AppsManager
import golem
from golem import model
from golem.appconfig import TASKARCHIVE_MAINTENANCE_INTERVAL, AppConfig
from golem.apps.default import APPS
from golem.clientconfigdescriptor import ConfigApprover, ClientConfigDescriptor
from golem.core import variables
from golem.core.common import (
    get_timestamp_utc,
    node_info_str,
    string_to_timeout,
    to_unicode,
)
from golem.core.deferred import deferred_from_future
from golem.core.fileshelper import du
from golem.core.keysauth import KeysAuth
from golem.core.service import LoopingCallService
from golem.core.simpleserializer import DictSerializer
from golem.database import Database
from golem.diag.service import DiagnosticsService, DiagnosticsOutputFormat
from golem.diag.vm import VMDiagnosticsProvider
from golem.environments.environmentsmanager import EnvironmentsManager
from golem.ethereum import exceptions as eth_exceptions
from golem.ethereum.fundslocker import FundsLocker
from golem.ethereum.transactionsystem import TransactionSystem
from golem.hardware.presets import HardwarePresets
from golem.manager.nodestatesnapshot import ComputingSubtaskStateSnapshot
from golem.monitor.model.nodemetadatamodel import NodeMetadataModel
from golem.monitor.monitor import SystemMonitor
from golem.monitorconfig import MONITOR_CONFIG
from golem.network import nodeskeeper
from golem.network.concent.client import ConcentClientService
from golem.network.concent.filetransfers import ConcentFiletransferService
from golem.network.history import MessageHistoryService
from golem.network.hyperdrive.daemon_manager import HyperdriveDaemonManager
from golem.network.p2p.local_node import LocalNode
from golem.network.p2p.p2pservice import P2PService
from golem.network.p2p.peersession import PeerSessionInfo
from golem.network.transport import msg_queue
from golem.network.transport.tcpnetwork import SocketAddress
from golem.network.upnp.mapper import PortMapperManager
from golem.ranking.ranking import Ranking
from golem.report import Component, Stage, StatusPublisher, report_calls
from golem.resource.base.resourceserver import BaseResourceServer
from golem.resource.dirmanager import DirManager, DirectoryType
from golem.resource.hyperdrive.resourcesmanager import HyperdriveResourceManager
from golem.rpc import utils as rpc_utils
from golem.rpc.mapping.rpceventnames import Task, Network, Environment, UI
from golem.task import taskpreset, taskstate
from golem.task.helpers import calculate_subtask_payment
from golem.task.taskarchiver import TaskArchiver
from golem.task.taskserver import TaskServer
from golem.task.tasktester import TaskTester
from golem.tools.os_info import OSInfo
from golem.tools.talkback import enable_sentry_logger

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    # pylint: disable=unused-import
    from golem.core.service import IService
    from golem.task.requestedtaskmanager import RequestedTaskManager
    from golem.task.taskmanager import TaskManager


class ClientTaskComputerEventListener(object):

    def __init__(self, client):
        self.client = client

    def lock_config(self, on=True):
        self.client.lock_config(on)

    def config_changed(self):
        self.client.config_changed()


class Client:  # noqa pylint: disable=too-many-instance-attributes,too-many-public-methods
    _services = []  # type: List[IService]

    def __init__(  # noqa pylint: disable=too-many-arguments,too-many-locals
            self,
            datadir: str,
            app_config: AppConfig,
            config_desc: ClientConfigDescriptor,
            keys_auth: KeysAuth,
            database: Database,
            transaction_system: TransactionSystem,
            # SEE: golem.core.variables.CONCENT_CHOICES
            concent_variant: dict,
            connect_to_known_hosts: bool = True,
            use_docker_manager: bool = True,
            use_monitor: bool = True,
            apps_manager: AppsManager = AppsManager(),
            task_finished_cb=None,
            update_hw_preset=None) -> None:

        self.apps_manager = apps_manager
        self.datadir = datadir
        self.task_tester: Optional[TaskTester] = None

        self.task_archiver = TaskArchiver(datadir)

        # Read and validate configuration
        self.app_config = app_config
        self.config_desc = config_desc
        self.config_approver = ConfigApprover(self.config_desc)

        logger.info(
            'Client %s, datadir: %s',
            node_info_str(self.config_desc.node_name,
                          keys_auth.key_id),
            datadir
        )

        self.db = database
        self.keys_auth = keys_auth

        # NETWORK
        self.node = LocalNode(
            node_name=self.config_desc.node_name,
            prv_addr=self.config_desc.node_address or None,
            key=self.keys_auth.key_id,
        )

        pub_key = self.keys_auth.key_id
        pub_key_short = pub_key[:16] + '...' + pub_key[-16:]

        golem.tools.talkback.update_sentry_user(
            node_id=pub_key_short,
            node_name=self.config_desc.node_name)

        self.p2pservice = None
        self.diag_service = None

        if not transaction_system.deposit_contract_available:
            logger.warning(
                'Disabling concent because deposit contract is unavailable',
            )
            concent_variant = variables.CONCENT_CHOICES['disabled']
        self.concent_service = ConcentClientService(
            variant=concent_variant,
            keys_auth=self.keys_auth,
        )
        self.concent_filetransfers = ConcentFiletransferService(
            keys_auth=self.keys_auth,
            variant=concent_variant,
        )

        self.task_server: Optional[TaskServer] = None
        self.port_mapper = None

        self.nodes_manager_client = None

        self._services = [
            NetworkConnectionPublisherService(
                self,
                int(self.config_desc.network_check_interval)),
            TaskArchiverService(self.task_archiver),
            MessageHistoryService(),
            DoWorkService(self),
            DailyJobsService(),
        ]

        clean_resources_older_than = \
            self.config_desc.clean_resources_older_than_seconds
        cleaning_enabled = self.config_desc.cleaning_enabled
        if cleaning_enabled and clean_resources_older_than > 0:
            logger.debug('Starting resource cleaner service ...')
            self._services.append(
                ResourceCleanerService(
                    self,
                    interval_seconds=max(
                        1, int(clean_resources_older_than / 10)),
                    older_than_seconds=clean_resources_older_than))

        self.ranking = Ranking(self)

        self.transaction_system = transaction_system
        self.transaction_system.start()

        self.funds_locker = FundsLocker(self.transaction_system)

        self.use_docker_manager = use_docker_manager
        self.connect_to_known_hosts = connect_to_known_hosts
        self.environments_manager = EnvironmentsManager()
        self.daemon_manager = None

        self.rpc_publisher = None
        self.task_test_result: Optional[Dict[str, Any]] = None

        self.resource_server = None
        self.resource_port = 0
        self.use_monitor = use_monitor
        self.monitor = None
        self.session_id = str(uuid.uuid4())

        # TODO: Move to message queue #3160
        self._task_finished_cb = task_finished_cb
        self._update_hw_preset = update_hw_preset

        if self.config_desc.in_shutdown:
            self.update_setting('in_shutdown', 0)

        dispatcher.connect(
            self.p2p_listener,
            signal='golem.p2p'
        )
        dispatcher.connect(
            self.taskmanager_listener,
            signal='golem.taskmanager'
        )
        dispatcher.connect(
            self.taskserver_listener,
            signal='golem.taskserver'
        )

        logger.debug('Client init completed')

    @property
    def task_manager(self):
        return self.task_server.task_manager

    def set_rpc_publisher(self, rpc_publisher):
        self.rpc_publisher = rpc_publisher

    def get_wamp_rpc_mapping(self):
        from apps.rendering.task import framerenderingtask
        from golem.environments.minperformancemultiplier import \
            MinPerformanceMultiplier
        from golem.network.concent import soft_switch as concent_soft_switch
        from golem.rpc.api import ethereum_ as api_ethereum
        from golem.task import rpc as task_rpc
        task_rpc_provider = task_rpc.ClientProvider(self)
        providers = (
            self,
            concent_soft_switch,
            framerenderingtask,
            MinPerformanceMultiplier,
            self.task_server,
            self.task_manager,
            self.environments_manager,
            self.transaction_system,
            task_rpc_provider,
            api_ethereum.ETSProvider(self.transaction_system),
        )
        mapping = {}
        for rpc_provider in providers:
            mapping.update(rpc_utils.object_method_map(rpc_provider))
        return mapping

    def p2p_listener(self, event='default', **kwargs):
        if event == 'unreachable':
            self.on_unreachable(**kwargs)
        elif event == 'open':
            self.on_open(**kwargs)
        elif event == 'unsynchronized':
            self.on_unsynchronized(**kwargs)
        elif event == 'new_version':
            self.on_new_version(**kwargs)

    def on_open(self, port, description, **_):
        self.node.port_statuses[port] = description

    def on_unreachable(self, port, description, **_):
        logger.warning('Port %d unreachable: %s', port, description)
        self.node.port_statuses[port] = description

    @staticmethod
    def on_unsynchronized(time_diff, **_):
        logger.warning(
            'Node time unsynchronized with monitor. Time diff: %f (s)',
            time_diff)

    def on_new_version(self, version, **_):
        logger.warning('New version of golem available: %s', version)
        self._publish(Network.new_version, str(version))

    def taskmanager_listener(self, sender, signal, event='default', **kwargs):
        if event != 'task_status_updated':
            return
        logger.debug(
            'taskmanager_listen (sender: %r, signal: %r, event: %r, args: %r)',
            sender, signal, event, kwargs
        )

        op = kwargs['op'] if 'op' in kwargs else None
        if op is taskstate.TaskOp.WORK_OFFER_RECEIVED:
            # Drop this event to avoid publisher flooding
            # main twisteds reactor thread
            return

        if op is not None and op.subtask_related():
            self._publish(Task.evt_subtask_status, kwargs['task_id'],
                          kwargs['subtask_id'], op.value)
        else:
            op_class_name: str = op.__class__.__name__ \
                if op is not None else None
            op_value: int = op.value if op is not None else None
            self._publish(Task.evt_task_status, kwargs['task_id'],
                          op_class_name, op_value)

    def taskserver_listener(
            self,
            event,
            **kwargs,
    ):
        if event == 'provider_rejected':
            self._publish(
                Task.evt_provider_rejected,
                node_id=kwargs['node_id'],
                task_id=kwargs['task_id'],
                reason=kwargs['reason'],
                details=kwargs['details'],
            )

    @report_calls(Component.client, 'sync')
    def sync(self):
        pass

    @report_calls(Component.client, 'start', stage=Stage.pre)
    def start(self):

        logger.debug('Starting client services ...')
        self.environments_manager.load_config(self.datadir)
        self.concent_service.start()
        self.concent_filetransfers.start()

        if self.use_monitor and not self.monitor:
            self.init_monitor()
        try:
            self.start_network()
        except Exception:
            logger.critical('Can\'t start network. Giving up.', exc_info=True)
            sys.exit(1)

        for service in self._services:
            if not service.running:
                service.start()
        logger.debug('Started client services')

    @inlineCallbacks
    @report_calls(Component.client, 'stop', stage=Stage.post)
    def stop(self):
        logger.debug('Stopping client services ...')
        self.stop_network()

        for service in self._services:
            if service.running:
                service.stop()
        self.concent_service.stop()
        if self.concent_filetransfers.running:
            self.concent_filetransfers.stop()
        if self.task_server:
            yield self.task_server.quit()
        if self.use_monitor and self.monitor:
            self.diag_service.stop()
            # This effectively removes monitor dispatcher connections (weakrefs)
            self.monitor = None
        logger.debug('Stopped client services')

    def start_network(self):
        logger.info("Starting network ...")
        self.node.collect_network_info(self.config_desc.seed_host,
                                       use_ipv6=self.config_desc.use_ipv6)

        logger.debug("Is super node? %s", self.node.is_super_node())

        self.p2pservice = P2PService(
            self.node,
            self.config_desc,
            self.keys_auth,
            connect_to_known_hosts=self.connect_to_known_hosts
        )

        self.task_server = TaskServer(
            self.node,
            self.config_desc,
            self,
            use_ipv6=self.config_desc.use_ipv6,
            use_docker_manager=self.use_docker_manager,
            task_archiver=self.task_archiver,
            apps_manager=self.apps_manager,
            task_finished_cb=self._task_finished_cb,
        )

        def get_performance_values():
            values = self.environments_manager.get_performance_values()
            new_env_manager = self.task_server.task_keeper.new_env_manager
            for env_id in new_env_manager.environments():
                benchmark_result = new_env_manager \
                    .get_cached_benchmark_result(env_id)
                if benchmark_result is not None:
                    values[env_id] = benchmark_result.performance
            return values
        self.p2pservice.add_metadata_provider(
            'performance', get_performance_values)

        self._restore_locks()

        monitoring_publisher_service = MonitoringPublisherService(
            self.task_server,
            interval_seconds=max(
                int(self.config_desc.node_snapshot_interval),
                60))
        monitoring_publisher_service.start()
        self._services.append(monitoring_publisher_service)

        if self.config_desc.net_masking_enabled:
            mask_udpate_service = MaskUpdateService(
                requested_task_manager=self.task_server.requested_task_manager,
                old_task_manager=self.task_server.task_manager,
                interval_seconds=self.config_desc.mask_update_interval,
                update_num_bits=self.config_desc.mask_update_num_bits
            )
            mask_udpate_service.start()
            self._services.append(mask_udpate_service)

        dir_manager = self.task_server.task_computer.dir_manager

        logger.info("Starting resource server ...")

        self.daemon_manager = HyperdriveDaemonManager(
            self.datadir,
            daemon_config={
                k: v for k, v in {
                    'host': self.config_desc.hyperdrive_address,
                    'port': self.config_desc.hyperdrive_port,
                    'rpc_host': self.config_desc.hyperdrive_rpc_address,
                    'rpc_port': self.config_desc.hyperdrive_rpc_port,
                }.items()
                if v is not None
            },
            client_config={
                'port': self.config_desc.hyperdrive_rpc_port,
                'host': self.config_desc.hyperdrive_rpc_address,
            }
        )
        self.daemon_manager.start()

        hyperdrive_addrs = self.daemon_manager.public_addresses(
            self.node.pub_addr)
        hyperdrive_ports = self.daemon_manager.ports()

        self.node.hyperdrive_prv_port = next(iter(hyperdrive_ports))

        clean_tasks_older_than = \
            self.config_desc.clean_tasks_older_than_seconds
        cleaning_enabled = self.config_desc.cleaning_enabled
        if cleaning_enabled and clean_tasks_older_than > 0:
            self.clean_old_tasks()

        resource_manager = HyperdriveResourceManager(
            dir_manager=dir_manager,
            daemon_address=hyperdrive_addrs,
            client_kwargs={
                'host': self.config_desc.hyperdrive_rpc_address,
                'port': self.config_desc.hyperdrive_rpc_port,
            },
        )
        self.resource_server = BaseResourceServer(
            resource_manager=resource_manager,
            client=self
        )

        logger.info("Restoring resources ...")
        self.task_server.restore_resources()

        # Start service after restore_resources() to avoid race conditions
        if cleaning_enabled and clean_tasks_older_than > 0:
            logger.debug('Starting task cleaner service ...')
            task_cleaner_service = TaskCleanerService(
                client=self,
                interval_seconds=max(1, clean_tasks_older_than // 10)
            )
            task_cleaner_service.start()
            self._services.append(task_cleaner_service)

        @inlineCallbacks
        def connect(ports):
            logger.info(
                'Golem is listening on addr: %s'
                ', ports: P2P=%s, Task=%s, Hyperdrive=%r',
                self.node.prv_addr,
                self.node.p2p_prv_port,
                self.node.prv_port,
                self.node.hyperdrive_prv_port
            )

            ports = ports + list(hyperdrive_ports)
            if self.config_desc.use_upnp:
                yield deferToThread(self.start_upnp, ports)

            self.node.update_public_info()
            public_ports = [
                self.node.p2p_pub_port,
                self.node.pub_port,
                self.node.hyperdrive_pub_port
            ]

            dispatcher.send(
                signal='golem.p2p',
                event='listening',
                ports=public_ports)

            listener = ClientTaskComputerEventListener(self)
            self.task_server.task_computer.register_listener(listener)

            self.task_server.requested_task_manager.restore_tasks()

            if self.monitor:
                self.diag_service.register(
                    self.p2pservice,
                    lambda data: dispatcher.send(
                        signal="golem.monitor",
                        event="peer_snapshot",
                        p2p_data=data,
                    ),
                )
                dispatcher.send(
                    signal="golem.monitor",
                    event="login",
                )

            StatusPublisher.publish(Component.client, 'start',
                                    stage=Stage.post)

        def terminate(*exceptions):
            logger.error("Golem cannot listen on ports: %s", exceptions)
            StatusPublisher.publish(Component.client, 'start',
                                    stage=Stage.exception,
                                    data=[to_unicode(e) for e in exceptions])
            sys.exit(1)

        task = Deferred()
        p2p = Deferred()

        gatherResults([p2p, task], consumeErrors=True).addCallbacks(connect,
                                                                    terminate)

        logger.info("Starting p2p server ...")
        self.p2pservice.task_server = self.task_server
        self.p2pservice.start_accepting(listening_established=p2p.callback,
                                        listening_failure=p2p.errback)

        logger.info("Starting task server ...")
        self.task_server.start_accepting(listening_established=task.callback,
                                         listening_failure=task.errback)

        self.resume()

    def _restore_locks(self) -> None:
        assert self.task_server is not None
        tm = self.task_server.task_manager
        for task_id, task_state in tm.tasks_states.items():
            if not task_state.status.is_completed():
                task = tm.tasks[task_id]
                unfinished_subtasks = task.get_total_tasks()
                for subtask_state in task_state.subtask_states.values():
                    if subtask_state.status is not None and\
                            subtask_state.status.is_finished():
                        unfinished_subtasks -= 1
                try:
                    self.funds_locker.lock_funds(
                        task_id,
                        task.subtask_price,
                        unfinished_subtasks,
                    )
                except eth_exceptions.NotEnoughFunds as e:
                    # May happen when gas prices increase, not much we can do
                    logger.info("Not enough funds to restore old locks: %r", e)

    def start_upnp(self, ports):
        logger.debug("Starting upnp ...")
        self.port_mapper = PortMapperManager()
        self.port_mapper.discover()

        if self.port_mapper.available:
            for port in ports:
                self.port_mapper.create_mapping(port)
            self.port_mapper.update_node(self.node)

    def stop_network(self):
        logger.info("Stopping network ...")
        if self.p2pservice:
            self.p2pservice.stop_accepting()
            self.p2pservice.disconnect()
        if self.task_server:
            self.task_server.stop_accepting()
            self.task_server.disconnect()
        if self.port_mapper:
            self.port_mapper.quit()

    @rpc_utils.expose('ui.stop')
    @inlineCallbacks
    def pause(self):
        logger.info("Pausing ...")
        for service in self._services:
            if service.running:
                service.stop()

        if self.p2pservice:
            logger.debug("Pausing p2pservice")
            self.p2pservice.pause()
            self.p2pservice.disconnect()
        if self.task_server:
            logger.debug("Pausing task_server")
            yield self.task_server.pause()
        logger.info("Paused")

    @rpc_utils.expose('ui.start')
    def resume(self):
        logger.info("Resuming ...")
        for service in self._services:
            if not service.running:
                service.start()

        if self.p2pservice:
            self.p2pservice.resume()
            self.p2pservice.connect_to_network()
        if self.task_server:
            self.task_server.resume()
        logger.info("Resumed")

    def init_monitor(self):
        logger.debug("Starting monitor ...")
        metadata = self.__get_nodemetadatamodel()
        self.monitor = SystemMonitor(metadata, MONITOR_CONFIG)
        self.monitor.start()
        self.diag_service = DiagnosticsService(DiagnosticsOutputFormat.data)
        self.diag_service.register(
            VMDiagnosticsProvider(),
            lambda data: dispatcher.send(
                signal="golem.monitor",
                event="vm_snapshot",
                vm_data=data,
            ),
        )
        self.diag_service.start()

    @rpc_utils.expose('net.peer.connect')
    def connect(self, socket_address):
        if isinstance(socket_address, collections.Iterable):
            socket_address = SocketAddress(
                socket_address[0],
                int(socket_address[1])
            )

        logger.debug(
            "P2pservice connecting to %s on port %s",
            socket_address.address,
            socket_address.port
        )
        self.p2pservice.connect(socket_address)

    @inlineCallbacks
    def quit(self):
        logger.info('Shutting down ...')
        yield self.stop()

        self.transaction_system.stop()
        if self.diag_service:
            self.diag_service.unregister_all()
        if self.daemon_manager:
            self.daemon_manager.stop()

        dispatcher.send(signal='golem.monitor', event='shutdown')

        if self.db:
            self.db.close()

    def resource_collected(self, res_id):
        self.task_server.resource_collected(res_id)

    def resource_failure(self, res_id, reason):
        self.task_server.resource_failure(res_id, reason)

    @rpc_utils.expose('comp.tasks.check.abort')
    def abort_test_task(self) -> bool:
        logger.debug('Aborting test task ...')
        self.task_test_result = None
        if self.task_tester is not None:
            self.task_tester.end_comp()
            return True
        return False

    @rpc_utils.expose('comp.task.test.status')
    def check_test_status(self) -> Optional[Dict[str, Any]]:
        logger.debug('Checking test task status ...')
        result = self.task_test_result
        if result is None:
            return None
        result = result.copy()
        result['status'] = result['status'].value
        return result

    @rpc_utils.expose('comp.task.abort')
    @inlineCallbacks
    def abort_task(self, task_id):
        logger.debug('Aborting task "%r" ...', task_id)
        rtm = self.task_server.requested_task_manager
        if rtm.task_exists(task_id):
            yield deferred_from_future(rtm.abort_task(task_id))
        else:
            self.task_server.task_manager.abort_task(task_id)

    @rpc_utils.expose('comp.task.subtask.restart')
    @inlineCallbacks
    def restart_subtask(self, subtask_id):
        logger.debug("Restarting subtask %s", subtask_id)

        rtm = self.task_server.requested_task_manager
        subtask = rtm.get_requested_subtask(subtask_id)
        if subtask:
            self.funds_locker.add_subtask(subtask.task.task_id)
            yield deferred_from_future(rtm.restart_subtask(subtask_id))
            return

        task_manager = self.task_server.task_manager
        task_id = task_manager.get_task_id(subtask_id)
        self.funds_locker.add_subtask(task_id)
        task_manager.restart_subtask(subtask_id)

    @rpc_utils.expose('comp.task.delete')
    @inlineCallbacks
    def delete_task(self, task_id):
        logger.debug('Deleting task "%r" ...', task_id)
        self.task_server.remove_task_header(task_id)
        self.remove_task(task_id)
        rtm = self.task_server.requested_task_manager
        is_active = False
        if rtm.task_exists(task_id):
            is_active = rtm.get_requested_task(task_id).status.is_active()
            yield deferred_from_future(rtm.delete_task(task_id))
        else:
            is_active = self.task_server.task_manager.is_task_active(task_id)
            self.task_server.task_manager.delete_task(task_id)
        if is_active:
            self.funds_locker.remove_task(task_id)

    @rpc_utils.expose('comp.task.purge')
    @inlineCallbacks
    def purge_tasks(self):
        tasks = self.get_tasks()
        logger.debug('Deleting %d tasks ...', len(tasks))
        for t in tasks:
            yield self.delete_task(t['id'])

    @rpc_utils.expose('net.ident')
    def get_node(self):
        return self.node.to_dict()

    @rpc_utils.expose('net.ident.name')
    def get_node_name(self):
        name = self.config_desc.node_name
        return str(name) if name else ''

    def get_neighbours_degree(self):
        return self.p2pservice.get_peers_degree()

    def get_suggested_addr(self, key_id):
        return self.p2pservice.suggested_address.get(key_id)

    def get_peers(self):
        if self.p2pservice:
            return list(self.p2pservice.peers.values())
        return list()

    @rpc_utils.expose('net.peers.known')
    def get_known_peers(self):
        peers = self.p2pservice.incoming_peers or dict()
        return [
            DictSerializer.dump(p['node'], typed=False)
            for p in list(peers.values())
        ]

    @rpc_utils.expose('net.peers.connected')
    def get_connected_peers(self):
        peers = self.get_peers() or []
        return [
            DictSerializer.dump(PeerSessionInfo(p), typed=False) for p in peers
        ]

    @rpc_utils.expose('crypto.keys.pub')
    def get_public_key(self):
        return self.keys_auth.public_key

    def get_dir_manager(self):
        if self.task_server:
            return self.task_server.task_computer.dir_manager

    @rpc_utils.expose('crypto.keys.id')
    def get_key_id(self):
        return self.keys_auth.key_id

    @rpc_utils.expose('net.ident.key')
    def get_node_key(self):
        key = self.node.key
        return str(key) if key else None

    @rpc_utils.expose('env.opts')
    def get_settings(self):
        settings = DictSerializer.dump(self.config_desc, typed=False)

        for key, value in settings.items():
            if ConfigApprover.is_big_int(key):
                settings[key] = str(value)

        return settings

    @rpc_utils.expose('env.opt')
    def get_setting(self, key):
        if not hasattr(self.config_desc, key):
            raise KeyError("Unknown setting: {}".format(key))

        value = getattr(self.config_desc, key)
        if ConfigApprover.is_numeric(key):
            return str(value)
        return value

    @rpc_utils.expose('env.opt.update')
    def update_setting(self, key, value, update_config=True):
        logger.debug("updating setting %s = %r", key, value)
        if not hasattr(self.config_desc, key):
            raise KeyError("Unknown setting: {}".format(key))
        setattr(self.config_desc, key, value)
        if update_config:
            self.change_config(self.config_desc)

    @rpc_utils.expose('env.opts.update')
    def update_settings(self, settings_dict, run_benchmarks=False):
        logger.debug("updating settings: %r", settings_dict)
        for key, value in list(settings_dict.items()):
            if not hasattr(self.config_desc, key):
                raise KeyError("Unknown setting: {}".format(key))
            setattr(self.config_desc, key, value)
        self.change_config(self.config_desc, run_benchmarks)

    @rpc_utils.expose('env.datadir')
    def get_datadir(self):
        return str(self.datadir)

    @rpc_utils.expose('net.p2p.port')
    def get_p2p_port(self) -> int:
        if not self.p2pservice:
            return 0
        return self.p2pservice.cur_port

    @rpc_utils.expose('net.tasks.port')
    def get_task_server_port(self) -> int:
        if not self.task_server:
            return 0
        return self.task_server.cur_port

    def get_task_count(self):
        if self.task_server:
            return len(self.task_server.task_keeper.get_all_tasks())
        return 0

    @rpc_utils.expose('comp.task')
    def get_task(self, task_id: str) -> Optional[dict]:
        assert isinstance(self.task_server, TaskServer)

        task_dict = self.task_server.task_manager.get_task_dict(task_id)
        if not task_dict:
            # NEW taskmanager
            logger.debug('get_task(task_id=%r) - NEW', task_id)
            rtm = self.task_server.requested_task_manager
            task = rtm.get_requested_task(task_id)
            if not task:
                return None
            subtask_ids = rtm.get_requested_task_subtask_ids(task_id)
            # time_started
            if task.start_time is None:
                time_started = get_timestamp_utc()
                if task.status == taskstate.TaskStatus.errorCreating:
                    time_started -= 1
            else:
                time_started = task.start_time.timestamp()
            # proress and time_remaining
            finished_subtasks = rtm.count_finished_subtasks(task.task_id)
            progress = finished_subtasks / task.max_subtasks
            time_remaining = None
            if progress > 0.0 and not task.status.is_completed():
                elapsed = task.elapsed_seconds
                time_remaining = (elapsed / progress) - elapsed
            # type
            app_name = 'Unknown'
            if task.app_id in APPS:
                app = APPS[task.app_id]
                app_name = app.name
            # last_updated
            if task.end_time is None:
                last_updated = get_timestamp_utc()
            else:
                last_updated = task.end_time.timestamp()
            # compute_on
            compute_on = 'cpu'
            if task.env_id == DOCKER_GPU_ENV_ID:
                compute_on = 'gpu'
            # estimated_cost and estimated_fee
            subtask_price = calculate_subtask_payment(
                task.max_price_per_hour,
                task.subtask_timeout
            )
            estimated_cost = subtask_price * task.max_subtasks
            estimated_fee = self.transaction_system.eth_for_batch_payment(
                task.max_subtasks
            )
            task_dict = {
                'id': task.task_id,
                'time_remaining': time_remaining,
                'subtasks_count': task.max_subtasks,
                'status': task.status.value,
                'progress': progress,
                'time_started': time_started,
                'last_updated': last_updated,
                'name': task.name,
                'bid': float(task.max_price_per_hour) / denoms.ether,
                'compute_on': compute_on,
                'concent_enabled': task.concent_enabled,
                'subtask_timeout': str(
                    datetime.timedelta(seconds=task.subtask_timeout),
                ),
                'timeout': str(datetime.timedelta(seconds=task.task_timeout)),
                'type': app_name,
                'options': {
                    'output_path': task.output_directory
                },
                'estimated_cost': estimated_cost,
                'estimated_fee': estimated_fee,
            }
        else:
            # OLD taskmanager
            logger.debug('get_task(task_id=%r) - OLD', task_id)
            task_state = self.task_server.task_manager.query_task_state(task_id)
            subtask_ids = list(task_state.subtask_states.keys())

        # Get total value and total fee for payments for the given subtask IDs
        subtasks_payments = \
            self.transaction_system.get_subtasks_payments(subtask_ids)
        statuses_of_interest = (
            model.WalletOperation.STATUS.sent,
            model.WalletOperation.STATUS.confirmed,
        )
        all_sent = all(
            p.wallet_operation.status in statuses_of_interest
            for p in subtasks_payments)
        if not subtasks_payments or not all_sent:
            task_dict['cost'] = None
            task_dict['fee'] = None
        else:
            task_dict['cost'] = sum(
                p.wallet_operation.amount for p in subtasks_payments
            )
            task_dict['fee'] = \
                sum(
                    p.wallet_operation.gas_cost for p in subtasks_payments
                    if p.wallet_operation.gas_cost
                )

        # Convert to string because RPC serializer fails on big numbers
        # and enums
        for k in ('cost', 'fee', 'estimated_cost', 'estimated_fee',
                  'x-run-verification'):
            if k in task_dict and task_dict[k] is not None:
                task_dict[k] = str(task_dict[k])

        return task_dict

    @rpc_utils.expose('comp.tasks')
    def get_tasks(
            self,
            task_id: Optional[str] = None,
            return_created_tasks_only: bool = False,
    ) -> Union[Optional[dict], Iterable[dict]]:
        if not self.task_server:
            return []

        if task_id:
            return self.get_task(task_id)

        tm = self.task_server.task_manager
        rtm = self.task_server.requested_task_manager

        task_keys: Set[str] = set()
        task_keys.update(tm.tasks.keys())
        task_keys.update(rtm.get_requested_task_ids())

        tasks = (self.get_task(task_id) for task_id in sorted(task_keys))
        filter_fn = None

        if return_created_tasks_only:
            filter_fn = self._filter_task_created_status

        filtered_tasks = list(filter(filter_fn, tasks))
        return sorted(filtered_tasks, key=lambda task: task['time_started'])

    @staticmethod
    def _filter_task_created_status(task: Dict) -> bool:
        return bool(task) and task['status'] not in (
            taskstate.TaskStatus.creating.value,
            taskstate.TaskStatus.errorCreating.value)

    @rpc_utils.expose('comp.task.subtasks')
    def get_subtasks(self, task_id: str) \
            -> Optional[List[Dict]]:
        try:
            assert isinstance(self.task_server, TaskServer)
            tm = self.task_server.task_manager
            rtm = self.task_server.requested_task_manager

            if rtm.task_exists(task_id):
                subtasks = rtm.get_requested_task_subtasks(task_id)
                return [subtask.to_dict() for subtask in subtasks]
            return tm.get_subtasks_dict(task_id)
        except KeyError:
            logger.info("Task not found: '%s'", task_id)
            return None

    @rpc_utils.expose('comp.task.subtask')
    def get_subtask(
            self,
            subtask_id: str,
    ) -> Tuple[Optional[Dict], Optional[str]]:
        try:
            assert isinstance(self.task_server, TaskServer)
            tm = self.task_server.task_manager
            rtm = self.task_server.requested_task_manager

            subtask = rtm.get_requested_subtask(subtask_id)
            if subtask:
                return subtask.to_dict(), None
            subtask = tm.get_subtask_dict(subtask_id)
            return subtask, None
        except (AttributeError, KeyError):
            return None, "Subtask not found: '{}'".format(subtask_id)

    @rpc_utils.expose('comp.task.preview')
    def get_task_preview(self, task_id, single=False):
        self._assert_not_task_api_task(task_id)
        return self.task_server.task_manager.get_task_preview(task_id,
                                                              single=single)

    @rpc_utils.expose('comp.tasks.stats')
    def get_task_stats(self) -> Dict[str, Any]:
        return {
            'provider_state': self.get_provider_status(),
            'in_network': self.get_task_count(),
            'supported': self.get_supported_task_count(),
            'subtasks_computed': self.get_comp_stat('computed_tasks'),
            'subtasks_accepted': self.get_provider_stat('provider_sra_cnt'),
            'subtasks_rejected': self.get_provider_stat('provider_srr_cnt'),
            'subtasks_with_errors': self.get_comp_stat('tasks_with_errors'),
            'subtasks_with_timeout': self.get_comp_stat('tasks_with_timeout'),
        }

    def get_supported_task_count(self) -> int:
        if self.task_server:
            return len(self.task_server.task_keeper.supported_tasks)
        return 0

    @rpc_utils.expose('comp.tasks.unsupport')
    def get_unsupport_reasons(self, last_days):
        if last_days < 0:
            raise ValueError("Incorrect number of days: {}".format(last_days))
        if last_days > 0:
            return self.task_archiver.get_unsupport_reasons(last_days)
        return self.task_server.task_keeper.get_unsupport_reasons()

    def get_comp_stat(self, name):
        if self.task_server and self.task_server.task_computer:
            return self.task_server.task_computer.stats.get_stats(name)
        return None, None

    def get_provider_stat(self, name):
        if self.task_server and self.task_manager:
            return self.task_manager.provider_stats_manager.get_stats(name)
        return None, None

    @rpc_utils.expose('pay.balance')
    def get_balance(self):
        balances = self.transaction_system.get_balance()
        gnt_total = balances['gnt_available'] + balances['gnt_nonconverted']
        return {
            'av_gnt': str(balances['gnt_available']),
            'gnt': str(gnt_total),
            'gnt_lock': str(balances['gnt_locked']),
            'gnt_nonconverted': str(balances['gnt_nonconverted']),
            'eth': str(balances['eth_available']),
            'eth_lock': str(balances['eth_locked']),
            'block_number': str(balances['block_number']),
            'last_gnt_update': str(balances['gnt_update_time']),
            'last_eth_update': str(balances['eth_update_time']),
            'contract_addresses': {
                contract.name: address
                for contract, address in
                self.transaction_system.contract_addresses.items()
            }
        }

    @rpc_utils.expose('pay.deposit_balance')
    def get_deposit_balance(self):
        if not self.concent_service.available:
            return None

        balance: int = self.transaction_system.concent_balance()
        timelock: int = self.transaction_system.concent_timelock()

        class DepositStatus(msg_datastructures.StringEnum):
            locked = enum.auto()
            unlocking = enum.auto()
            unlocked = enum.auto()

        now = time.time()
        if timelock == 0:
            status = DepositStatus.locked
        elif timelock < now:
            status = DepositStatus.unlocked
        else:
            status = DepositStatus.unlocking
        return {
            'value': str(balance),
            'status': status.value,
            'timelock': str(timelock),
        }

    @rpc_utils.expose('pay.withdraw.gas_cost')
    def get_withdraw_gas_cost(
            self,
            amount: Union[str, int],
            destination: str,
            currency: str) -> int:
        if isinstance(amount, str):
            amount = int(amount)
        return self.transaction_system.get_withdraw_gas_cost(
            amount,
            destination,
            currency,
        )

    @rpc_utils.expose('pay.withdraw')
    def withdraw(
            self,
            amount: Union[str, int],
            destination: str,
            currency: str,
            gas_price: Optional[int] = None) -> List[str]:
        if isinstance(amount, str):
            amount = int(amount)
        # It returns a list for backwards compatibility with Electron.
        return [self.transaction_system.withdraw(
            amount,
            destination,
            currency,
            gas_price,
        )]

    @rpc_utils.expose('rep.comp')
    def get_computing_trust(self, node_id):
        if self.use_ranking():
            return self.ranking.get_computing_trust(node_id)
        return None

    @rpc_utils.expose('rep.requesting')
    def get_requesting_trust(self, node_id):
        if self.use_ranking():
            return self.ranking.get_requesting_trust(node_id)
        return None

    @rpc_utils.expose('env.use_ranking')
    def use_ranking(self):
        return bool(self.ranking)

    def want_to_start_task_session(self, key_id, node_id, conn_id):
        self.p2pservice.want_to_start_task_session(key_id, node_id, conn_id)

    # CLIENT CONFIGURATION
    def set_rpc_server(self, rpc_server):
        self.rpc_server = rpc_server
        return self.rpc_server.add_service(self)

    def change_config(self, new_config_desc, run_benchmarks=False):
        self.config_desc = self.config_approver.change_config(new_config_desc)

        hw_preset_present = bool(getattr(
            new_config_desc,
            'hardware_preset_name',
            None,
        ))
        if self._update_hw_preset and hw_preset_present:
            self._update_hw_preset(
                HardwarePresets.from_config(self.config_desc))

        if self.p2pservice:
            self.p2pservice.change_config(self.config_desc)
        if self.task_server:
            self.task_server.change_config(self.config_desc,
                                           run_benchmarks=run_benchmarks)

        self.enable_talkback(bool(self.config_desc.enable_talkback))
        self.app_config.change_config(self.config_desc)

        dispatcher.send(
            signal='golem.monitor',
            event='config_update',
            meta_data=self.__get_nodemetadatamodel()
        )

    def register_nodes_manager_client(self, nodes_manager_client):
        self.nodes_manager_client = nodes_manager_client

    @rpc_utils.expose('comp.task.state')
    def query_task_state(self, task_id):
        self._assert_not_task_api_task(task_id)
        state = self.task_server.task_manager.query_task_state(task_id)
        return DictSerializer.dump(state)

    def _assert_not_task_api_task(self, task_id: str):
        assert self.task_server
        if self.task_server.requested_task_manager.task_exists(task_id):
            raise RuntimeError("Task API: unsupported RPC call")

    def pull_resources(self, task_id, resources, client_options=None):
        self.resource_server.download_resources(
            resources,
            task_id,
            client_options=client_options
        )

    @rpc_utils.expose('res.dirs')
    def get_res_dirs(self):
        return {"total received data": self.get_received_files_dir(),
                "total distributed data": self.get_distributed_files_dir()}

    @rpc_utils.expose('res.dirs.size')
    def get_res_dirs_sizes(self):
        return {str(name): str(du(d))
                for name, d in list(self.get_res_dirs().items())}

    @rpc_utils.expose('res.dir')
    def get_res_dir(self, dir_type):
        if dir_type == DirectoryType.DISTRIBUTED:
            return self.get_distributed_files_dir()
        elif dir_type == DirectoryType.RECEIVED:
            return self.get_received_files_dir()
        raise Exception("Unknown dir type: {}".format(dir_type))

    def get_received_files_dir(self):
        return str(self.task_server.task_manager.get_task_manager_root())

    def get_distributed_files_dir(self):
        return str(self.resource_server.get_distributed_resource_root())

    @rpc_utils.expose('res.dir.clear')
    def clear_dir(self, dir_type, older_than_seconds: int = 0):
        if dir_type == DirectoryType.DISTRIBUTED:
            return self.remove_distributed_files(older_than_seconds)
        elif dir_type == DirectoryType.RECEIVED:
            return self.remove_received_files(older_than_seconds)
        raise Exception("Unknown dir type: {}".format(dir_type))

    def remove_distributed_files(self, older_than_seconds: int = 0):
        dir_manager = DirManager(self.datadir)
        dir_manager.clear_dir(self.get_distributed_files_dir(),
                              older_than_seconds)

    def remove_received_files(self, older_than_seconds: int = 0):
        dir_manager = DirManager(self.datadir)
        dir_manager.clear_dir(
            self.get_received_files_dir(), older_than_seconds)

    def remove_task(self, task_id):
        self.p2pservice.remove_task(task_id)

    def clean_old_tasks(self):
        logger.debug('Cleaning old tasks ...')
        now = get_timestamp_utc()
        for task in self.get_tasks():
            deadline = task['time_started'] \
                + string_to_timeout(task['timeout'])\
                + self.config_desc.clean_tasks_older_than_seconds
            if deadline <= now:
                logger.info('Task %s got too old. Deleting.', task['id'])
                self.delete_task(task['id'])

    @rpc_utils.expose('comp.tasks.known')
    def get_known_tasks(self):
        if self.task_server is None:
            return {}
        headers = {}
        for key, header in\
                list(self.task_server.task_keeper.task_headers.items()):  # noqa
            headers[str(key)] = DictSerializer.dump(header)
        return headers

    @rpc_utils.expose('comp.environments')
    def get_environments(self):
        envs = copy(self.environments_manager.get_environments())
        return [{
            'id': env_id,
            'supported': bool(env.check_support()),
            'accepted': env.is_accepted(),
            'performance': env.get_benchmark_result().performance,
            'cpu_usage': env.get_benchmark_result().cpu_usage,
            'min_accepted': env.get_min_accepted_performance(),
            'description': str(env.short_description)
        } for env_id, env in envs.items()]

    @rpc_utils.expose('comp.environment.benchmark')
    @inlineCallbacks
    def run_benchmark(self, env_id):
        deferred = Deferred()

        self.task_server.benchmark_manager.run_benchmark_for_env_id(
            env_id, deferred.callback, deferred.errback)

        result = yield deferred
        return result

    @rpc_utils.expose('comp.environment.enable')
    def enable_environment(self, env_id):
        try:
            return self.environments_manager.change_accept_tasks(env_id, True)
        except KeyError:
            return "No such environment"

    @rpc_utils.expose('comp.environment.disable')
    def disable_environment(self, env_id):
        try:
            return self.environments_manager.change_accept_tasks(env_id, False)
        except KeyError:
            return "No such environment"

    def send_gossip(self, gossip, send_to):
        return self.p2pservice.send_gossip(gossip, send_to)

    def send_stop_gossip(self):
        return self.p2pservice.send_stop_gossip()

    def collect_gossip(self):
        return self.p2pservice.pop_gossips()

    def collect_stopped_peers(self):
        return self.p2pservice.pop_stop_gossip_form_peers()

    def collect_neighbours_loc_ranks(self):
        return self.p2pservice.pop_neighbours_loc_ranks()

    def push_local_rank(self, node_id, loc_rank):
        self.p2pservice.push_local_rank(node_id, loc_rank)

    @rpc_utils.expose('comp.tasks.preset.save')
    @staticmethod
    def save_task_preset(preset_name, task_type, data):
        taskpreset.save_task_preset(preset_name, task_type, data)

    @rpc_utils.expose('comp.tasks.preset.get')
    @staticmethod
    def get_task_presets(task_type):
        logger.info("Loading presets for %s", task_type)
        return taskpreset.get_task_presets(task_type)

    @rpc_utils.expose('comp.tasks.preset.delete')
    @staticmethod
    def delete_task_preset(task_type, preset_name):
        taskpreset.delete_task_preset(task_type, preset_name)

    def _publish(self, event_name, *args, **kwargs):
        if self.rpc_publisher:
            self.rpc_publisher.publish(event_name, *args, **kwargs)

    def lock_config(self, on=True):
        self._publish(UI.evt_lock_config, on)

    def config_changed(self):
        self._publish(Environment.evt_opts_changed)

    def __get_nodemetadatamodel(self):
        return NodeMetadataModel(
            client=self,
            os_info=OSInfo.get_os_info(),
            ver=golem.__version__
        )

    def _make_connection_status_raw_data(self) -> Dict[str, Any]:
        listen_port = self.get_p2p_port()
        task_server_port = self.get_task_server_port()

        status: Dict[str, Any] = dict()

        if listen_port == 0 or task_server_port == 0:
            status['listening'] = False
            return status
        status['listening'] = True

        status['port_statuses'] = deepcopy(self.node.port_statuses)
        status['connected'] = bool(self.get_connected_peers())
        return status

    @staticmethod
    def _make_connection_status_human_readable_message(
            status: Dict[str, Any]) -> str:
        # To create the message use the data that is only in `status` dict.
        # This is to make sure that message has no additional information.

        if not status['listening']:
            return "Application not listening, check config file."

        messages = []

        if status['port_statuses']:
            port_statuses = ", ".join(
                "{}: {}".format(port, port_status)
                for port, port_status in status['port_statuses'].items())
            messages.append("Port(s) {}.".format(port_statuses))

        if status['connected']:
            messages.append("Connected")
        else:
            messages.append("Not connected to Golem Network, "
                            "check seed parameters.")

        return ' '.join(messages)

    @rpc_utils.expose('net.status')
    def connection_status(self) -> Dict[str, Any]:
        status = self._make_connection_status_raw_data()
        status['msg'] \
            = Client._make_connection_status_human_readable_message(status)
        return status

    def has_assigned_task(self) -> bool:
        if self.task_server is None:
            return False
        return self.task_server.task_computer.has_assigned_task()

    def get_provider_status(self) -> Dict[str, Any]:
        # golem is starting
        if self.task_server is None:
            return {
                'status': 'Golem is starting',
            }

        task_computer = self.task_server.task_computer

        # computing
        subtask_progress: Optional[ComputingSubtaskStateSnapshot] = \
            task_computer.get_progress()
        if subtask_progress is not None:
            environment: Optional[str] = \
                task_computer.get_environment()
            return {
                'status': 'Computing',
                'subtask': subtask_progress.__dict__,
                'environment': environment
            }

        # not accepting tasks
        if not self.config_desc.accept_tasks:
            return {
                'status': 'Not accepting tasks',
            }

        return {
            'status': 'Idle',
        }

    @rpc_utils.expose('golem.status')
    @staticmethod
    def get_golem_status():
        return StatusPublisher.last_status()

    @rpc_utils.expose('env.hw.preset.activate')
    @inlineCallbacks
    def activate_hw_preset(self, name, run_benchmarks=False):
        config_changed = HardwarePresets.update_config(name, self.config_desc)
        run_benchmarks = run_benchmarks or config_changed

        if hasattr(self, 'task_server') and self.task_server:
            deferred = self.task_server.change_config(
                self.config_desc, run_benchmarks=run_benchmarks)

            result = yield deferred
            logger.info('change hw config result: %r', result)
            return self.environments_manager.get_performance_values()
        return None

    @staticmethod
    def enable_talkback(value: bool):
        enable_sentry_logger(value)

    @rpc_utils.expose('net.peer.block')
    def block_node(
            self,
            node_id: Union[str, list],
            timeout_seconds: int = -1,
    ) -> Tuple[bool, Optional[str]]:
        if not self.task_server:
            return False, 'Client is not ready'

        try:
            if isinstance(node_id, str):
                node_id = [node_id]

            for item in node_id:
                self.task_server.disallow_node(item,
                                               timeout_seconds=timeout_seconds,
                                               persist=True)

            return True, None
        except Exception as e:  # pylint: disable=broad-except
            return False, str(e)


class DoWorkService(LoopingCallService):
    _client = None  # type: Client

    def __init__(self, client: Client) -> None:
        super().__init__(interval_seconds=1)
        self._client = client

    def start(self):
        super().start(now=False)

    def _run(self):
        # TODO: split it into separate services. Issue #2431

        if self._client.config_desc.send_pings:
            self._client.p2pservice.ping_peers(
                self._client.config_desc.pings_interval)

        try:
            self._client.p2pservice.sync_network()
        except Exception:
            logger.exception("p2pservice.sync_network failed")
        try:
            self._client.task_server.sync_network()
        except Exception:
            logger.exception("task_server.sync_network failed")
        try:
            self._client.resource_server.sync_network()
        except Exception:
            logger.exception("resource_server.sync_network failed")
        try:
            self._client.ranking.sync_network()
        except Exception:
            logger.exception("ranking.sync_network failed")


class MonitoringPublisherService(LoopingCallService):
    _task_server = None  # type: TaskServer

    def __init__(self,
                 task_server: TaskServer,
                 interval_seconds: int) -> None:
        super().__init__(interval_seconds)
        self._task_server = task_server

    def _run(self):
        dispatcher.send(
            signal='golem.monitor',
            event='stats_snapshot',
            known_tasks=len(self._task_server.task_keeper.get_all_tasks()),
            supported_tasks=len(self._task_server.task_keeper.supported_tasks),
            stats=self._task_server.task_computer.stats,
        )
        dispatcher.send(
            signal='golem.monitor',
            event='task_computer_snapshot',
            task_computer=self._task_server.task_computer,
        )
        dispatcher.send(
            signal='golem.monitor',
            event='requestor_stats_snapshot',
            current_stats=(self._task_server.task_manager
                           .requestor_stats_manager.get_current_stats()),
            finished_stats=(self._task_server.task_manager
                            .requestor_stats_manager.get_finished_stats())
        )
        dispatcher.send(
            signal='golem.monitor',
            event='requestor_aggregate_stats_snapshot',
            stats=(self._task_server.task_manager.requestor_stats_manager
                   .get_aggregate_stats()),
        )
        dispatcher.send(
            signal='golem.monitor',
            event='provider_stats_snapshot',
            stats=(self._task_server.task_manager.comp_task_keeper
                   .provider_stats_manager.keeper.global_stats),
        )


class NetworkConnectionPublisherService(LoopingCallService):
    _client = None  # type: Client

    def __init__(self,
                 client: Client,
                 interval_seconds: int) -> None:
        super().__init__(interval_seconds)
        self._client = client
        self._last_value = self.poll()

    def _run_async(self):
        # Skip the async_run call and publish events in the main thread
        self._run()

    def _run(self):
        current_value = self.poll()
        if current_value == self._last_value:
            return
        self._last_value = current_value
        self._client._publish(  # pylint: disable=protected-access
            Network.evt_connection,
            self._last_value,
        )

    def poll(self) -> Dict[str, Any]:
        return self._client.connection_status()


class TaskArchiverService(LoopingCallService):
    _task_archiver = None  # type: TaskArchiver

    def __init__(self,
                 task_archiver: TaskArchiver) -> None:
        super().__init__(interval_seconds=TASKARCHIVE_MAINTENANCE_INTERVAL)
        self._task_archiver = task_archiver

    def _run(self):
        self._task_archiver.do_maintenance()


class ResourceCleanerService(LoopingCallService):
    _client = None  # type: Client
    older_than_seconds = 0  # type: int

    def __init__(self,
                 client: Client,
                 interval_seconds: int,
                 older_than_seconds: int) -> None:
        super().__init__(interval_seconds)
        self._client = client
        self.older_than_seconds = older_than_seconds

    def _run(self):
        # TODO: is any synchronization needed here? golemcli has none.
        # Issue #2432
        self._client.remove_distributed_files(self.older_than_seconds)
        self._client.remove_received_files(self.older_than_seconds)


class TaskCleanerService(LoopingCallService):
    _client = None  # type: Client

    def __init__(self,
                 client: Client,
                 interval_seconds: int) -> None:
        super().__init__(interval_seconds)
        self._client = client

    def _run(self):
        self._client.clean_old_tasks()


class MaskUpdateService(LoopingCallService):

    def __init__(
            self,
            requested_task_manager: 'RequestedTaskManager',
            old_task_manager: 'TaskManager',
            interval_seconds: int,
            update_num_bits: int
    ) -> None:
        self._requested_task_manager = requested_task_manager
        self._old_task_manager = old_task_manager
        self._update_num_bits = update_num_bits
        self._interval = interval_seconds
        super().__init__(interval_seconds)

    def _run(self):
        logger.debug('Updating masks')
        old_task_manager = self._old_task_manager
        requested_task_manager = self._requested_task_manager
        # Using list() because tasks could be changed by another thread
        for task_id, task in list(old_task_manager.tasks.items()):
            if not old_task_manager.task_needs_computation(task_id):
                continue
            task_state = old_task_manager.query_task_state(task_id)
            if task_state.elapsed_time < self._interval:
                continue

            old_task_manager.decrease_task_mask(
                task_id=task_id,
                num_bits=self._update_num_bits)
            logger.info('Updating mask for task %r Mask size: %r',
                        task_id, task.header.mask.num_bits)

        for db_task in requested_task_manager.get_started_tasks():
            elapsed_seconds = db_task.elapsed_seconds
            if elapsed_seconds is None or elapsed_seconds < self._interval:
                continue

            requested_task_manager.decrease_task_mask(
                task_id=db_task.task_id,
                num_bits=self._update_num_bits)
            logger.info(
                'Updating mask. task_id=%r, new_mask_size=%r',
                db_task.task_id,
                msg_datastructures.masking.Mask(db_task.mask).num_bits)


class DailyJobsService(LoopingCallService):
    def __init__(self):
        super().__init__(
            interval_seconds=datetime.timedelta(days=1).total_seconds(),
        )

    def _run(self) -> None:
        jobs = (
            nodeskeeper.sweep,
            msg_queue.sweep,
            lambda: logger.info(
                "Time marker. time(): %s now(): %s, utcnow(): %s, delta: %s",
                time.time(),
                datetime.datetime.now(),
                datetime.datetime.utcnow(),
                datetime.datetime.now() - datetime.datetime.utcnow(),
            ),
        )
        logger.info('Running daily jobs')
        for job in jobs:
            try:
                job()
            except Exception as e:  # pylint: disable=broad-except
                logger.warning("Daily job failed. job=%r, e=%s", job, e)
                logger.debug("Details", exc_info=True)
        logger.info('Finished daily jobs')
