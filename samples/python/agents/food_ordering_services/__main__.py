import logging
import os

import click
from eth_account import Account

from agent import FoodOrderingAgent
from common.server import A2AServer
from common.types import (
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    MissingAPIKeyError,
)
from dotenv import load_dotenv
from task_manager import AgentTaskManager


load_dotenv()

# Configure logging to reduce verbosity
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

# Reduce Google ADK and related library logs
logging.getLogger('google.adk.models.google_llm').setLevel(logging.ERROR)
logging.getLogger('google_genai.models').setLevel(logging.ERROR)
logging.getLogger('google_genai.types').setLevel(logging.ERROR)
logging.getLogger('httpx').setLevel(logging.ERROR)
logging.getLogger('uvicorn.access').setLevel(logging.ERROR)

# Keep important logs
logging.getLogger('__main__').setLevel(logging.INFO)
logging.getLogger('task_manager').setLevel(logging.INFO)
logging.getLogger('common.server.task_manager').setLevel(logging.INFO)


@click.command()
@click.option('--host', default='localhost')
@click.option('--port', default=10002)
@click.option('--verify-signatures', is_flag=True, default=True, help='Enable signature verification')
@click.option('--ethereum-address', default=None, help='Ethereum address for Remote Agent (optional)')
def main(host, port, verify_signatures, ethereum_address):
    try:
        # Check for API key only if Vertex AI is not configured
        if not os.getenv('GOOGLE_GENAI_USE_VERTEXAI') == 'TRUE':
            if not os.getenv('GOOGLE_API_KEY'):
                raise MissingAPIKeyError(
                    'GOOGLE_API_KEY environment variable not set and GOOGLE_GENAI_USE_VERTEXAI is not TRUE.'
                )

        # If ethereum_address is not provided, try to get it from ETH_PRIVATE_KEY environment variable
        if not ethereum_address:
            eth_private_key = os.environ.get('ETH_PRIVATE_KEY')
            if eth_private_key:
                try:
                    ethereum_address = Account.from_key(eth_private_key).address
                    logger.info(f"Generated ethereum_address from ETH_PRIVATE_KEY: {ethereum_address}")
                except Exception as e:
                    logger.error(f"Error generating Ethereum address from private key: {e}")
                    ethereum_address = '0x123456789abcdef0123456789abcdef012345678'  # Default address
            else:
                logger.warning("ETH_PRIVATE_KEY not set, using default ethereum_address")
                ethereum_address = '0x123456789abcdef0123456789abcdef012345678'  # Default address

        capabilities = AgentCapabilities(streaming=False)
        
        # Define agent skills
        restaurant_skill = AgentSkill(
            id='restaurant_search',
            name='Restaurant Search Tool',
            description='Helps users find restaurants in the Bay Area based on cuisine, location, and price range.',
            tags=['restaurant', 'search', 'bay area'],
            examples=[
                '我想找湾区的中餐馆',
                '旧金山有什么好的披萨店推荐吗？',
                '伯克利附近有什么价格适中的日本料理？'
            ],
        )
        
        delivery_skill = AgentSkill(
            id='food_delivery',
            name='Food Delivery Tool',
            description='Helps users order food delivery from restaurants in the Bay Area.',
            tags=['delivery', 'food', 'order'],
            examples=[
                '我想点一份披萨外卖',
                '从Zachary\'s Chicago Pizza订餐',
                '我想订购中餐外卖送到家里'
            ],
        )
        
        reservation_skill = AgentSkill(
            id='restaurant_reservation',
            name='Restaurant Reservation Tool',
            description='Helps users make restaurant reservations in the Bay Area.',
            tags=['reservation', 'dining'],
            examples=[
                '我想预订餐厅',
                '今晚在Mister Jiu\'s预订4人的位子',
                '明天晚上7点帮我在Rintaro预约两个人'
            ],
        )
        
        # Add ethereum_address to AgentCard metadata
        agent_card = AgentCard(
            name='Food Ordering Agent',
            description='This agent helps Bay Area users find restaurants, order food delivery, or make restaurant reservations.',
            url=f'http://localhost:{port}/',
            version='1.0.0',
            defaultInputModes=FoodOrderingAgent.SUPPORTED_CONTENT_TYPES,
            defaultOutputModes=FoodOrderingAgent.SUPPORTED_CONTENT_TYPES,
            capabilities=capabilities,
            skills=[restaurant_skill, delivery_skill, reservation_skill],
            metadata={"ethereum_address": ethereum_address}  # Add ethereum address
        )
        
        # Initialize the task manager with signature verification and save ethereum_address
        logger.info(f"Initializing AgentTaskManager with signature verification: {verify_signatures}")
        agent = FoodOrderingAgent()
        task_manager = AgentTaskManager(
            agent=FoodOrderingAgent(),
            verify_signatures=verify_signatures
        )
        # Set agent ethereum address
        task_manager.agent_address = ethereum_address
        logger.info(f"Agent ethereum address set to: {ethereum_address}")
        
        # Also set it as environment variable for easier access
        os.environ['AGENT_ETH_ADDRESS'] = ethereum_address
        
        server = A2AServer(
            agent_card=agent_card,
            task_manager=task_manager,
            host=host,
            port=port,
        )
        server.start()
    except MissingAPIKeyError as e:
        logger.error(f'Error: {e}')
        exit(1)
    except Exception as e:
        logger.error(f'An error occurred during server startup: {e}')
        exit(1)


if __name__ == '__main__':
    main()
