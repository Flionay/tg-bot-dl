"""Generate browser test data with the real backend serializer, without login."""
import json
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

with tempfile.TemporaryDirectory() as directory:
    app = types.ModuleType("bot.app")
    app.BASE_FOLDER = app.CONFIG_FOLDER = directory
    app.ADMINS, app.app, app.user = [], NS(), NS()
    sys.modules["bot.app"] = app
    from bot.download import panel, state
    from bot.download.types import Batch, BatchItem

    for kind, count in [("comments", 20), ("range", 51)]:
        batch = Batch(f"{kind}:test", f"{kind}-folder", count, None)
        batch.items = [BatchItem(i + 1, f"file{i}.jpg", status="done" if kind == "comments" and i < 5 else "waiting") for i in range(count)]
        state.active_batches[batch.id] = batch
    live = state.active_batches["comments:test"].items[5]
    live.status, live.received, live.total, live.speed = "downloading", 40, 100, 10
    running = panel.snapshot()
    paused = panel.set_paused(True)
    panel.set_paused(False)
    building = state.active_batches["range:test"]
    building.building = True
    building.items = building.items[:1]
    print(json.dumps({"running": running, "paused": paused, "building": panel.snapshot()}))
