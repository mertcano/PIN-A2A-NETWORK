import json
import logging

from collections.abc import AsyncIterable
from typing import Any

from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from common.server.task_manager import TaskManager
from common.types import (
    A2ARequest,
    AgentCard,
    CancelTaskRequest,
    GetTaskPushNotificationRequest,
    GetTaskRequest,
    InternalError,
    InvalidRequestError,
    JSONParseError,
    JSONRPCResponse,
    SendTaskRequest,
    SendTaskStreamingRequest,
    SetTaskPushNotificationRequest,
    TaskResubscriptionRequest,
)


logger = logging.getLogger(__name__)


class A2AServer:
    def __init__(
        self,
        host='0.0.0.0',
        port=5000,
        endpoint='/',
        agent_card: AgentCard = None,
        task_manager: TaskManager = None,
    ):
        self.host = host
        self.port = port
        self.endpoint = endpoint
        self.task_manager = task_manager
        self.agent_card = agent_card
        self.app = Starlette()
        self.app.add_route(
            self.endpoint, self._process_request, methods=['POST']
        )
        self.app.add_route(
            '/.well-known/agent.json', self._get_agent_card, methods=['GET']
        )

    def start(self):
        if self.agent_card is None:
            raise ValueError('agent_card is not defined')

        if self.task_manager is None:
            raise ValueError('request_handler is not defined')

        import uvicorn

        # Configure uvicorn with reduced logging
        uvicorn.run(
            self.app, 
            host=self.host, 
            port=self.port,
            log_level="error",  # Reduce uvicorn logs
            access_log=False    # Disable access logs
        )

    def _get_agent_card(self, request: Request) -> JSONResponse:
        return JSONResponse(self.agent_card.model_dump(exclude_none=True))

    async def _process_request(self, request: Request):
        try:
            body = await request.json()
            json_rpc_request = A2ARequest.validate_python(body)

            if isinstance(json_rpc_request, GetTaskRequest):
                result = await self.task_manager.on_get_task(json_rpc_request)
            elif isinstance(json_rpc_request, SendTaskRequest):
                result = await self.task_manager.on_send_task(json_rpc_request)
            elif isinstance(json_rpc_request, SendTaskStreamingRequest):
                result = await self.task_manager.on_send_task_subscribe(
                    json_rpc_request
                )
            elif isinstance(json_rpc_request, CancelTaskRequest):
                result = await self.task_manager.on_cancel_task(
                    json_rpc_request
                )
            elif isinstance(json_rpc_request, SetTaskPushNotificationRequest):
                result = await self.task_manager.on_set_task_push_notification(
                    json_rpc_request
                )
            elif isinstance(json_rpc_request, GetTaskPushNotificationRequest):
                result = await self.task_manager.on_get_task_push_notification(
                    json_rpc_request
                )
            elif isinstance(json_rpc_request, TaskResubscriptionRequest):
                result = await self.task_manager.on_resubscribe_to_task(
                    json_rpc_request
                )
            else:
                logger.warning(
                    f'Unexpected request type: {type(json_rpc_request)}'
                )
                raise ValueError(f'Unexpected request type: {type(request)}')

            return self._create_response(result)

        except Exception as e:
            return self._handle_exception(e)

    def _handle_exception(self, e: Exception) -> JSONResponse:
        if isinstance(e, json.decoder.JSONDecodeError):
            json_rpc_error = JSONParseError()
        elif isinstance(e, ValidationError):
            json_rpc_error = InvalidRequestError(data=json.loads(e.json()))
        else:
            logger.error(f'Unhandled exception: {e}')
            json_rpc_error = InternalError()

        response = JSONRPCResponse(id=None, error=json_rpc_error)
        return JSONResponse(
            response.model_dump(exclude_none=True), status_code=400
        )

    def _create_response(
        self, result: Any
    ) -> JSONResponse | StreamingResponse:
        if isinstance(result, AsyncIterable):

            async def sse_stream_generator():
                """Generate Server-Sent Events format manually"""
                try:
                    async for item in result:
                        data = item.model_dump_json(exclude_none=True)
                        # Manually format SSE data
                        yield f"data: {data}\n\n"
                except Exception as e:
                    logger.error(f"Error in SSE stream: {e}")
                    # Send error as SSE event
                    error_data = {"error": str(e)}
                    yield f"data: {json.dumps(error_data)}\n\n"

            return StreamingResponse(
                sse_stream_generator(),
                media_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'Access-Control-Allow-Origin': '*',
                    'Access-Control-Allow-Headers': 'Cache-Control'
                }
            )
        if isinstance(result, JSONRPCResponse):
            return JSONResponse(result.model_dump(exclude_none=True))
        logger.error(f'Unexpected result type: {type(result)}')
        raise ValueError(f'Unexpected result type: {type(result)}')
