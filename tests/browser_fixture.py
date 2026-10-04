"""Not included in the production image or public static files."""
import os
from pathlib import Path
from server.main import Settings, create_app
from tests.test_app import FixtureEngine

app = create_app(Settings(data_dir=Path(os.environ["Y2AUDIO_TEST_DATA"])), FixtureEngine())
