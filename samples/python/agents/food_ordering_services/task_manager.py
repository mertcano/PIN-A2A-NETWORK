import json
import logging
import os

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
        # Store session_id for use in tool functions
        if hasattr(self, '_current_session_id'):
            self._current_session_id = session_id
        
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
        # Store session_id for use in tool functions
        if hasattr(self, '_current_session_id'):
            self._current_session_id = session_id
            
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
    def __init__(self, agent: AgentWithTaskManager, verify_signatures: bool = True, verify_blockchain: bool = True):
        super().__init__()
        self.agent = agent
        self.verify_signatures = verify_signatures
        self.verify_blockchain = verify_blockchain
        
        # 获取从AgentCard中设置的以太坊地址
        self.agent_address = None

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
                    
                # logger.info(f"Signature verified successfully for address {address}")
                print(f"[PIN AI NETWORK] Service Agent: signature verified successfully for Host Agent address {address}")
                return True, ""
            except Exception as e:
                return False, f"Error recovering address from signature: {e}"
                
        except Exception as e:
            logger.error(f"Error validating signature: {e}")
            return False, f"Error validating signature: {e}"
    
    async def _validate_blockchain_confirmation(self, task_send_params: TaskSendParams) -> tuple[bool, str]:
        """Validate blockchain task confirmation.
        
        Args:
            task_send_params: Task parameters containing blockchain transaction hash.
            
        Returns:
            A tuple of (is_valid, error_message).
        """
        try:
            # Skip validation if disabled
            if not self.verify_blockchain:
                return True, ""
                
            # Check if blockchain data exists in metadata
            if (not task_send_params.message or 
                not task_send_params.message.metadata or
                'blockchain' not in task_send_params.message.metadata):
                return True, "No blockchain confirmation data, skipping validation"
                
            # Extract transaction hash - adapt to new metadata format
            blockchain_data = task_send_params.message.metadata.get('blockchain', {})
            confirm_task_data = blockchain_data.get('confirmTask', {})
            tx_hash = confirm_task_data.get('tx_hash')
            
            if not tx_hash:
                return False, "Missing blockchain transaction hash"
                
            # Get session ID and convert to on-chain UUID
            session_id = task_send_params.sessionId
            task_uuid = int(session_id.replace('-', ''), 16) % (2**256)
                
            # Connect to blockchain
            w3 = Web3(Web3.HTTPProvider(os.environ.get('CHAIN_RPC', "http://127.0.0.1:8545/")))
            if not w3.is_connected():
                return False, "Unable to connect to blockchain network"
                
            # Hardcoded contract address
            contract_address = os.environ.get('PIN_AI_NETWORK_TASK_CONTRACT', "0x5FbDB2315678afecb367f032d93F642f64180aa3")
            
            # Simplified ABI containing only tasks mapping getter function
            abi = [
                {
                    "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
                    "name": "tasks",
                    "outputs": [
                        {"internalType": "address", "name": "taskAgent", "type": "address"},
                        {"internalType": "address", "name": "serviceAgent", "type": "address"},
                        {"internalType": "bool", "name": "isCompleted", "type": "bool"},
                        {"internalType": "uint256", "name": "createdAt", "type": "uint256"},
                        {"internalType": "uint256", "name": "lastestCompletedAt", "type": "uint256"},
                        {"internalType": "uint256", "name": "payAmount", "type": "uint256"}
                    ],
                    "stateMutability": "view",
                    "type": "function"
                }
            ]
            contract = w3.eth.contract(address=contract_address, abi=abi)
            
            # Check if agent ethereum address is set
            if not self.agent_address:
                # Try to get from environment variable
                self.agent_address = os.environ.get('AGENT_ETH_ADDRESS')
                
                if not self.agent_address:
                    logger.warning("Agent ethereum address not set, skipping blockchain task validation")
                    return True, "Blockchain validation skipped due to missing agent address"
            
            try:
                # Get task data from blockchain
                task_data = contract.functions.tasks(task_uuid).call()
                
                # Validate task exists and remote agent address matches
                service_agent_address = task_data[1]  # serviceAgent field is at index 1
                
                if not service_agent_address or service_agent_address == "0x0000000000000000000000000000000000000000":
                    logger.warning(f"On-chain task UUID {task_uuid} does not exist or is not properly set")
                    return True, "Blockchain task not found, but allowing task to proceed"
                    
                if service_agent_address.lower() != self.agent_address.lower():
                    return False, f"Task service agent address mismatch. Expected: {self.agent_address}, Got: {service_agent_address}"
                
                # Validate transaction status
                tx_receipt = w3.eth.get_transaction_receipt(tx_hash)
                if not tx_receipt:
                    return False, "Unable to get transaction receipt"
                    
                if tx_receipt.status != 1:
                    return False, "Transaction execution failed"
                    
            except Exception as e:
                logger.warning(f"Blockchain task validation failed: {e}, but allowing task to proceed")
                return True, f"Blockchain validation failed but proceeding: {e}"
            
            # logger.info(f"Blockchain task confirmation validated successfully, UUID: {task_uuid}")
            print(f"[PIN AI NETWORK] Service Agent: On-chain task check confirmed! UUID: {task_uuid}")
            return True, ""
        except Exception as e:
            logger.error(f"Error validating blockchain confirmation: {e}")
            return False, f"Blockchain confirmation validation failed: {e}"

    async def _stream_generator(
        self, request: SendTaskStreamingRequest
    ) -> AsyncIterable[SendTaskStreamingResponse]:
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
                    break
        except Exception as e:
            logger.error(f'An error occurred while streaming the response: {e}')
            yield SendTaskStreamingResponse(
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
        
        # Validate blockchain confirmation
        is_valid, error_message = await self._validate_blockchain_confirmation(request.params)
        if not is_valid:
            logger.warning(f"Blockchain confirmation validation failed: {error_message}")
            return JSONRPCResponse(
                id=request.id,
                error=InternalError(message=f"Blockchain confirmation validation failed: {error_message}")
            )
            
        await self.upsert_task(request.params)
        return await self._invoke(request)

    async def on_send_task_subscribe(
        self, request: SendTaskStreamingRequest
    ) -> AsyncIterable[SendTaskStreamingResponse]:
        error = self._validate_request(request)
        if error:
            yield SendTaskStreamingResponse(id=request.id, error=error.error)
            return
            
        # Validate signature
        is_valid, error_message = await self._validate_signature(request.params)
        if not is_valid:
            logger.warning(f"Signature validation failed: {error_message}")
            yield SendTaskStreamingResponse(
                id=request.id,
                error=InternalError(message=f"Signature verification failed: {error_message}")
            )
            return
        
        # Validate blockchain confirmation
        is_valid, error_message = await self._validate_blockchain_confirmation(request.params)
        if not is_valid:
            logger.warning(f"Blockchain confirmation validation failed: {error_message}")
            yield SendTaskStreamingResponse(
                id=request.id,
                error=InternalError(message=f"Blockchain confirmation validation failed: {error_message}")
            )
            return
            
        await self.upsert_task(request.params)
        try:
            async for response in self._stream_generator(request):
                yield response
        except Exception as e:
            logger.error(f"Error in stream generator: {e}")
            yield SendTaskStreamingResponse(
                id=request.id,
                error=InternalError(message=f"Stream generation error: {e}")
            )

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
