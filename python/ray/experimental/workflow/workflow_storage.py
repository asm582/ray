"""
This module is higher-level abstraction of storage directly used by
workflows.
"""

import asyncio
from typing import Dict, List, Optional, Any, Callable, Tuple, Union
from dataclasses import dataclass

import ray
from ray._private import signature
from ray.experimental.workflow import storage
from ray.experimental.workflow.common import (Workflow, WorkflowData, StepID,
                                              WorkflowMetaData, WorkflowStatus,
                                              WorkflowRef, StepType)
from ray.experimental.workflow import workflow_context
from ray.experimental.workflow import serialization_context
from ray.experimental.workflow.storage import (DataLoadError, DataSaveError,
                                               KeyNotFoundError)

ArgsType = Tuple[List[Any], Dict[str, Any]]  # args and kwargs

# constants used for keys
OBJECTS_DIR = "objects"
STEPS_DIR = "steps"
STEP_INPUTS_METADATA = "inputs.json"
STEP_OUTPUTS_METADATA = "outputs.json"
STEP_ARGS = "args.pkl"
STEP_OUTPUT = "output.pkl"
STEP_FUNC_BODY = "func_body.pkl"
CLASS_BODY = "class_body.pkl"
WORKFLOW_META = "workflow_meta.json"
WORKFLOW_PROGRESS = "progress.json"
# Without this counter, we're going to scan all steps to get the number of
# steps with a given name. This can be very expensive if there are too
# many duplicates.
DUPLICATE_NAME_COUNTER = "duplicate_name_counter"


# TODO: Get rid of this and use asyncio.run instead once we don't support py36
def asyncio_run(coro):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)


@dataclass
class StepInspectResult:
    # The step output checkpoint exists and valid. If this field
    # is set, we do not set all other fields below.
    output_object_valid: bool = False
    # The ID of the step that could contain the output checkpoint of this
    # step. If this field is set, we do not set all other fields below.
    output_step_id: Optional[StepID] = None
    # The step input arguments checkpoint exists and valid.
    args_valid: bool = False
    # The step function body checkpoint exists and valid.
    func_body_valid: bool = False
    # The object refs in the inputs of the workflow.
    object_refs: Optional[List[str]] = None
    # The workflows in the inputs of the workflow.
    workflows: Optional[List[str]] = None
    # The dynamically referenced workflows in the input of the workflow.
    workflow_refs: Optional[List[str]] = None
    # The num of retry for application exception
    max_retries: int = 1
    # Whether the user want to handle the exception mannually
    catch_exceptions: bool = False
    # ray_remote options
    ray_options: Optional[Dict[str, Any]] = None
    # type of workflow step
    step_type: Optional[StepType] = None

    def is_recoverable(self) -> bool:
        return (self.output_object_valid or self.output_step_id
                or (self.args_valid and self.object_refs is not None
                    and self.workflows is not None and self.func_body_valid))


@dataclass
class StepStatus:
    # does the step output checkpoint exist?
    output_object_exists: bool
    # does the step output metadata exist?
    output_metadata_exists: bool
    # does the step input metadata exist?
    input_metadata_exists: bool
    # does the step input argument exist?
    args_exists: bool
    # does the step function body exist?
    func_body_exists: bool


class WorkflowStorage:
    """Access workflow in storage. This is a higher-level abstraction,
    which does not care about the underlining storage implementation."""

    def __init__(self, workflow_id: str, store: storage.Storage):
        self._storage = store
        self._workflow_id = workflow_id

    def load_step_output(self, step_id: StepID) -> Any:
        """Load the output of the workflow step from checkpoint.

        Args:
            step_id: ID of the workflow step.

        Returns:
            Output of the workflow step.
        """
        return asyncio_run(self._get(self._key_step_output(step_id)))

    def save_step_output(self, step_id: StepID, ret: Union[Workflow, Any],
                         outer_most_step_id: Optional[StepID]) -> None:
        """When a workflow step returns,
        1. If the returned object is a workflow, this means we are a nested
           workflow. We save the output metadata that points to the workflow.
        2. Otherwise, checkpoint the output.

        Args:
            step_id: The ID of the workflow step. If it is an empty string,
                it means we are in the workflow job driver process.
            ret: The returned object from a workflow step.
            outer_most_step_id: See
                "step_executor.execute_workflow" for explanation.
        """
        tasks = []
        if isinstance(ret, Workflow):
            # This workflow step returns a nested workflow.
            assert step_id != ret.step_id
            tasks.append(
                self._put(
                    self._key_step_output_metadata(step_id),
                    {"output_step_id": ret.step_id}, True))
            dynamic_output_id = ret.step_id
        else:
            # This workflow step returns a object.
            ret = ray.get(ret) if isinstance(ret, ray.ObjectRef) else ret
            tasks.append(self._put(self._key_step_output(step_id), ret))
            dynamic_output_id = step_id
        # outer_most_step_id == "" indicates the root step of a workflow.
        # This would directly update "outputs.json" in the workflow dir,
        # and we want to avoid it.
        if outer_most_step_id is not None and outer_most_step_id != "":
            tasks.append(
                self._update_dynamic_output(outer_most_step_id,
                                            dynamic_output_id))
        asyncio_run(asyncio.gather(*tasks))

    def load_step_func_body(self, step_id: StepID) -> Callable:
        """Load the function body of the workflow step.

        Args:
            step_id: ID of the workflow step.

        Returns:
            A callable function.
        """
        return asyncio_run(self._get(self._key_step_function_body(step_id)))

    def gen_step_id(self, step_name: str) -> int:
        async def _gen_step_id():
            key = self._key_num_steps_with_name(step_name)
            try:
                val = await self._get(key, True)
                await self._put(key, val + 1, True)
                return val + 1
            except KeyNotFoundError:
                await self._put(key, 0, True)
                return 0

        return asyncio_run(_gen_step_id())

    def load_step_args(
            self, step_id: StepID, workflows: List[Any],
            object_refs: List[ray.ObjectRef],
            workflow_refs: List[WorkflowRef]) -> Tuple[List, Dict[str, Any]]:
        """Load the input arguments of the workflow step. This must be
        done under a serialization context, otherwise the arguments would
        not be reconstructed successfully.

        Args:
            step_id: ID of the workflow step.
            workflows: The workflows in the original arguments,
                replaced by the actual workflow outputs.
            object_refs: The object refs in the original arguments.

        Returns:
            Args and kwargs.
        """
        with serialization_context.workflow_args_resolving_context(
                workflows, object_refs, workflow_refs):
            flattened_args = asyncio_run(
                self._get(self._key_step_args(step_id)))
            return signature.recover_args(flattened_args)

    def save_object_ref(self, obj_ref: ray.ObjectRef) -> None:
        """Save the object ref.

        Args:
            obj_ref: The object reference

        Returns:
            None
        """

        async def _save_object_ref():
            data = await obj_ref
            await self._put(self._key_obj_id(obj_ref.hex()), data)

        return asyncio_run(_save_object_ref())

    def load_object_ref(self, object_id: str) -> ray.ObjectRef:
        """Load the input object ref.

        Args:
            object_id: The hex ObjectID.

        Returns:
            The object ref.
        """

        async def _load_obj_ref() -> ray.ObjectRef:
            data = await self._get(self._key_obj_id(object_id))
            ref = ray.put(data)
            return ref

        return asyncio_run(_load_obj_ref())

    async def _update_dynamic_output(self, outer_most_step_id: StepID,
                                     dynamic_output_step_id: StepID) -> None:
        """Update dynamic output.

        There are two steps involved:
        1. outer_most_step[id=outer_most_step_id]
        2. dynamic_step[id=dynamic_output_step_id]

        Initially, the output of outer_most_step comes from its *direct*
        nested step. However, the nested step could also have a nested
        step inside it (unknown currently and could be generated
        dynamically in the future). We call such steps "dynamic_step".

        When the dynamic_step produces its output, the output of
        outer_most_step can be updated and point to the dynamic_step. This
        creates a "shortcut" and accelerate workflow resuming. This is
        critical for scalability of virtual actors.

        Args:
            outer_most_step_id: ID of outer_most_step. See
                "step_executor.execute_workflow" for explanation.
            dynamic_output_step_id: ID of dynamic_step.
        """
        metadata = await self._get(
            self._key_step_output_metadata(outer_most_step_id), True)
        if (dynamic_output_step_id != metadata["output_step_id"]
                and dynamic_output_step_id !=
                metadata.get("dynamic_output_step_id")):
            metadata["dynamic_output_step_id"] = dynamic_output_step_id
            await self._put(
                self._key_step_output_metadata(outer_most_step_id), metadata,
                True)

    async def _locate_output_step_id(self, step_id: StepID) -> str:
        metadata = await self._get(
            self._key_step_output_metadata(step_id), True)
        return (metadata.get("dynamic_output_step_id")
                or metadata["output_step_id"])

    def get_entrypoint_step_id(self) -> StepID:
        """Load the entrypoint step ID of the workflow.

        Returns:
            The ID of the entrypoint step.
        """
        # empty StepID represents the workflow driver
        try:
            return asyncio_run(self._locate_output_step_id(""))
        except Exception as e:
            raise ValueError("Fail to get entrypoint step ID from workflow"
                             f"[id={self._workflow_id}]") from e

    def inspect_step(self, step_id: StepID) -> StepInspectResult:
        """
        Get the status of a workflow step. The status indicates whether
        the workflow step can be recovered etc.

        Args:
            step_id: The ID of a workflow step

        Returns:
            The status of the step.
        """
        return asyncio_run(self._inspect_step(step_id))

    async def _inspect_step(self, step_id: StepID) -> StepInspectResult:
        items = await self._scan(self._key_step_prefix(step_id))
        keys = set(items)
        field_list = StepStatus(
            output_object_exists=(STEP_OUTPUT in keys),
            output_metadata_exists=(STEP_OUTPUTS_METADATA in keys),
            input_metadata_exists=(STEP_INPUTS_METADATA in keys),
            args_exists=(STEP_ARGS in keys),
            func_body_exists=(STEP_FUNC_BODY in keys))
        # does this step contains output checkpoint file?
        if field_list.output_object_exists:
            return StepInspectResult(output_object_valid=True)
        # do we know where the output comes from?
        if field_list.output_metadata_exists:
            output_step_id = await self._locate_output_step_id(step_id)
            return StepInspectResult(output_step_id=output_step_id)

        # read inputs metadata
        try:
            metadata = await self._get(
                self._key_step_input_metadata(step_id), True)
            return StepInspectResult(
                args_valid=field_list.args_exists,
                func_body_valid=field_list.func_body_exists,
                object_refs=metadata["object_refs"],
                workflows=metadata["workflows"],
                workflow_refs=metadata["workflow_refs"],
                max_retries=metadata.get("max_retries"),
                catch_exceptions=metadata.get("catch_exceptions"),
                ray_options=metadata.get("ray_options", {}),
                step_type=StepType[metadata.get("step_type")],
            )
        except Exception:
            return StepInspectResult(
                args_valid=field_list.args_exists,
                func_body_valid=field_list.func_body_exists)

    async def _write_step_inputs(self, step_id: StepID,
                                 inputs: WorkflowData) -> None:
        """Save workflow inputs."""
        metadata = inputs.to_metadata()
        with serialization_context.workflow_args_keeping_context():
            # TODO(suquark): in the future we should write to storage directly
            # with plasma store object in memory.
            args_obj = ray.get(inputs.inputs.args)
        save_tasks = [
            self._put(self._key_step_input_metadata(step_id), metadata, True),
            self._put(self._key_step_function_body(step_id), inputs.func_body),
            self._put(self._key_step_args(step_id), args_obj)
        ]
        await asyncio.gather(*save_tasks)

    def save_subworkflow(self, workflow: Workflow) -> None:
        """Save the DAG and inputs of the sub-workflow.

        Args:
            workflow: A sub-workflow. Could be a nested workflow inside
                a workflow step.
        """
        assert not workflow.executed
        tasks = [
            self._write_step_inputs(w.step_id, w.data)
            for w in workflow.iter_workflows_in_dag()
        ]
        asyncio_run(asyncio.gather(*tasks))

    def load_actor_class_body(self) -> type:
        """Load the class body of the virtual actor.

        Raises:
            DataLoadError: if we fail to load the class body.
        """
        return asyncio_run(self._get(self._key_class_body()))

    def save_actor_class_body(self, cls: type) -> None:
        """Save the class body of the virtual actor.

        Args:
            cls: The class body used by the virtual actor.

        Raises:
            DataSaveError: if we fail to save the class body.
        """
        asyncio_run(self._put(self._key_class_body(), cls))

    def save_workflow_meta(self, metadata: WorkflowMetaData) -> None:
        """Save the metadata of the current workflow.

        Args:
            metadata: WorkflowMetaData of the current workflow.

        Raises:
            DataSaveError: if we fail to save the class body.
        """

        metadata = {
            "status": metadata.status.value,
        }
        asyncio_run(self._put(self._key_workflow_metadata(), metadata, True))

    def load_workflow_meta(self) -> Optional[WorkflowMetaData]:
        """Load the metadata of the current workflow.

        Returns:
            The metadata of the current workflow. If it doesn't exist,
            return None.
        """

        try:
            metadata = asyncio_run(
                self._get(self._key_workflow_metadata(), True))
            return WorkflowMetaData(status=WorkflowStatus(metadata["status"]))
        except KeyNotFoundError:
            return None

    async def _list_workflow(self) -> List[Tuple[str, WorkflowStatus]]:
        prefix = self._storage.make_key("")
        workflow_ids = await self._storage.scan_prefix(prefix)
        metadata = await asyncio.gather(*[
            self._get([workflow_id, WORKFLOW_META], True)
            for workflow_id in workflow_ids
        ])
        return [(wid, WorkflowStatus(meta["status"]) if meta else None)
                for (wid, meta) in zip(workflow_ids, metadata)]

    def list_workflow(self) -> List[Tuple[str, WorkflowStatus]]:
        return asyncio_run(self._list_workflow())

    def advance_progress(self, finished_step_id: "StepID") -> None:
        """Save the latest progress of a workflow. This is used by a
        virtual actor.

        Args:
            finished_step_id: The step that contains the latest output.

        Raises:
            DataSaveError: if we fail to save the progress.
        """
        asyncio_run(
            self._put(self._key_workflow_progress(), {
                "step_id": finished_step_id,
            }, True))

    def get_latest_progress(self) -> "StepID":
        """Load the latest progress of a workflow. This is used by a
        virtual actor.

        Raises:
            DataLoadError: if we fail to load the progress.

        Returns:
            The step that contains the latest output.
        """
        return asyncio_run(self._get(self._key_workflow_progress(),
                                     True))["step_id"]

    async def _put(self, paths: List[str], data: Any,
                   is_json: bool = False) -> None:
        try:
            key = self._storage.make_key(*paths)
            await self._storage.put(key, data, is_json=is_json)
        except Exception as e:
            raise DataSaveError from e

    async def _get(self, paths: List[str], is_json: bool = False) -> Any:
        try:
            key = self._storage.make_key(*paths)
            return await self._storage.get(key, is_json=is_json)
        except KeyNotFoundError:
            raise
        except Exception as e:
            raise DataLoadError from e

    async def _scan(self, paths: List[str]) -> Any:
        try:
            prefix = self._storage.make_key(*paths)
            return await self._storage.scan_prefix(prefix)
        except Exception as e:
            raise DataLoadError from e

    # The following functions are helper functions to get the key
    # for a specific fields

    def _key_workflow_progress(self):
        return [self._workflow_id, STEPS_DIR, WORKFLOW_PROGRESS]

    def _key_step_input_metadata(self, step_id):
        return [self._workflow_id, STEPS_DIR, step_id, STEP_INPUTS_METADATA]

    def _key_step_output(self, step_id):
        return [self._workflow_id, STEPS_DIR, step_id, STEP_OUTPUT]

    def _key_step_output_metadata(self, step_id):
        return [self._workflow_id, STEPS_DIR, step_id, STEP_OUTPUTS_METADATA]

    def _key_step_function_body(self, step_id):
        return [self._workflow_id, STEPS_DIR, step_id, STEP_FUNC_BODY]

    def _key_step_args(self, step_id):
        return [self._workflow_id, STEPS_DIR, step_id, STEP_ARGS]

    def _key_obj_id(self, object_id):
        return [self._workflow_id, OBJECTS_DIR, object_id]

    def _key_step_prefix(self, step_id):
        return [self._workflow_id, STEPS_DIR, step_id, ""]

    def _key_class_body(self):
        return [self._workflow_id, CLASS_BODY]

    def _key_workflow_metadata(self):
        return [self._workflow_id, WORKFLOW_META]

    def _key_num_steps_with_name(self, name):
        return [self._workflow_id, DUPLICATE_NAME_COUNTER, name]


def get_workflow_storage(workflow_id: Optional[str] = None) -> WorkflowStorage:
    """Get the storage for the workflow.

    Args:
        workflow_id: The ID of the storage.

    Returns:
        A workflow storage.
    """
    store = storage.get_global_storage()
    if workflow_id is None:
        workflow_id = workflow_context.get_workflow_step_context().workflow_id
    return WorkflowStorage(workflow_id, store)
