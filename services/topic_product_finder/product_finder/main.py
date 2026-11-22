"""Topic 2 service: cheapest product finder (CrewAI)."""
import product_finder  # noqa: F401  (disables CrewAI telemetry before crewai is imported)
from agency_platform.agents import create_topic_service
from product_finder.agent import ProductFinderAgent
from product_finder.params import settings

app = create_topic_service(ProductFinderAgent(), settings())
