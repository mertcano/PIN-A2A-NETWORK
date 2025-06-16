import json
import logging

from abc import ABC, abstractmethod
from collections.abc import AsyncIterable
from typing import Any

from common.server import utils
from common.server.task_manager import InMemoryTaskManager
from common.types import (
    Artifact,
    InternalError,
    JSONRPCResponse,
    Message,
    SendTaskRequest,
    SendTaskResponse,
    SendTaskStreamingRequest,
    SendTaskStreamingResponse,
    Task,
    TaskArtifactUpdateEvent,
    TaskSendParams,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)
from google.genai import types
# Import Ethereum related libraries
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct


logger = logging.getLogger(__name__)


# TODO: Move this class (or these classes) to a common directory
class AgentWithTaskManager(ABC):
    @abstractmethod
    def get_processing_message(self) -> str:
        pass

    def invoke(self, query, session_id) -> str:
        session = self._runner.session_service.get_session(
            app_name=self._agent.name,
            user_id=self._user_id,
            session_id=session_id,
        )
        content = types.Content(
            role='user', parts=[types.Part.from_text(text=query)]
        )
        if session is None:
            session = self._runner.session_service.create_session(
                app_name=self._agent.name,
                user_id=self._user_id,
                state={},
                session_id=session_id,
            )
        events = list(
            self._runner.run(
                user_id=self._user_id,
                session_id=session.id,
                new_message=content,
            )
        )
        if not events or not events[-1].content or not events[-1].content.parts:
            return ''
        return '\n'.join([p.text for p in events[-1].content.parts if p.text])

    async def stream(self, query, session_id) -> AsyncIterable[dict[str, Any]]:
        session = self._runner.session_service.get_session(
            app_name=self._agent.name,
            user_id=self._user_id,
            session_id=session_id,
        )
        content = types.Content(
            role='user', parts=[types.Part.from_text(text=query)]
        )
        if session is None:
            session = self._runner.session_service.create_session(
                app_name=self._agent.name,
                user_id=self._user_id,
                state={},
                session_id=session_id,
            )
        async for event in self._runner.run_async(
            user_id=self._user_id, session_id=session.id, new_message=content
        ):
            if event.is_final_response():
                response = ''
                if (
                    event.content
                    and event.content.parts
                    and event.content.parts[0].text
                ):
                    response = '\n'.join(
                        [p.text for p in event.content.parts if p.text]
                    )
                elif (
                    event.content
                    and event.content.parts
                    and any(
                        [
                            True
                            for p in event.content.parts
                            if p.function_response
                        ]
                    )
                ):
                    response = next(
                        p.function_response.model_dump()
                        for p in event.content.parts
                    )
                yield {
                    'is_task_complete': True,
                    'content': response,
                }
            else:
                yield {
                    'is_task_complete': False,
                    'updates': self.get_processing_message(),
                }


class AgentTaskManager(InMemoryTaskManager):
    def __init__(self, agent: AgentWithTaskManager, verify_signatures: bool = True):
        super().__init__()
        self.agent = agent
        self.verify_signatures = verify_signatures

    async def _validate_signature(self, task_send_params: TaskSendParams) -> tuple[bool, str]:
        """Validate the signature from the Host Agent.
        
        Args:
            task_send_params: The task parameters containing message metadata with signature.
            
        Returns:
            A tuple of (is_valid, error_message).
        """
        # Skip validation if disabled
        if not self.verify_signatures:
            return True, ""
            
        try:
            # Get session ID for signature verification
            session_id = task_send_params.sessionId
            
            # Check if auth data exists in metadata
            if (not task_send_params.message or 
                not task_send_params.message.metadata or
                'auth' not in task_send_params.message.metadata):
                return False, "No authentication data found in message metadata"
                
            # Extract auth data
            auth_data = task_send_params.message.metadata.get('auth', {})
            address = auth_data.get('address')
            signature = auth_data.get('signature')
            
            # Validate required fields
            if not address:
                return False, "Missing Ethereum address in auth data"
                
            if not signature:
                return False, "Missing signature in auth data"
                
            # Reconstruct the original message that was signed
            message_to_verify = f"{address}{session_id}"
            message_hash = encode_defunct(text=message_to_verify)
            
            # Recover the address that signed the message
            try:
                recovered_address = Account.recover_message(message_hash, signature=signature)
                
                # Check if the recovered address matches the claimed address
                if recovered_address.lower() != address.lower():
                    return False, f"Signature verification failed. Expected {address}, got {recovered_address}"
                    
                logger.info(f"Signature verified successfully for address {address}")
                return True, ""
            except Exception as e:
                return False, f"Error recovering address from signature: {e}"
                
        except Exception as e:
            logger.error(f"Error validating signature: {e}")
            return False, f"Error validating signature: {e}"

    async def _stream_generator(
        self, request: SendTaskStreamingRequest
    ) -> AsyncIterable[SendTaskStreamingResponse] | JSONRPCResponse:
        task_send_params: TaskSendParams = request.params
        query = self._get_user_query(task_send_params)
        try:
            async for item in self.agent.stream(
                query, task_send_params.sessionId
            ):
                is_task_complete = item['is_task_complete']
                artifacts = None
                if not is_task_complete:
                    task_state = TaskState.WORKING
                    parts = [{'type': 'text', 'text': item['updates']}]
                else:
                    if isinstance(item['content'], dict):
                        if (
                            'response' in item['content']
                            and 'result' in item['content']['response']
                        ):
                            data = json.loads(
                                item['content']['response']['result']
                            )
                            task_state = TaskState.INPUT_REQUIRED
                        else:
                            data = item['content']
                            task_state = TaskState.COMPLETED
                        parts = [{'type': 'data', 'data': data}]
                    else:
                        task_state = TaskState.COMPLETED
                        parts = [{'type': 'text', 'text': item['content']}]
                    artifacts = [Artifact(parts=parts, index=0, append=False)]
            message = Message(role='agent', parts=parts)
            task_status = TaskStatus(state=task_state, message=message)
            await self._update_store(
                task_send_params.id, task_status, artifacts
            )
            task_update_event = TaskStatusUpdateEvent(
                id=task_send_params.id,
                status=task_status,
                final=False,
            )
            yield SendTaskStreamingResponse(
                id=request.id, result=task_update_event
            )
            # Now yield Artifacts too
            if artifacts:
                for artifact in artifacts:
                    yield SendTaskStreamingResponse(
                        id=request.id,
                        result=TaskArtifactUpdateEvent(
                            id=task_send_params.id,
                            artifact=artifact,
                        ),
                    )
            if is_task_complete:
                yield SendTaskStreamingResponse(
                    id=request.id,
                    result=TaskStatusUpdateEvent(
                        id=task_send_params.id,
                        status=TaskStatus(
                            state=task_status.state,
                        ),
                        final=True,
                    ),
                )
        except Exception as e:
            logger.error(f'An error occurred while streaming the response: {e}')
            yield JSONRPCResponse(
                id=request.id,
                error=InternalError(
                    message='An error occurred while streaming the response'
                ),
            )

    def _validate_request(
        self, request: SendTaskRequest | SendTaskStreamingRequest
    ) -> None:
        task_send_params: TaskSendParams = request.params
        if not utils.are_modalities_compatible(
            task_send_params.acceptedOutputModes,
            self.agent.SUPPORTED_CONTENT_TYPES,
        ):
            logger.warning(
                'Unsupported output mode. Received %s, Support %s',
                task_send_params.acceptedOutputModes,
                self.agent.SUPPORTED_CONTENT_TYPES,
            )
            return utils.new_incompatible_types_error(request.id)

    async def on_send_task(self, request: SendTaskRequest) -> SendTaskResponse:
        error = self._validate_request(request)
        if error:
            return error
            
        # Validate signature
        is_valid, error_message = await self._validate_signature(request.params)
        if not is_valid:
            logger.warning(f"Signature validation failed: {error_message}")
            return JSONRPCResponse(
                id=request.id,
                error=InternalError(message=f"Signature verification failed: {error_message}")
            )
            
        await self.upsert_task(request.params)
        return await self._invoke(request)

    async def on_send_task_subscribe(
        self, request: SendTaskStreamingRequest
    ) -> AsyncIterable[SendTaskStreamingResponse] | JSONRPCResponse:
        error = self._validate_request(request)
        if error:
            return error
            
        # Validate signature
        is_valid, error_message = await self._validate_signature(request.params)
        if not is_valid:
            logger.warning(f"Signature validation failed: {error_message}")
            return JSONRPCResponse(
                id=request.id,
                error=InternalError(message=f"Signature verification failed: {error_message}")
            )
            
        await self.upsert_task(request.params)
        return self._stream_generator(request)

    async def _update_store(
        self, task_id: str, status: TaskStatus, artifacts: list[Artifact]
    ) -> Task:
        async with self.lock:
            try:
                task = self.tasks[task_id]
            except KeyError:
                logger.error(f'Task {task_id} not found for updating the task')
                raise ValueError(f'Task {task_id} not found')
            task.status = status
            # if status.message is not None:
            #    self.task_messages[task_id].append(status.message)
            if artifacts is not None:
                if task.artifacts is None:
                    task.artifacts = []
                task.artifacts.extend(artifacts)
            return task

    async def _invoke(self, request: SendTaskRequest) -> SendTaskResponse:
        task_send_params: TaskSendParams = request.params
        query = self._get_user_query(task_send_params)
        try:
            result = self.agent.invoke(query, task_send_params.sessionId)
        except Exception as e:
            logger.error(f'Error invoking agent: {e}')
            raise ValueError(f'Error invoking agent: {e}')
        parts = [{'type': 'text', 'text': result}]
        task_state = (
            TaskState.INPUT_REQUIRED
            if 'MISSING_INFO:' in result
            else TaskState.COMPLETED
        )
        task = await self._update_store(
            task_send_params.id,
            TaskStatus(
                state=task_state, message=Message(role='agent', parts=parts)
            ),
            [Artifact(parts=parts)],
        )
        return SendTaskResponse(id=request.id, result=task)

    def _get_user_query(self, task_send_params: TaskSendParams) -> str:
        part = task_send_params.message.parts[0]
        if not isinstance(part, TextPart):
            raise ValueError('Only text parts are supported')
        return part.text
