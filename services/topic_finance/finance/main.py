"""Topic 3 service: finance analyst team (AutoGen)."""
from agency_platform.agents import create_topic_service
from finance.agent import FinanceAgent
from finance.params import settings

app = create_topic_service(FinanceAgent(), settings())
