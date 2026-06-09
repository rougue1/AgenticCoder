import sys
from pathlib import Path

# Add agentic-coder/ to sys.path so `app.backend.*` is importable
sys.path.insert(0, str(Path(__file__).parent))
