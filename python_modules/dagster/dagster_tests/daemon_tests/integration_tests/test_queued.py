from dagster.core.host_representation import PipelineHandle
from dagster.core.storage.pipeline_run import PipelineRun
from dagster.core.test_utils import create_run_for_test, poll_for_finished_run
from dagster.utils import merge_dicts
from dagster.utils.external import external_pipeline_from_run

from .utils import setup_instance, start_daemon


def create_run(instance, pipeline_handle, **kwargs):  # pylint: disable=redefined-outer-name
    pipeline_args = merge_dicts(
        {
            "pipeline_name": "foo_pipeline",
            "external_pipeline_origin": pipeline_handle.get_external_origin(),
        },
        kwargs,
    )
    return create_run_for_test(instance, **pipeline_args)


def assert_events_in_order(logs, expected_events):

    logged_events = [log.dagster_event.event_type_value for log in logs if log.is_dagster_event]
    filtered_logged_events = [event for event in logged_events if event in expected_events]

    assert filtered_logged_events == expected_events


def test_queue_from_schedule_and_sensor(tmpdir, foo_example_repo):
    dagster_home_path = tmpdir.strpath
    with setup_instance(
        dagster_home_path,
        """run_coordinator:
    module: dagster.core.run_coordinator
    class: QueuedRunCoordinator
    config:
        dequeue_interval_seconds: 1
    """,
    ) as instance:
        external_schedule = foo_example_repo.get_external_schedule("never_run_schedule")
        external_sensor = foo_example_repo.get_external_sensor("never_on_sensor")

        foo_pipeline_handle = PipelineHandle("foo_pipeline", foo_example_repo.handle)

        instance.start_schedule_and_update_storage_state(external_schedule)
        instance.start_sensor(external_sensor)

        with start_daemon(timeout=180):
            run = create_run(instance, foo_pipeline_handle)
            with external_pipeline_from_run(run) as external_pipeline:
                instance.submit_run(run.run_id, external_pipeline)

                runs = [
                    poll_for_finished_run(instance, run.run_id),
                    poll_for_finished_run(
                        instance, run_tags=PipelineRun.tags_for_sensor(external_sensor)
                    ),
                    poll_for_finished_run(
                        instance,
                        run_tags=PipelineRun.tags_for_schedule(external_schedule),
                        timeout=90,
                    ),
                ]

                for run in runs:
                    logs = instance.all_logs(run.run_id)
                    assert_events_in_order(
                        logs,
                        [
                            "PIPELINE_ENQUEUED",
                            "PIPELINE_DEQUEUED",
                            "PIPELINE_STARTING",
                            "PIPELINE_START",
                            "PIPELINE_SUCCESS",
                        ],
                    )


def test_queued_runs(tmpdir, foo_pipeline_handle):
    dagster_home_path = tmpdir.strpath
    with setup_instance(
        dagster_home_path,
        """run_coordinator:
    module: dagster.core.run_coordinator
    class: QueuedRunCoordinator
    config:
        dequeue_interval_seconds: 1
    """,
    ) as instance:
        with start_daemon():
            run = create_run(instance, foo_pipeline_handle)
            with external_pipeline_from_run(run) as external_pipeline:
                instance.submit_run(run.run_id, external_pipeline)

                poll_for_finished_run(instance, run.run_id)

                logs = instance.all_logs(run.run_id)
                assert_events_in_order(
                    logs,
                    [
                        "PIPELINE_ENQUEUED",
                        "PIPELINE_DEQUEUED",
                        "PIPELINE_STARTING",
                        "PIPELINE_START",
                        "PIPELINE_SUCCESS",
                    ],
                )
