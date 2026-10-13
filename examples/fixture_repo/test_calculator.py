"""定义 M02 fixture 修复前失败、修复后通过的最小回归测试。"""

import unittest

from calculator import add


class CalculatorTests(unittest.TestCase):
    """验证示例加法函数的正常输入与跨正负号输入。"""

    def test_add(self) -> None:
        """要求 fixture 实现正确处理普通加法和跨正负号加法。"""
        self.assertEqual(add(2, 3), 5)
        self.assertEqual(add(-1, 1), 0)
