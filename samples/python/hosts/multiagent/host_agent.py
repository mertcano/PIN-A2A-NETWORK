import base64
import json
import os
import uuid
from datetime import datetime

from common.client import A2ACardResolver
from common.types import (
    AgentCard,
    DataPart,
    Message,
    Part,
    Task,
    TaskSendParams,
    TaskState,
    TextPart,
)
from google.adk import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.tools.tool_context import ToolContext
from google.genai import types
# Import Ethereum related libraries
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_defunct

from .remote_agent_connection import RemoteAgentConnections, TaskUpdateCallback


class HostAgent:
    """The host agent.

    This is the agent responsible for choosing which remote agents to send
    tasks to and coordinate their work.
    """

    def __init__(
        self,
        remote_agent_addresses: list[str],
        task_callback: TaskUpdateCallback | None = None,
        private_key: str = None,  # Add private key parameter
    ):
        self.task_callback = task_callback
        self.remote_agent_connections: dict[str, RemoteAgentConnections] = {}
        self.cards: dict[str, AgentCard] = {}
        
        # Get private key from environment variable if not provided
        self.private_key = private_key or os.environ.get('ETH_PRIVATE_KEY')
        
        # Generate Ethereum address from private key if available
        self.eth_address = None
        if self.private_key:
            try:
                self.eth_address = Account.from_key(self.private_key).address
            except Exception as e:
                print(f"Error generating Ethereum address: {e}")
                
        for address in remote_agent_addresses:
            card_resolver = A2ACardResolver(address)
            card = card_resolver.get_agent_card()
            remote_connection = RemoteAgentConnections(card)
            self.remote_agent_connections[card.name] = remote_connection
            self.cards[card.name] = card
        agent_info = []
        for ra in self.list_remote_agents():
            agent_info.append(json.dumps(ra))
        self.agents = '\n'.join(agent_info)

    def register_agent_card(self, card: AgentCard):
        remote_connection = RemoteAgentConnections(card)
        self.remote_agent_connections[card.name] = remote_connection
        self.cards[card.name] = card
        agent_info = []
        for ra in self.list_remote_agents():
            agent_info.append(json.dumps(ra))
        self.agents = '\n'.join(agent_info)

    def create_agent(self) -> Agent:
        return Agent(
            model='gemini-2.0-flash-001',
            name='host_agent',
            instruction=self.root_instruction,
            before_model_callback=self.before_model_callback,
            description=(
                'This agent orchestrates the decomposition of the user request into'
                ' tasks that can be performed by the child agents.'
            ),
            tools=[
                self.list_remote_agents,
                self.send_task,
                self.confirm_task,
                self.get_user_context,
            ],
        )

    def root_instruction(self, context: ReadonlyContext) -> str:
        current_agent = self.check_state(context)
        return f"""You are an expert delegator that can delegate the user request to the
appropriate remote agents.

Discovery:
- You can use `list_remote_agents` to list the available remote agents you
can use to delegate the task.
- You can use `get_user_context` to understand the user's current situation, preferences,
and location, which will help you make better decisions.

Execution:
- For IMPORTANT TASKS that involve real-world actions, transactions, or commitments, 
  use `confirm_task` to provide blockchain-level verification and security. This includes:
  * Food ordering and delivery requests
  * Restaurant reservations
  * Payment processing
  * Booking confirmations
  * Any task that involves spending money or making commitments
  
- For INFORMATIONAL QUERIES and simple interactions, use `send_task`:
  * Searching for restaurants or information
  * Getting recommendations
  * Asking questions
  * General conversation

- PRIORITIZE `confirm_task` for actionable requests. When a user makes a clear request
  like "order food", "book a table", or "make a reservation", use `confirm_task` 
  to ensure the task is properly verified on the blockchain.

- Be sure to include the remote agent name when you respond to the user.

- When the request is related to food or dining, first check the user context with
`get_user_context` to understand their preferences and current situation.

You can use `check_pending_task_states` to check the states of the pending
tasks.

When you receive requests like "I want to order food", use the user context to be more proactive
and helpful. Don't ask for information that is already available in the user context.
For example, instead of asking which restaurant, suggest their favorite restaurant
from the context.

IMPORTANT: When delegating food orders to the Food Ordering Agent, always include the user's 
delivery address from the user context in your message. For example: "Please order Van Damme 
pizza from Za Pizza for delivery to 2240 Calle De Luna, Santa Clara" instead of just 
"I want to order Van Damme pizza from Za Pizza".

Please rely on tools to address the request, and don't make up the response. If you are not sure, please ask the user for more details.
Focus on the most recent parts of the conversation primarily.

If there is an active agent, send the request to that agent with the update task tool.

Agents:
{self.agents}

Current agent: {current_agent['active_agent']}
"""

    def check_state(self, context: ReadonlyContext):
        state = context.state
        if (
            'session_id' in state
            and 'session_active' in state
            and state['session_active']
            and 'agent' in state
        ):
            return {'active_agent': f'{state["agent"]}'}
        return {'active_agent': 'None'}

    def before_model_callback(
        self, callback_context: CallbackContext, llm_request
    ):
        state = callback_context.state
        if 'session_active' not in state or not state['session_active']:
            if 'session_id' not in state:
                state['session_id'] = str(uuid.uuid4())
            state['session_active'] = True

    def list_remote_agents(self):
        """List the available remote agents you can use to delegate the task."""
        if not self.remote_agent_connections:
            return []

        remote_agent_info = []
        for card in self.cards.values():
            remote_agent_info.append(
                {'name': card.name, 'description': card.description}
            )
        return remote_agent_info

    def get_user_context(self):
        """Get the current user context information to help understand user needs better."""
        # Hardcoded user information for demo purposes
        user_context = {
            "environment": {
                "weather": "currently bad weather, not suitable for going out",
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            },
            "preferences": {
                "food_preference": "pizza",
                "favorite_restaurant": "Za Pizza",
                "favorite_dish": "Van Damme pizza"
            },
            "location": {
                "current_location": "home",
                "address": "2240 Calle De Luna, Santa Clara"
            }
        }
        
        return user_context

    def sign_message(self, message: str) -> str:
        """Sign a message using the host agent's private key.
        
        Args:
            message: The message to sign.
            
        Returns:
            The hex string of the signature if successful, or None if failed.
        """
        if not self.private_key:
            return None
            
        try:
            message_hash = encode_defunct(text=message)
            signed_message = Account.sign_message(message_hash, private_key=self.private_key)
            print(f"[PIN AI NETWORK] Host Agent: signed message: {signed_message.signature.hex()} with public key: {self.eth_address}")
            return signed_message.signature.hex()
        except Exception as e:
            print(f"Error signing message: {e}")
            return None

    async def confirm_task(
        self, agent_name: str, message: str, tool_context: ToolContext
    ):
        """Interacts with blockchain to confirm tasks and sends them to remote agents.
        
        This method is similar to send_task but registers task confirmation on blockchain.
        
        Args:
          agent_name: The name of the remote agent
          message: The task message to send to the agent
          tool_context: The tool context this method runs in
        
        Returns:
          A dictionary of JSON data including blockchain confirmation
        """
        if agent_name not in self.remote_agent_connections:
            raise ValueError(f'Agent {agent_name} not found')
            
        # Set state and get necessary information
        state = tool_context.state
        state['agent'] = agent_name
        card = self.cards[agent_name]
        client = self.remote_agent_connections[agent_name]
        if not client:
            raise ValueError(f'Client not available for {agent_name}')
            
        # Get or create task ID and session ID
        if 'task_id' in state:
            taskId = state['task_id']
        else:
            taskId = str(uuid.uuid4())
        sessionId = state['session_id']
        
        # Get remote agent's ethereum address
        remote_agent_address = None
        if hasattr(card, 'metadata') and card.metadata:
            remote_agent_address = card.metadata.get('ethereum_address')
            
        # If not found in card metadata, try environment variables
        if not remote_agent_address:
            remote_agent_address = os.environ.get('REMOTE_AGENT_ETH_ADDRESS', "0x70997970C51812dc3A010C7d01b50e0d17dc79C8")
            
        if not remote_agent_address:
            raise ValueError(f"Could not determine ethereum address for remote agent {agent_name}")
            
        # Initialize Web3 connection
        try:
            w3 = Web3(Web3.HTTPProvider(os.environ.get('CHAIN_RPC', "http://127.0.0.1:8545/")))
            if not w3.is_connected():
                raise ConnectionError("Unable to connect to blockchain network")
        except Exception as e:
            print(f"Blockchain connection error: {e}")
            print(f"Falling back to regular send_task without blockchain confirmation")
            # Fallback to regular send_task when blockchain is not available
            return await self.send_task(agent_name, message, tool_context)
            
        # Hardcoded contract address
        contract_address = os.environ.get('PIN_AI_NETWORK_TASK_CONTRACT', "0x5FbDB2315678afecb367f032d93F642f64180aa3")
        
        # Contract ABI with confirmTask function
        abi = [{"inputs":[{"name":"uuid","type":"uint256"},{"name":"remoteAgent","type":"address"}],"name":"confirmTask","outputs":[],"stateMutability":"payable","type":"function"}]
        contract = w3.eth.contract(address=contract_address, abi=abi)
        
        # Convert sessionId to on-chain task UUID
        task_uuid = int(sessionId.replace('-', ''), 16) % (2**256)
        
        # Cannot proceed without private key
        if not self.private_key:
            raise ValueError("Host Agent has no private key configured, cannot perform blockchain confirmation")
            
        # default 0.001 ETH (convert to integer)
        bounty = int(os.environ.get('PIN_AI_NETWORK_TASK_BOUNTY', "1000000000000000"))
        try:
            # Create account instance
            account = Account.from_key(self.private_key)
            
            # Build transaction
            tx = contract.functions.confirmTask(
                task_uuid,
                remote_agent_address
            ).build_transaction({
                'from': account.address,
                'value': bounty,  # Send funds as integer
                'gas': 500000,
                'gasPrice': w3.eth.gas_price,
                'nonce': w3.eth.get_transaction_count(account.address)
            })
            
            # Sign transaction
            signed_tx = w3.eth.account.sign_transaction(tx, private_key=self.private_key)
            
            # Send transaction
            tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

            print(f"[PIN AI NETWORK] Host Agent: created task transaction sent!, check on explorer: https://sepolia.basescan.org/tx/0x{tx_hash.hex()}")
            
            # Wait for transaction confirmation
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
            tx_hash_hex = receipt.transactionHash.hex()
            
        except Exception as e:
            print(f"Blockchain transaction error: {e}")
            print(f"Falling back to regular send_task without blockchain confirmation")
            # Fallback to regular send_task when blockchain transaction fails
            return await self.send_task(agent_name, message, tool_context)
        
        # Prepare message metadata
        messageId = ''
        metadata = {}
        if 'input_message_metadata' in state:
            metadata.update(**state['input_message_metadata'])
            if 'message_id' in state['input_message_metadata']:
                messageId = state['input_message_metadata']['message_id']
        if not messageId:
            messageId = str(uuid.uuid4())
            
        # Add basic metadata
        metadata.update(conversation_id=sessionId, message_id=messageId)
        
        # Add signature information to metadata
        signature = None
        if self.eth_address:
            message_to_sign = f"{self.eth_address}{sessionId}"
            signature = self.sign_message(message_to_sign)
            if signature:
                metadata["auth"] = {
                    "address": self.eth_address,
                    "signature": signature
                }
        
        # Add blockchain confirmation information to metadata
        metadata["blockchain"] = {
            "confirmTask": {
                "tx_hash": tx_hash_hex
            }
        }
        
        # Create task request
        request: TaskSendParams = TaskSendParams(
            id=taskId,
            sessionId=sessionId,
            message=Message(
                role='user',
                parts=[TextPart(text=message)],
                metadata=metadata,
            ),
            acceptedOutputModes=['text', 'text/plain', 'image/png'],
            metadata={'conversation_id': sessionId},
        )
        
        # Send task
        task = await client.send_task(request, self.task_callback)
        
        # Update session state
        state['session_active'] = task.status.state not in [
            TaskState.COMPLETED,
            TaskState.CANCELED,
            TaskState.FAILED,
            TaskState.UNKNOWN,
        ]
        
        # Handle task status
        if task.status.state == TaskState.INPUT_REQUIRED:
            tool_context.actions.skip_summarization = True
            tool_context.actions.escalate = True
        elif task.status.state == TaskState.CANCELED:
            raise ValueError(f'Agent {agent_name} task {task.id} is cancelled')
        elif task.status.state == TaskState.FAILED:
            raise ValueError(f'Agent {agent_name} task {task.id} failed')
            
        # Process response
        response = []
        if task.status.message:
            response.extend(
                convert_parts(task.status.message.parts, tool_context)
            )
        if task.artifacts:
            for artifact in task.artifacts:
                response.extend(convert_parts(artifact.parts, tool_context))
                
        # Return result with blockchain confirmation information
        response.append({
            "blockchain_confirmation": {
                "confirmTask": {
                    "transaction_hash": tx_hash_hex,
                    "contract_address": contract_address,
                    "task_uuid": task_uuid
                }
            }
        })
        
        return response

    async def send_task(
        self, agent_name: str, message: str, tool_context: ToolContext
    ):
        """Sends a task either streaming (if supported) or non-streaming.

        This will send a message to the remote agent named agent_name.

        Args:
          agent_name: The name of the agent to send the task to.
          message: The message to send to the agent for the task.
          tool_context: The tool context this method runs in.

        Yields:
          A dictionary of JSON data.
        """
        if agent_name not in self.remote_agent_connections:
            raise ValueError(f'Agent {agent_name} not found')
        state = tool_context.state
        state['agent'] = agent_name
        card = self.cards[agent_name]
        client = self.remote_agent_connections[agent_name]
        if not client:
            raise ValueError(f'Client not available for {agent_name}')
        if 'task_id' in state:
            taskId = state['task_id']
        else:
            taskId = str(uuid.uuid4())
        sessionId = state['session_id']
        
        # Generate ECDSA signature for authentication
        signature = None
        if self.private_key and self.eth_address:
            # Sign message combining agent address and session ID
            message_to_sign = f"{self.eth_address}{sessionId}"
            signature = self.sign_message(message_to_sign)
        
        task: Task
        messageId = ''
        metadata = {}
        if 'input_message_metadata' in state:
            metadata.update(**state['input_message_metadata'])
            if 'message_id' in state['input_message_metadata']:
                messageId = state['input_message_metadata']['message_id']
        if not messageId:
            messageId = str(uuid.uuid4())
            
        # Add basic metadata
        metadata.update(conversation_id=sessionId, message_id=messageId)
        
        # Add signature information to metadata if available
        if signature and self.eth_address:
            metadata.update({
                "auth": {
                    "address": self.eth_address,
                    "signature": signature
                }
            })
        
        request: TaskSendParams = TaskSendParams(
            id=taskId,
            sessionId=sessionId,
            message=Message(
                role='user',
                parts=[TextPart(text=message)],
                metadata=metadata,
            ),
            acceptedOutputModes=['text', 'text/plain', 'image/png'],
            # pushNotification=None,
            metadata={'conversation_id': sessionId},
        )
        task = await client.send_task(request, self.task_callback)
        # Assume completion unless a state returns that isn't complete
        state['session_active'] = task.status.state not in [
            TaskState.COMPLETED,
            TaskState.CANCELED,
            TaskState.FAILED,
            TaskState.UNKNOWN,
        ]
        if task.status.state == TaskState.INPUT_REQUIRED:
            # Force user input back
            tool_context.actions.skip_summarization = True
            tool_context.actions.escalate = True
        elif task.status.state == TaskState.CANCELED:
            # Open question, should we return some info for cancellation instead
            raise ValueError(f'Agent {agent_name} task {task.id} is cancelled')
        elif task.status.state == TaskState.FAILED:
            # Raise error for failure
            raise ValueError(f'Agent {agent_name} task {task.id} failed')
        response = []
        if task.status.message:
            # Assume the information is in the task message.
            response.extend(
                convert_parts(task.status.message.parts, tool_context)
            )
        if task.artifacts:
            for artifact in task.artifacts:
                response.extend(convert_parts(artifact.parts, tool_context))
        return response


def convert_parts(parts: list[Part], tool_context: ToolContext):
    rval = []
    for p in parts:
        rval.append(convert_part(p, tool_context))
    return rval


def convert_part(part: Part, tool_context: ToolContext):
    if part.type == 'text':
        return part.text
    if part.type == 'data':
        return part.data
    if part.type == 'file':
        # Repackage A2A FilePart to google.genai Blob
        # Currently not considering plain text as files
        file_id = part.file.name
        file_bytes = base64.b64decode(part.file.bytes)
        file_part = types.Part(
            inline_data=types.Blob(
                mime_type=part.file.mimeType, data=file_bytes
            )
        )
        tool_context.save_artifact(file_id, file_part)
        tool_context.actions.skip_summarization = True
        tool_context.actions.escalate = True
        return DataPart(data={'artifact-file-id': file_id})
    return f'Unknown type: {part.type}'
