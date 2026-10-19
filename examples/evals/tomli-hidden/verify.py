"""在全新验证副本中检查 TOML 的多模块回归与兼容行为。"""

import io
import math
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))
import tomli

assert tomli.loads('name="hello"\r\ncount=3\r\n') == {"name": "hello", "count": 3}
assert tomli.load(io.BytesIO(b"[group]\r\na=[1,2,3]\r\n")) == {"group": {"a": [1, 2, 3]}}
for text in ["0.1", "1_000.25", "-2.5e-3", "+1.0E2"]:
    value = tomli.loads("number=" + text, parse_float=Decimal)["number"]
    assert isinstance(value, Decimal) and value == Decimal(text)
assert tomli.loads("integer=0xFF\n") == {"integer": 255}
assert tomli.loads("t=2001-01-01T00:00:00Z\n")["t"] == datetime(2001, 1, 1, tzinfo=UTC)
assert math.isnan(tomli.loads("n=nan")["n"])
try:
    tomli.loads("bad=1\rbad2=2")
except tomli.TOMLDecodeError:
    pass
else:
    raise AssertionError("bare carriage return must remain invalid")
try:
    tomli.loads("x=1.5", parse_float=lambda x: [])
except ValueError:
    pass
else:
    raise AssertionError("illegal parse_float result accepted")
print("hidden TOML CRLF/Decimal/binary/date/invalid-input checks passed")
