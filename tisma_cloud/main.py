"""Entry point of the master server: uvicorn tisma_cloud.main:app"""

from .api import create_app

app = create_app()
