"""在全新副本中验证文本参数及迭代器分块的组合兼容性。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from boltons.iterutils import chunked, chunked_iter
from boltons.strutils import slugify

assert slugify("Hello World!", lower=False) == "Hello_World"
assert slugify("Hello World!") == "hello_world"
assert slugify("Hello World!", delim="-", lower=False) == "Hello-World"
assert slugify("") == ""
assert slugify("!!!", delim="-") == "-"
assert slugify("Kurt Gödel", ascii=True) == b"kurt_goedel"
assert chunked(range(5), 2, fill=None) == [[0, 1], [2, 3], [4, None]]
assert chunked(range(5), 2, fill=0) == [[0, 1], [2, 3], [4, 0]]
assert chunked(range(5), 2) == [[0, 1], [2, 3], [4]]
assert list(chunked_iter((x for x in range(3)), 2, fill=False)) == [[0, 1], [2, False]]
assert chunked(range(9), 2, count=2, fill=None) == [[0, 1], [2, 3]]
try:
    chunked(range(2), 0)
except ValueError:
    pass
else:
    raise AssertionError("invalid size accepted")
print("hidden slugify options/generator/fill/count/error checks passed")
