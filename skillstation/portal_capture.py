"""Current local admission portal page extraction."""
import re
from pathlib import Path

def read_page(file):
    html = Path(file).read_text(encoding='utf-8')
    return re.findall(r'data-booking="([^"]+)"', html)
