import sys
import threading
from abc import ABC, abstractmethod, abstractproperty
from collections import namedtuple

from dagster import check
from dagster.api.get_server_id import sync_get_server_id
from dagster.api.list_repositories import sync_list_repositories_grpc
from dagster.api.snapshot_repository import sync_get_streaming_external_repositories_data_grpc
from dagster.core.definitions.reconstructable import repository_def_from_pointer
from dagster.core.errors import DagsterInvariantViolationError
from dagster.core.host_representation.grpc_server_state_subscriber import (
    LocationStateChangeEvent,
    LocationStateChangeEventType,
)
from dagster.core.host_representation.origin import (
    ExternalRepositoryOrigin,
    GrpcServerRepositoryLocationOrigin,
    InProcessRepositoryLocationOrigin,
    ManagedGrpcPythonEnvRepositoryLocationOrigin,
    RepositoryLocationOrigin,
)
from dagster.core.host_representation.selector import PipelineSelector
from dagster.core.origin import RepositoryPythonOrigin
from dagster.utils import merge_dicts


def _get_repository_python_origin(
    executable_path, repository_code_pointer_dict, repository_name, container_image
):
    if repository_name not in repository_code_pointer_dict:
        raise DagsterInvariantViolationError(
            "Unable to find repository name {} on GRPC server.".format(repository_name)
        )

    code_pointer = repository_code_pointer_dict[repository_name]
    return RepositoryPythonOrigin(
        executable_path=executable_path, code_pointer=code_pointer, container_image=container_image
    )


class RepositoryLocationHandle(ABC):
    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, traceback):
        self.cleanup()

    def __del__(self):
        self.cleanup()

    def cleanup(self):
        pass

    def add_state_subscriber(self, subscriber):
        pass

    @abstractmethod
    def get_repository_python_origin(self, repository_name):
        pass

    @abstractmethod
    def create_location(self):
        pass

    @abstractproperty
    def origin(self):
        pass

    def get_display_metadata(self):
        return self.origin.get_display_metadata()


class GrpcServerRepositoryLocationHandle(RepositoryLocationHandle):
    """
    Represents a gRPC server that Dagster is not responsible for managing.
    """

    def __init__(
        self,
        origin,
        host=None,
        port=None,
        socket=None,
        server_id=None,
        heartbeat=False,
        watch_server=True,
    ):
        from dagster.grpc.client import DagsterGrpcClient, client_heartbeat_thread
        from dagster.grpc.server_watcher import create_grpc_watch_thread

        self._origin = check.inst_param(origin, "origin", RepositoryLocationOrigin)

        if isinstance(self._origin, GrpcServerRepositoryLocationOrigin):
            self._port = self.origin.port
            self._socket = self.origin.socket
            self._host = self.origin.host
            self._use_ssl = bool(self.origin.use_ssl)
        else:
            self._port = check.opt_int_param(port, "port")
            self._socket = check.opt_str_param(socket, "socket")
            self._host = check.str_param(host, "host")
            self._use_ssl = False

        self._watch_thread_shutdown_event = None
        self._watch_thread = None

        self._heartbeat_shutdown_event = None
        self._heartbeat_thread = None

        self._heartbeat = check.bool_param(heartbeat, "heartbeat")
        self._watch_server = check.bool_param(watch_server, "watch_server")

        self.server_id = None
        self._external_repositories_data = None

        try:
            self.client = DagsterGrpcClient(
                port=self._port,
                socket=self._socket,
                host=self._host,
                use_ssl=self._use_ssl,
            )
            list_repositories_response = sync_list_repositories_grpc(self.client)

            self.server_id = server_id if server_id else sync_get_server_id(self.client)
            self.repository_names = set(
                symbol.repository_name for symbol in list_repositories_response.repository_symbols
            )

            if self._heartbeat:
                self._heartbeat_shutdown_event = threading.Event()

                self._heartbeat_thread = threading.Thread(
                    target=client_heartbeat_thread,
                    args=(
                        self.client,
                        self._heartbeat_shutdown_event,
                    ),
                    name="grpc-client-heartbeat",
                )
                self._heartbeat_thread.daemon = True
                self._heartbeat_thread.start()

            if self._watch_server:
                self._state_subscribers = []
                self._watch_thread_shutdown_event, self._watch_thread = create_grpc_watch_thread(
                    self.client,
                    on_updated=lambda new_server_id: self._send_state_event_to_subscribers(
                        LocationStateChangeEvent(
                            LocationStateChangeEventType.LOCATION_UPDATED,
                            location_name=self.location_name,
                            message="Server has been updated.",
                            server_id=new_server_id,
                        )
                    ),
                    on_error=lambda: self._send_state_event_to_subscribers(
                        LocationStateChangeEvent(
                            LocationStateChangeEventType.LOCATION_ERROR,
                            location_name=self.location_name,
                            message="Unable to reconnect to server. You can reload the server once it is "
                            "reachable again",
                        )
                    ),
                )

                self._watch_thread.start()

            self.executable_path = list_repositories_response.executable_path
            self.repository_code_pointer_dict = (
                list_repositories_response.repository_code_pointer_dict
            )

            self.container_image = self._reload_current_image()

            self._external_repositories_data = sync_get_streaming_external_repositories_data_grpc(
                self.client,
                self,
            )
        except:
            self.cleanup()
            raise

    @property
    def origin(self):
        return self._origin

    def add_state_subscriber(self, subscriber):
        if self._watch_server:
            self._state_subscribers.append(subscriber)

    def _send_state_event_to_subscribers(self, event):
        check.inst_param(event, "event", LocationStateChangeEvent)
        for subscriber in self._state_subscribers:
            subscriber.handle_event(event)

    def cleanup(self):
        if self._heartbeat_shutdown_event:
            self._heartbeat_shutdown_event.set()
            self._heartbeat_shutdown_event = None

        if self._watch_thread_shutdown_event:
            self._watch_thread_shutdown_event.set()
            self._watch_thread_shutdown_event = None

        if self._heartbeat_thread:
            self._heartbeat_thread.join()
            self._heartbeat_thread = None

        if self._watch_thread:
            self._watch_thread.join()
            self._watch_thread = None

    @property
    def port(self):
        return self._port

    @property
    def socket(self):
        return self._socket

    @property
    def host(self):
        return self._host

    @property
    def use_ssl(self):
        return self._use_ssl

    @property
    def location_name(self):
        return self.origin.location_name

    def _reload_current_image(self):
        return self.client.get_current_image().current_image

    def get_repository_python_origin(self, repository_name):
        return _get_repository_python_origin(
            self.executable_path,
            self.repository_code_pointer_dict,
            repository_name,
            self.container_image,
        )

    def create_location(self):
        from dagster.core.host_representation.repository_location import (
            GrpcServerRepositoryLocation,
        )

        return GrpcServerRepositoryLocation(self)

    def create_external_repositories(self):
        from dagster.core.host_representation.external import ExternalRepository

        return {
            repo_name: ExternalRepository(
                repo_data,
                RepositoryHandle(
                    repository_name=repo_name,
                    repository_location_handle=self,
                ),
            )
            for repo_name, repo_data in self._external_repositories_data.items()
        }

    def get_display_metadata(self):
        return merge_dicts(
            self.origin.get_display_metadata(),
            ({"image": self.container_image} if self.container_image else {}),
        )


class ManagedGrpcPythonEnvRepositoryLocationHandle(RepositoryLocationHandle):
    """
    A Python environment for which Dagster is managing a gRPC server.
    """

    def __init__(self, origin):
        from dagster.grpc.client import client_heartbeat_thread
        from dagster.grpc.server import GrpcServerProcess

        self.grpc_server_process = None
        self.client = None
        self.heartbeat_shutdown_event = None
        self.heartbeat_thread = None

        self._origin = check.inst_param(
            origin, "origin", ManagedGrpcPythonEnvRepositoryLocationOrigin
        )
        loadable_target_origin = origin.loadable_target_origin

        self._external_repositories_data = None

        try:
            self.grpc_server_process = GrpcServerProcess(
                loadable_target_origin=loadable_target_origin,
                heartbeat=True,
            )

            self.client = self.grpc_server_process.create_ephemeral_client()

            self.heartbeat_shutdown_event = threading.Event()

            self.heartbeat_thread = threading.Thread(
                target=client_heartbeat_thread,
                args=(
                    self.client,
                    self.heartbeat_shutdown_event,
                ),
                name="grpc-client-heartbeat",
            )
            self.heartbeat_thread.daemon = True
            self.heartbeat_thread.start()

            list_repositories_response = sync_list_repositories_grpc(self.client)

            self.repository_code_pointer_dict = (
                list_repositories_response.repository_code_pointer_dict
            )
            self.container_image = self.client.get_current_image().current_image

            self._external_repositories_data = sync_get_streaming_external_repositories_data_grpc(
                self.client,
                self,
            )
        except:
            self.cleanup()
            raise

    def create_external_repositories(self):
        from dagster.core.host_representation.external import ExternalRepository

        return {
            repo_name: ExternalRepository(
                repo_data,
                RepositoryHandle(
                    repository_name=repo_name,
                    repository_location_handle=self,
                ),
            )
            for repo_name, repo_data in self._external_repositories_data.items()
        }

    def get_repository_python_origin(self, repository_name):
        return _get_repository_python_origin(
            self.executable_path,
            self.repository_code_pointer_dict,
            repository_name,
            self.container_image,
        )

    @property
    def origin(self):
        return self._origin

    @property
    def executable_path(self):
        return self.loadable_target_origin.executable_path

    @property
    def location_name(self):
        return self.origin.location_name

    @property
    def loadable_target_origin(self):
        return self.origin.loadable_target_origin

    @property
    def repository_names(self):
        return set(self.repository_code_pointer_dict.keys())

    @property
    def host(self):
        return "localhost"

    @property
    def port(self):
        return self.grpc_server_process.port

    @property
    def socket(self):
        return self.grpc_server_process.socket

    @property
    def use_ssl(self):
        return False

    def cleanup(self):
        if self.heartbeat_shutdown_event:
            self.heartbeat_shutdown_event.set()
            self.heartbeat_shutdown_event = None

        if self.heartbeat_thread:
            self.heartbeat_thread.join()
            self.heartbeat_thread = None

        if self.client:
            self.client.cleanup_server()
            self.client = None

    @property
    def is_cleaned_up(self):
        return not self.client

    def create_location(self):
        from dagster.core.host_representation.repository_location import (
            GrpcServerRepositoryLocation,
        )

        return GrpcServerRepositoryLocation(self)

    def get_display_metadata(self):
        return merge_dicts(
            self.origin.get_display_metadata(),
            ({"image": self.container_image} if self.container_image else {}),
        )


class InProcessRepositoryLocationHandle(RepositoryLocationHandle):
    def __init__(self, origin):
        self._origin = check.inst_param(origin, "origin", InProcessRepositoryLocationOrigin)

        pointer = self.origin.recon_repo.pointer
        repo_def = repository_def_from_pointer(pointer)
        self.repository_code_pointer_dict = {repo_def.name: pointer}

    @property
    def origin(self):
        return self._origin

    @property
    def location_name(self):
        return self.origin.location_name

    def get_repository_python_origin(self, repository_name):
        return _get_repository_python_origin(
            sys.executable,
            self.repository_code_pointer_dict,
            repository_name,
            None,
        )

    def create_location(self):
        from dagster.core.host_representation.repository_location import InProcessRepositoryLocation

        return InProcessRepositoryLocation(self)


class RepositoryHandle(
    namedtuple("_RepositoryHandle", "repository_name repository_location_handle")
):
    def __new__(cls, repository_name, repository_location_handle):
        return super(RepositoryHandle, cls).__new__(
            cls,
            check.str_param(repository_name, "repository_name"),
            check.inst_param(
                repository_location_handle, "repository_location_handle", RepositoryLocationHandle
            ),
        )

    def get_external_origin(self):
        return ExternalRepositoryOrigin(
            self.repository_location_handle.origin,
            self.repository_name,
        )

    def get_python_origin(self):
        return self.repository_location_handle.get_repository_python_origin(self.repository_name)


class PipelineHandle(namedtuple("_PipelineHandle", "pipeline_name repository_handle")):
    def __new__(cls, pipeline_name, repository_handle):
        return super(PipelineHandle, cls).__new__(
            cls,
            check.str_param(pipeline_name, "pipeline_name"),
            check.inst_param(repository_handle, "repository_handle", RepositoryHandle),
        )

    def to_string(self):
        return "{self.location_name}.{self.repository_name}.{self.pipeline_name}".format(self=self)

    @property
    def repository_name(self):
        return self.repository_handle.repository_name

    @property
    def location_name(self):
        return self.repository_handle.repository_location_handle.location_name

    def get_external_origin(self):
        return self.repository_handle.get_external_origin().get_pipeline_origin(self.pipeline_name)

    def get_python_origin(self):
        return self.repository_handle.get_python_origin().get_pipeline_origin(self.pipeline_name)

    def to_selector(self):
        return PipelineSelector(self.location_name, self.repository_name, self.pipeline_name, None)


class JobHandle(namedtuple("_JobHandle", "job_name repository_handle")):
    def __new__(cls, job_name, repository_handle):
        return super(JobHandle, cls).__new__(
            cls,
            check.str_param(job_name, "job_name"),
            check.inst_param(repository_handle, "repository_handle", RepositoryHandle),
        )

    @property
    def repository_name(self):
        return self.repository_handle.repository_name

    @property
    def location_name(self):
        return self.repository_handle.repository_location_handle.location_name

    def get_external_origin(self):
        return self.repository_handle.get_external_origin().get_job_origin(self.job_name)


class PartitionSetHandle(namedtuple("_PartitionSetHandle", "partition_set_name repository_handle")):
    def __new__(cls, partition_set_name, repository_handle):
        return super(PartitionSetHandle, cls).__new__(
            cls,
            check.str_param(partition_set_name, "partition_set_name"),
            check.inst_param(repository_handle, "repository_handle", RepositoryHandle),
        )

    @property
    def repository_name(self):
        return self.repository_handle.repository_name

    @property
    def location_name(self):
        return self.repository_handle.repository_location_handle.location_name

    def get_external_origin(self):
        return self.repository_handle.get_external_origin().get_partition_set_origin(
            self.partition_set_name
        )
