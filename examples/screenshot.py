"""Save PNG screenshots of the QGIS window and of the map canvas (run in the QGIS Python console).

    import os; exec(open(os.path.expanduser("~/qgis-eopf-geozarr/examples/screenshot.py")).read())
    screenshot("~/Pictures/eopf")      # -> eopf_window.png and eopf_map.png

The map canvas is saved once it has finished drawing, because remote Zarr/XYZ layers
can take a few seconds to render.
"""
import os

from qgis.PyQt.QtCore import QTimer
from qgis.utils import iface


def screenshot(prefix="~/qgis_screenshot", delay_ms=2000):
    prefix = os.path.expanduser(prefix)
    canvas = iface.mapCanvas()

    def save():
        if canvas.isDrawing():  # still rendering: try again later
            QTimer.singleShot(delay_ms, save)
            return
        iface.mainWindow().grab().save(f"{prefix}_window.png")
        canvas.saveAsImage(f"{prefix}_map.png")
        print(f"saved {prefix}_window.png and {prefix}_map.png")

    QTimer.singleShot(delay_ms, save)
